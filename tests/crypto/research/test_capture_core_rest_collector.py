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
    # at t=120s, only the 60s-cadence (HIGH) series are due, not the 300s ones
    due = rc.due_series(specs, now=120.0, last_run=last)
    assert {s.name for s in due} == {"open_interest", "premium_index"}


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
