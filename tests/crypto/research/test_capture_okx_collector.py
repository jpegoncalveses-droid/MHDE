"""OKX Stage A: collector end-to-end against a fake client + the gap manifest.

The factories reuse ``RestPresentStateCollector`` UNCHANGED — pacing, dedup
cursor, writers, universe re-resolve all come from capture_core. What is pinned
here: one ``collect_once`` writes all 7 as-of datasets (and klines via its own
collector) under Binance-style ``symbol=`` partitions in the OKX root; windowed
rubik series dedup across polls; a poll gap longer than the silence threshold
writes a ``_gaps`` manifest row (reason ``rest_outage``), reproducing the
capture-outage contract for REST collectors.
"""
from __future__ import annotations

import asyncio
import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import config as cc_cfg
from crypto.research.capture_core_okx import collector as okx_collector

_TS = 1_783_101_300_000
_UNIVERSE = ["BTC-USDT-SWAP", "PEPE-USDT-SWAP"]


def _read(root, dataset):
    rows = []
    for fp in sorted(pathlib.Path(root, dataset).rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


class _FakeOkxClient:
    """Mirrors OkxRestClient.get_with_weight: (payload, None), composite joins
    served under the synthetic ``join:`` endpoints."""

    def __init__(self, ts=_TS):
        self.ts = ts
        self.calls: list[str] = []

    def get_with_weight(self, path, params=None):
        self.calls.append(path)
        return self._payload(path, params or {}), None

    def _payload(self, path, params):
        ts = str(self.ts)
        if path == "/api/v5/public/open-interest":
            return [{"instId": i, "oi": "1", "oiCcy": "10.5", "oiUsd": "1",
                     "ts": ts} for i in _UNIVERSE + ["BTC-USD-SWAP"]]
        if path == "join:premium_index":
            return {
                "mark": [{"instId": i, "markPx": "62143.9", "ts": ts}
                         for i in _UNIVERSE],
                "index": [{"instId": "BTC-USDT", "idxPx": "62150.1", "ts": ts},
                          {"instId": "PEPE-USDT", "idxPx": "0.00001", "ts": ts}],
                "funding": [{"instId": i, "fundingRate": "0.0001",
                             "interestRate": "0.0001",
                             "fundingTime": "1783123200000", "ts": ts}
                            for i in _UNIVERSE],
            }
        if path == "join:basis":
            return {
                "tickers": [{"instId": i, "last": "62310", "ts": ts}
                            for i in _UNIVERSE],
                "index": [{"instId": "BTC-USDT", "idxPx": "62000", "ts": ts},
                          {"instId": "PEPE-USDT", "idxPx": "0.00001", "ts": ts}],
            }
        if "taker-volume-contract" in path:
            return [[ts, "8", "10"]]
        if "long-short" in path:
            return [[ts, "1.5"]]
        if path == "/api/v5/market/candles":
            return [[ts, "62100", "62250", "62050", "62200",
                     "30000", "300.0", "18600000", "1"]]
        raise AssertionError(f"unexpected path {path}")


async def _noop_sleep(_s):
    return None


def _asof_collector(tmp_path, *, client=None, now_ts_ns=None, **kw):
    return okx_collector.build_okx_asof_collector(
        str(tmp_path), client=client or _FakeOkxClient(), universe=_UNIVERSE,
        sleep_fn=_noop_sleep, install_signals=False,
        clock_ns=(lambda: now_ts_ns[0]) if now_ts_ns else (lambda: _TS * 1_000_000),
        **kw)


def _collect(collector, now=0.0):
    asyncio.run(collector.collect_once(now))
    collector.flush_all()


# -- all 7 as-of datasets, Binance-style partitions -------------------------------

def test_collect_once_writes_all_asof_datasets():
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        _collect(_asof_collector(root))
        for name in cc_cfg.CAPTURE_ASOF_DATASETS:
            rows = _read(root, name)
            assert rows, f"no rows for {name}"
        # Binance-style symbol partitions, never instIds
        oi_dirs = {p.name for p in pathlib.Path(root, "open_interest").iterdir()}
        assert oi_dirs == {"symbol=BTCUSDT", "symbol=PEPEUSDT"}
        basis_dirs = {p.name for p in pathlib.Path(root, "basis").iterdir()}
        assert basis_dirs == {"symbol=BTCUSDT", "symbol=PEPEUSDT"}


def test_windowed_series_dedup_across_polls():
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        c = _asof_collector(root)
        _collect(c, now=0.0)
        _collect(c, now=cc_cfg.FUTURES_DATA_CADENCE_S + 1.0)   # ratios due again
        # same rubik bucket re-fetched -> deduped, one row per symbol
        assert len(_read(root, "global_ls_account")) == len(_UNIVERSE)
        # point-in-time OI: each poll is a fresh observation -> two per symbol
        assert len(_read(root, "open_interest")) == 2 * len(_UNIVERSE)


# -- klines collector --------------------------------------------------------------

def test_klines_collector_writes_closed_bars():
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        c = okx_collector.build_okx_klines_collector(
            str(root), client=_FakeOkxClient(), universe=_UNIVERSE,
            sleep_fn=_noop_sleep, install_signals=False,
            clock_ns=lambda: _TS * 1_000_000)
        _collect(c)
        rows = _read(root, cc_cfg.KLINES_DATASET)
        assert {r["s"] for r in rows} == {"BTCUSDT", "PEPEUSDT"}
        assert all(r["trades"] is None and r["takerBuyBase"] is None
                   and r["takerBuyQuote"] is None for r in rows)
        assert all(r["volume"] == "300.0" for r in rows)


# -- klines seed: backward pagination over history-candles -------------------------

class _FakeSeedClient:
    """history-candles pages newest-first; ``after`` returns strictly-older rows."""

    _HOUR = 3_600_000

    def __init__(self, newest_open_ms, n_bars):
        self.newest = newest_open_ms
        self.n = n_bars
        self.calls: list[dict] = []

    def get_with_weight(self, path, params=None):
        assert path == "/api/v5/market/history-candles"
        self.calls.append(dict(params))
        after = int(params["after"])
        limit = int(params["limit"])
        opens = [self.newest - i * self._HOUR for i in range(self.n)]
        older = [o for o in opens if o < after][:limit]
        return [[str(o), "1", "2", "0.5", "1.5", "10", "5.0", "7.5", "1"]
                for o in older], None


def test_seed_klines_pages_backward_and_bounds_horizon():
    import tempfile
    now_ms = _TS
    newest_open = now_ms - (now_ms % 3_600_000) - 3_600_000   # last closed hour
    with tempfile.TemporaryDirectory() as root:
        client = _FakeSeedClient(newest_open, n_bars=100)
        written = okx_collector.seed_klines(
            str(root), days=1, client=client, universe=["BTC-USDT-SWAP"],
            now_ms=now_ms, page_limit=10)
        rows = _read(root, cc_cfg.KLINES_DATASET)
        expected = sum(1 for i in range(100)
                       if newest_open - i * 3_600_000 >= now_ms - 86_400_000)
        assert written == len(rows) == expected                # exactly the 1d horizon
        assert all(r["s"] == "BTCUSDT" for r in rows)
        assert all(r["trades"] is None for r in rows)
        assert min(r["openTime"] for r in rows) >= now_ms - 86_400_000
        # backward pagination: first call anchored at now, then strictly older
        afters = [int(c["after"]) for c in client.calls]
        assert afters[0] == now_ms and all(a < b for a, b in
                                           zip(afters[1:], afters[:-1]))


# -- gap manifest: silence between successful polls -> _gaps row -------------------

def test_gap_manifest_row_after_silent_outage():
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        now_ns = [_TS * 1_000_000]
        c = _asof_collector(root, now_ts_ns=now_ns)
        _collect(c, now=0.0)
        assert _read(root, "_gaps") == []          # first cycle: no prior baseline

        # jump both wall-clock and scheduler far past the silence threshold for
        # the 60s series (factor x cadence), as if the collector was down.
        gap_s = 10 * 60.0
        now_ns[0] += int(gap_s * 1e9)
        _collect(c, now=gap_s)
        gaps = _read(root, "_gaps")
        assert gaps, "expected a rest_outage gap row after the silent window"
        by_stream = {g["stream"] for g in gaps}
        assert "open_interest" in by_stream and "premium_index" in by_stream
        g = next(g for g in gaps if g["stream"] == "open_interest")
        assert g["reason"] == "rest_outage" and g["symbol"] == "*"
        assert g["gap_start_ms"] == _TS
        assert g["gap_end_ms"] == _TS + int(gap_s * 1000)
        # ratio series (1200s cadence): 600s silence is within tolerance -> no gap
        assert "global_ls_account" not in by_stream
