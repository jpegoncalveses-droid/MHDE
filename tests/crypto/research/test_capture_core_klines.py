"""Tests for the long-horizon 1h klines store (capture-completion piece 2).

Covers: closed-bars-only filter; dedup across overlapping hourly polls (zero dup,
no gap); missed-poll backfill by the window; seed pagination ~90d; retention
expiry boundary; shared /fapi weight pacing; universe reuse.
"""
from __future__ import annotations

import asyncio
import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import klines_store as ks
from crypto.research.capture_core import rest_collector as rc

HOUR = cfg.HOUR_MS
_BASE = (1_780_000_000_000 // HOUR) * HOUR  # ms, hour-aligned for arithmetic clarity


def _read(root, dataset="klines_1h"):
    rows = []
    for fp in sorted(pathlib.Path(root, dataset).rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


def _kline(open_ms):
    """A 12-element venue kline array with closeTime = open + 1h - 1ms."""
    return [open_ms, "1.0", "2.0", "0.5", "1.5", "100.0",
            open_ms + HOUR - 1, "150.0", 7, "60.0", "90.0", "0"]


async def _noop_sleep(_s):
    return None


# -- closed-bars-only parser --

def test_parse_klines_drops_in_progress_bar_keeps_closed():
    now_ms = _BASE + 10 * HOUR + 1_800_000   # mid-way through the 10th hour
    data = [_kline(_BASE + 8 * HOUR),         # closed
            _kline(_BASE + 9 * HOUR),         # closed
            _kline(_BASE + 10 * HOUR)]        # closeTime in the future -> in-progress
    rows = ks.parse_klines(data, "BTCUSDT", now_ms * 1_000_000)
    assert [r["openTime"] for r in rows] == [_BASE + 8 * HOUR, _BASE + 9 * HOUR]
    assert all(r["s"] == "BTCUSDT" for r in rows)
    # full row persisted (default-to-inclusion)
    assert set(rows[0]) >= {"openTime", "open", "high", "low", "close", "volume",
                            "closeTime", "quoteVolume", "trades",
                            "takerBuyBase", "takerBuyQuote"}
    assert rows[0]["trades"] == 7 and rows[0]["quoteVolume"] == "150.0"


def test_klines_spec_is_windowed_and_dedups_on_open_time():
    assert ks.KLINES_1H_SPEC.pool == "fapi"
    assert ks.KLINES_1H_SPEC.scope == "per_symbol"
    assert ks.KLINES_1H_SPEC.dedup_ts_field == "openTime"
    assert ks.KLINES_1H_SPEC.params["interval"] == "1h"
    assert ks.KLINES_1H_SPEC.params["limit"] == cfg.KLINES_MAINT_LIMIT


# -- maintenance: dedup across overlapping hourly polls --

class _MaintClient:
    """Returns the trailing ``limit`` hourly bars ending at the current hour of
    state['now_ms']; the most recent bar is in-progress (parser drops it)."""

    def __init__(self, state):
        self.state = state
        self.calls = []

    def get_with_weight(self, path, params=None):
        self.calls.append((path, params))
        now_ms = self.state["now_ms"]
        limit = params["limit"]
        cur_open = (now_ms // HOUR) * HOUR
        bars = [_kline(cur_open - (limit - 1 - i) * HOUR) for i in range(limit)]
        return bars, 1   # /fapi weight 1 at limit<100

    def fetch_usdtm_perp_universe(self):
        return ["BTCUSDT"]


def _maint_collector(tmp_path, state):
    return rc.RestPresentStateCollector(
        root=str(tmp_path), client=_MaintClient(state), universe=["BTCUSDT"],
        specs=[ks.KLINES_1H_SPEC], sleep_fn=_noop_sleep, install_signals=False,
        clock_ns=lambda: state["now_ms"] * 1_000_000)


def _contiguous_hours(open_times):
    return open_times == list(range(open_times[0], open_times[-1] + HOUR, HOUR))


def test_overlapping_hourly_polls_zero_duplicates_no_gap(tmp_path):
    state = {"now_ms": _BASE + 10 * HOUR + 1_800_000}
    col = _maint_collector(tmp_path, state)
    asyncio.run(col.collect_once(now=0.0))
    state["now_ms"] = _BASE + 11 * HOUR + 1_800_000   # one hour later
    asyncio.run(col.collect_once(now=cfg.KLINES_MAINT_CADENCE_S))
    col.flush_all()
    ots = sorted(r["openTime"] for r in _read(tmp_path))
    assert len(ots) == len(set(ots))     # zero duplicate bars across polls
    assert _contiguous_hours(ots)        # no gap
    assert ots[-1] == _BASE + 10 * HOUR  # 11th-hour bar still in-progress, excluded


def test_missed_poll_is_backfilled_by_the_window(tmp_path):
    state = {"now_ms": _BASE + 10 * HOUR + 1_800_000}
    col = _maint_collector(tmp_path, state)
    asyncio.run(col.collect_once(now=0.0))            # closed up to hour 9
    state["now_ms"] = _BASE + 12 * HOUR + 1_800_000   # TWO hours later (a poll skipped)
    asyncio.run(col.collect_once(now=2 * cfg.KLINES_MAINT_CADENCE_S))
    col.flush_all()
    ots = sorted(r["openTime"] for r in _read(tmp_path))
    assert _contiguous_hours(ots)              # the skipped hour was backfilled
    assert ots[-1] == _BASE + 11 * HOUR        # hour 12 in-progress, excluded
    assert len(ots) == len(set(ots))


# -- seed: paginated ~90d backfill --

class _SeedClient:
    """Serves closed hourly bars from history; pages forward by startTime."""

    def __init__(self, now_ms, universe):
        self.now_ms = now_ms
        self._universe = universe
        self.calls = []

    def get_with_weight(self, path, params=None):
        self.calls.append(params["symbol"])
        start = params["startTime"]
        limit = params["limit"]
        bars = []
        ot = (start // HOUR) * HOUR
        while len(bars) < limit and ot + HOUR - 1 < self.now_ms:  # closed only
            bars.append(_kline(ot))
            ot += HOUR
        return bars, 10   # /fapi weight 10 at limit>1000

    def fetch_usdtm_perp_universe(self):
        return list(self._universe)


def test_seed_pagination_covers_full_horizon_two_calls_per_symbol(tmp_path):
    days = 90
    now_ms = _BASE + days * 24 * HOUR          # exactly `days` of history available
    client = _SeedClient(now_ms, ["BTCUSDT", "ETHUSDT"])
    written = ks.seed(str(tmp_path), days=days, client=client, now_ms=now_ms,
                      sleep_fn=lambda _s: None)
    rows = _read(tmp_path)
    per_symbol = days * 24                      # 2160 closed 1h bars
    assert written == per_symbol * 2
    btc = sorted(r["openTime"] for r in rows if r["s"] == "BTCUSDT")
    assert len(btc) == per_symbol and _contiguous_hours(btc)
    assert all(r["closeTime"] < now_ms for r in rows)      # closed only
    # ~2 calls/symbol at limit 1500 over 2160 bars
    assert client.calls.count("BTCUSDT") == 2


def test_seed_uses_universe_resolver_when_universe_omitted(tmp_path):
    now_ms = _BASE + 30 * 24 * HOUR
    client = _SeedClient(now_ms, ["AAAUSDT", "BBBUSDT"])
    ks.seed(str(tmp_path), days=30, client=client, now_ms=now_ms, sleep_fn=lambda _s: None)
    symbols = {r["s"] for r in _read(tmp_path)}
    assert symbols == {"AAAUSDT", "BBBUSDT"}


# -- seed shares the /fapi weight pacer --

def test_seed_paces_under_fapi_weight_budget(tmp_path):
    slept = []

    class _HotClient:
        def __init__(self): self.calls = 0
        def get_with_weight(self, path, params=None):
            self.calls += 1
            # one page per symbol then end (short history), reporting over-budget weight
            start = params["startTime"]
            over = int(cfg.REST_BUDGET_FRACTION * cfg.FAPI_WEIGHT_LIMIT) + 100
            return [_kline((start // HOUR) * HOUR)], over
        def fetch_usdtm_perp_universe(self):
            return ["AUSDT", "BUSDT", "CUSDT"]

    now_ms = _BASE + 2 * HOUR
    ks.seed(str(tmp_path), days=1, client=_HotClient(), now_ms=now_ms,
            sleep_fn=lambda s: slept.append(s))
    # after the first over-budget response, every subsequent symbol pre-sleeps the backoff
    assert slept and all(s == cfg.REST_BUDGET_BACKOFF_S for s in slept)


# -- retention expiry boundary --

def _make_partition(root, symbol, day):
    d = pathlib.Path(root, "klines_1h", f"symbol={symbol}", f"date={day}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "part-x.parquet").write_bytes(b"x")
    return d


def test_expire_removes_old_partitions_keeps_cutoff_boundary(tmp_path):
    now_ms = _BASE + 200 * 24 * HOUR
    keep_today = ks._date_str(now_ms)
    keep_inside = ks._date_str(now_ms - 89 * 86_400_000)
    keep_boundary = ks._date_str(now_ms - 90 * 86_400_000)   # == cutoff -> kept
    expire_old = ks._date_str(now_ms - 91 * 86_400_000)      # < cutoff -> removed
    for day in (keep_today, keep_inside, keep_boundary, expire_old):
        _make_partition(str(tmp_path), "BTCUSDT", day)
    removed = ks.expire_klines_partitions(str(tmp_path), days=90, now_ms=now_ms)
    assert any(expire_old in r for r in removed)
    surviving = {p.name.split("date=")[1]
                 for p in pathlib.Path(tmp_path, "klines_1h", "symbol=BTCUSDT").iterdir()}
    assert surviving == {keep_today, keep_inside, keep_boundary}


def test_expire_is_noop_when_dataset_absent(tmp_path):
    assert ks.expire_klines_partitions(str(tmp_path), days=90) == []
