"""Tests for the budget-aware REST present-state collector."""
from __future__ import annotations

import asyncio
import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import rest_series as rs
from crypto.research.capture_core import rest_collector as rc
from crypto.research.capture_core.client import RateLimited

_TS = 1_780_524_000_000


def _read(root, dataset):
    rows = []
    for fp in sorted(pathlib.Path(root, dataset).rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


class _FakeClient:
    def __init__(self, used=100, rate_limit_paths=()):
        self.used = used
        self.rate_limit_paths = set(rate_limit_paths)
        self.calls = []

    def get_with_weight(self, path, params=None):
        self.calls.append(path)
        if path in self.rate_limit_paths:
            raise RateLimited(429, 1.0)
        used = self.used if path.startswith("/fapi") else None
        return self._payload(path, params), used

    def _payload(self, path, params):
        if path.endswith("/openInterest"):
            return {"symbol": params["symbol"], "openInterest": "100.0", "time": _TS}
        if path.endswith("/premiumIndex"):
            return [{"symbol": "BTCUSDT", "markPrice": "1", "indexPrice": "1",
                     "estimatedSettlePrice": "1", "lastFundingRate": "0",
                     "interestRate": "0.0001", "nextFundingTime": 1, "time": _TS},
                    {"symbol": "ETHUSDT", "markPrice": "2", "indexPrice": "2",
                     "estimatedSettlePrice": "2", "lastFundingRate": "0",
                     "interestRate": "0.0001", "nextFundingTime": 1, "time": _TS}]
        if "LongShort" in path:
            return [{"symbol": params["symbol"], "longAccount": "0.6",
                     "shortAccount": "0.4", "longShortRatio": "1.5", "timestamp": _TS}]
        if "takerlongshort" in path:
            return [{"buySellRatio": "1.1", "buyVol": "10", "sellVol": "9",
                     "timestamp": _TS}]
        if path.endswith("/basis"):
            return [{"pair": params["pair"], "contractType": "PERPETUAL",
                     "indexPrice": "1", "futuresPrice": "1", "basis": "0",
                     "basisRate": "0", "annualizedBasisRate": "", "timestamp": _TS}]
        return {}


def _collector(tmp_path, **kw):
    return rc.RestPresentStateCollector(
        root=str(tmp_path), client=kw.pop("client", _FakeClient()),
        universe=kw.pop("universe", ["BTCUSDT", "ETHUSDT"]),
        sleep_fn=kw.pop("sleep_fn", _noop_sleep), install_signals=False,
        clock_ns=lambda: _TS * 1_000_000, **kw)


async def _noop_sleep(_s):
    return None


# -- pure helpers --

def test_due_series_respects_cadence():
    specs = rs.SERIES
    assert len(rc.due_series(specs, now=0.0, last_run={})) == len(specs)  # all due first
    last = {s.name: 0.0 for s in specs}
    # at t=120s, only the 60s-cadence (HIGH) series are due, not the coarsened
    # /futures/data ones.
    due = rc.due_series(specs, now=120.0, last_run=last)
    assert {s.name for s in due} == {"open_interest", "premium_index"}


def test_futures_data_series_cadence_is_coarsened():
    from crypto.research.capture_core import config as cfg
    fd = [s for s in rs.SERIES if s.pool == "futures_data"]
    assert fd, "expected /futures/data series in the registry"
    # All /futures/data series sample on the coarsened cadence (well above the old
    # 5m), so a full sweep fits under the verified IP ceiling.
    assert all(s.target_cadence_s == cfg.FUTURES_DATA_CADENCE_S for s in fd)
    assert cfg.FUTURES_DATA_CADENCE_S >= 600.0


def test_dedup_new_buckets_keeps_distinct_and_advances_last():
    rows = [{"timestamp": 100}, {"timestamp": 200}, {"timestamp": 300}]
    kept, last = rc.dedup_new_buckets(rows, "timestamp", None)
    assert [r["timestamp"] for r in kept] == [100, 200, 300] and last == 300
    # overlapping next poll: only buckets newer than last survive
    kept2, last2 = rc.dedup_new_buckets(
        [{"timestamp": 200}, {"timestamp": 300}, {"timestamp": 400}], "timestamp", last)
    assert [r["timestamp"] for r in kept2] == [400] and last2 == 400
    # fully-overlapping poll: nothing new, last unchanged
    kept3, last3 = rc.dedup_new_buckets([{"timestamp": 400}], "timestamp", last2)
    assert kept3 == [] and last3 == 400


def test_fapi_over_budget_threshold():
    # 0.70 * 2400 = 1680 is the trigger; at/over -> True, under -> False
    assert rc.fapi_over_budget(1680, limit=2400, fraction=0.70) is True
    assert rc.fapi_over_budget(1679, limit=2400, fraction=0.70) is False
    assert rc.fapi_over_budget(0, limit=2400, fraction=0.70) is False


def test_fd_pace_wait_caps_raw_request_rate():
    # headroom -> no wait
    assert rc.fd_pace_wait(0.0, 5, now=10.0, budget=10, window_s=300.0) == 0.0
    # at budget -> wait until the oldest in-window request ages out
    assert rc.fd_pace_wait(0.0, 10, now=100.0, budget=10, window_s=300.0) == 200.0
    # oldest already aged past the window -> non-negative guard
    assert rc.fd_pace_wait(0.0, 10, now=400.0, budget=10, window_s=300.0) == 0.0
    # empty window -> no wait
    assert rc.fd_pace_wait(None, 0, now=5.0, budget=10, window_s=300.0) == 0.0


def test_select_under_pressure_drops_fapi_non_high_only():
    specs = rs.SERIES
    over = rc.select_under_pressure(specs, used_weight=2000, limit=2400, fraction=0.7)
    names = {s.name for s in over}
    assert "open_interest" in names and "premium_index" in names      # fapi HIGH kept
    assert "global_ls_account" in names and "basis" in names          # futures_data kept
    # under budget -> everything kept
    assert len(rc.select_under_pressure(specs, used_weight=10, limit=2400, fraction=0.7)) == len(specs)
    # None (futures_data-only cycle) -> kept
    assert len(rc.select_under_pressure(specs, used_weight=None, limit=2400, fraction=0.7)) == len(specs)


# -- integration: one full pass writes each dataset --

def test_collect_once_writes_every_series(tmp_path):
    col = _collector(tmp_path)
    asyncio.run(col.collect_once(now=0.0))
    col.flush_all()
    assert len(_read(tmp_path, "open_interest")) == 2          # per-symbol x2
    assert len(_read(tmp_path, "premium_index")) == 2          # all -> fan out
    assert len(_read(tmp_path, "global_ls_account")) == 2
    assert len(_read(tmp_path, "taker_ls_ratio")) == 2
    assert len(_read(tmp_path, "basis")) == 2
    # premium_index kept interestRate (the genuinely-new field vs the markPrice WS)
    assert _read(tmp_path, "premium_index")[0]["interestRate"] == "0.0001"


# -- /fapi self-pacing off the live used-weight --

def test_fapi_paces_when_over_budget(tmp_path):
    slept = []

    async def sleep_fn(s):
        slept.append(s)

    col = _collector(tmp_path, client=_FakeClient(used=2000),
                     specs=[next(s for s in rs.SERIES if s.name == "open_interest")],
                     sleep_fn=sleep_fn)
    asyncio.run(col.collect_once(now=0.0))
    # first symbol: used starts 0 -> no pre-sleep; after it returns 2000 (>0.7*2400)
    # the second symbol pre-sleeps the budget backoff.
    assert slept  # at least one budget backoff occurred


# -- /futures/data raw-count pacing: rolling-window request budget --

def test_futures_data_raw_count_pacing_blocks_over_budget(tmp_path):
    t = {"now": 0.0}
    slept = []

    async def sleep_fn(s):
        slept.append(s)
        t["now"] += s

    # budget=2 per 300s window, no floor spacing -> the 3rd per-symbol request must
    # wait ~one window for the oldest to age out.
    col = rc.RestPresentStateCollector(
        root=str(tmp_path), client=_FakeClient(),
        universe=["A", "B", "C"],
        specs=[next(s for s in rs.SERIES if s.name == "global_ls_account")],
        sleep_fn=sleep_fn, now_fn=lambda: t["now"], install_signals=False,
        clock_ns=lambda: _TS * 1_000_000,
        futures_data_req_budget=2, futures_data_window_s=300.0,
        futures_data_min_interval_s=0.0)
    asyncio.run(col.collect_once(now=0.0))
    col.flush_all()
    assert any(abs(s - 300.0) < 1e-6 for s in slept), slept
    # all three still got written after the pacing wait
    assert len(_read(tmp_path, "global_ls_account")) == 3


# -- /futures/data windowed dedup: overlapping polls keep every distinct bucket --

class _WindowClient:
    """Returns the trailing ``limit`` 5m buckets ending at state['latest'] (in
    bucket units; ts = unit*300_000 ms). Advancing state['latest'] between polls
    simulates time passing."""

    def __init__(self, state):
        self.state = state
        self.calls = []

    def get_with_weight(self, path, params=None):
        self.calls.append(path)
        n = params["limit"]
        latest = self.state["latest"]
        rows = [{"symbol": params.get("symbol"), "longAccount": "0.6",
                 "shortAccount": "0.4", "longShortRatio": "1.5",
                 "timestamp": (latest - (n - 1) + i) * 300_000} for i in range(n)]
        return rows, None


def _ls_collector(tmp_path, client):
    spec = next(s for s in rs.SERIES if s.name == "global_ls_account")
    return rc.RestPresentStateCollector(
        root=str(tmp_path), client=client, universe=["BTCUSDT"], specs=[spec],
        sleep_fn=_noop_sleep, install_signals=False, clock_ns=lambda: _TS * 1_000_000)


def test_overlapping_polls_retain_every_bucket_zero_duplicates(tmp_path):
    state = {"latest": 8}
    col = _ls_collector(tmp_path, _WindowClient(state))
    asyncio.run(col.collect_once(now=0.0))       # buckets 1..8
    state["latest"] = 12                          # +4 buckets (one 20-min poll later)
    asyncio.run(col.collect_once(now=1200.0))     # window 5..12; only 9..12 are new
    col.flush_all()
    ts = sorted(r["timestamp"] for r in _read(tmp_path, "global_ls_account"))
    assert ts == [u * 300_000 for u in range(1, 13)]   # 1..12 continuous
    assert len(ts) == len(set(ts))                     # zero duplicate buckets


def test_skipped_poll_is_backfilled_by_the_limit(tmp_path):
    state = {"latest": 8}
    col = _ls_collector(tmp_path, _WindowClient(state))
    asyncio.run(col.collect_once(now=0.0))       # buckets 1..8
    state["latest"] = 16                          # +8 buckets: a whole poll was skipped
    asyncio.run(col.collect_once(now=2400.0))     # window 9..16 (limit 8) backfills the gap
    col.flush_all()
    ts = sorted(r["timestamp"] for r in _read(tmp_path, "global_ls_account"))
    assert ts == [u * 300_000 for u in range(1, 17)]   # no permanent hole
    assert len(ts) == len(set(ts))


def test_fapi_series_not_deduped(tmp_path):
    # /fapi point-in-time series have dedup_ts_field=None -> always append, even when
    # the venue 'time' field repeats across polls.
    col = _collector(tmp_path, universe=["BTCUSDT"],
                     specs=[next(s for s in rs.SERIES if s.name == "open_interest")])
    asyncio.run(col.collect_once(now=0.0))
    asyncio.run(col.collect_once(now=60.0))
    col.flush_all()
    assert len(_read(tmp_path, "open_interest")) == 2   # both kept (no dedup)


# -- /futures/data 429 degrades LOW first, never HIGH --

def test_futures_data_429_degrades_low_tier(tmp_path):
    col = _collector(tmp_path,
                     client=_FakeClient(rate_limit_paths=["/futures/data/basis"]),
                     universe=["BTCUSDT"])
    asyncio.run(col.collect_once(now=0.0))
    basis = next(s for s in rs.SERIES if s.name == "basis")
    oi = next(s for s in rs.SERIES if s.name == "open_interest")
    assert col._is_degraded(basis, now=1.0) is True       # LOW suppressed after its 429
    assert col._is_degraded(oi, now=1.0) is False         # HIGH never degraded
