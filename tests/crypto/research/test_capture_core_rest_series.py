"""Tests for the REST present-state series registry (parsers + request + schema)."""
from __future__ import annotations

import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import rest_series as rs
from crypto.research.capture_core import store

_TS = 1_780_524_000_000  # ms


def _spec(name):
    return next(s for s in rs.SERIES if s.name == name)


def test_parse_open_interest():
    rows = rs._parse_open_interest(
        {"symbol": "BTCUSDT", "openInterest": "104359.057", "time": _TS}, "BTCUSDT", 7)
    assert rows == [{"recv_ts_ns": 7, "s": "BTCUSDT",
                     "openInterest": "104359.057", "time": _TS}]


def test_parse_premium_index_fans_out_and_keeps_interest_rate_and_index():
    data = [
        {"symbol": "BTCUSDT", "markPrice": "1", "indexPrice": "1.1",
         "estimatedSettlePrice": "1.2", "lastFundingRate": "0.0001",
         "interestRate": "0.0001", "nextFundingTime": 9, "time": _TS},
        {"symbol": "ETHUSDT", "markPrice": "2", "indexPrice": "2.1",
         "estimatedSettlePrice": "2.2", "lastFundingRate": "0.0002",
         "interestRate": "0.0001", "nextFundingTime": 9, "time": _TS},
    ]
    rows = rs._parse_premium_index(data, None, 5)
    assert [r["s"] for r in rows] == ["BTCUSDT", "ETHUSDT"]
    assert rows[0]["interestRate"] == "0.0001" and rows[0]["indexPrice"] == "1.1"


def test_parse_ls_ratio_attaches_symbol_when_absent():
    rows = rs._parse_ls_ratio(
        [{"longAccount": "0.6", "shortAccount": "0.4", "longShortRatio": "1.5",
          "timestamp": _TS}], "BTCUSDT", 3)
    assert rows[0]["s"] == "BTCUSDT" and rows[0]["longShortRatio"] == "1.5"


def test_parse_taker_ls_uses_request_symbol():
    rows = rs._parse_taker_ls(
        [{"buySellRatio": "1.1", "buyVol": "10", "sellVol": "9", "timestamp": _TS}],
        "SOLUSDT", 4)
    assert rows[0]["s"] == "SOLUSDT" and rows[0]["buySellRatio"] == "1.1"


def test_parse_basis_uses_request_pair():
    rows = rs._parse_basis(
        [{"contractType": "PERPETUAL", "indexPrice": "1", "futuresPrice": "1",
          "basis": "-0.5", "basisRate": "-0.0003", "annualizedBasisRate": "",
          "timestamp": _TS}], "BTCUSDT", 2)
    assert rows[0]["pair"] == "BTCUSDT" and rows[0]["basisRate"] == "-0.0003"


def test_fd_limit_covers_two_poll_intervals_and_marks_dedup():
    from crypto.research.capture_core import config as cfg
    buckets_per_poll = cfg.FUTURES_DATA_CADENCE_S // 300
    # limit covers ~2 poll intervals so a single missed/late poll self-heals
    assert rs._FD_LIMIT >= 2 * buckets_per_poll
    assert rs._FD_LIMIT >= 8
    assert rs._FD_LIMIT <= 500  # Binance /futures/data limit cap
    for s in rs.SERIES:
        if s.pool == "futures_data":
            assert s.params["limit"] == rs._FD_LIMIT
            assert s.dedup_ts_field == "timestamp"   # windowed -> needs bucket dedup
        else:
            assert s.dedup_ts_field is None          # /fapi point-in-time -> no dedup


def test_request_builds_per_symbol_per_pair_and_all():
    assert _spec("open_interest").request("BTCUSDT") == (
        "/fapi/v1/openInterest", {"symbol": "BTCUSDT"})
    assert _spec("premium_index").request(None) == ("/fapi/v1/premiumIndex", {})
    path, params = _spec("basis").request("BTCUSDT")
    assert params["pair"] == "BTCUSDT" and params["contractType"] == "PERPETUAL"


def test_series_schema_round_trips_via_dataset_writer(tmp_path):
    spec = _spec("open_interest")
    w = store.dataset_writer(str(tmp_path), spec.name, spec.schema,
                             symbol_key=spec.symbol_key, time_key=spec.time_key)
    for r in spec.parse({"symbol": "BTCUSDT", "openInterest": "9.0", "time": _TS}, "BTCUSDT", 1):
        w.append(r)
    w.flush_all()
    files = sorted(pathlib.Path(tmp_path, "open_interest").rglob("*.parquet"))
    assert files and files[0].relative_to(tmp_path / "open_interest").parts[0] == "symbol=BTCUSDT"
    rows = pq.read_table(str(files[0])).to_pylist()
    assert rows[0]["openInterest"] == "9.0"


def test_basis_partitions_on_pair(tmp_path):
    spec = _spec("basis")
    w = store.dataset_writer(str(tmp_path), spec.name, spec.schema,
                             symbol_key=spec.symbol_key, time_key=spec.time_key)
    for r in spec.parse([{"contractType": "PERPETUAL", "indexPrice": "1",
                          "futuresPrice": "1", "basis": "0", "basisRate": "0",
                          "annualizedBasisRate": "", "timestamp": _TS}], "ETHUSDT", 1):
        w.append(r)
    w.flush_all()
    files = sorted(pathlib.Path(tmp_path, "basis").rglob("*.parquet"))
    assert files[0].relative_to(tmp_path / "basis").parts[0] == "symbol=ETHUSDT"
