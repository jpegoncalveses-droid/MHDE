"""OKX Stage A: the series registry — schema parity, mapping specifics, parsers.

The write-time-normalization contract, pinned:
  * every OKX as-of spec writes THE SAME parquet schema object as its Binance
    twin (identity, not just equality) and the same dataset name;
  * OI value = ``oiCcy`` (coin units); premium_index = 3-call join with
    ``estimatedSettlePrice`` empty; ratio series derive long/short from r;
    taker uses ``unit=0`` (coin) with buySellRatio computed; basis is computed
    at poll time (last - idx); klines map volCcy/volCcyQuote, NULL the three
    unfillable fields, and persist closed bars only (confirm == "1").
  * Binance-side cadences are kept (60s OI/premium, 1200s ratios, hourly klines).
"""
from __future__ import annotations

import pytest

from crypto.research.capture_core import config as cc_cfg
from crypto.research.capture_core import klines_store as ks
from crypto.research.capture_core import rest_series as rs
from crypto.research.capture_core_okx import series as okx_series
from crypto.research.capture_core_okx.symbols import SymbolMap

_TS = 1_783_101_300_000                      # a 5m-bucket venue ms
_RECV_NS = 1_783_101_360_000 * 1_000_000     # arrival ns

_MAP = SymbolMap(["BTC-USDT-SWAP", "PEPE-USDT-SWAP"])


def _specs():
    return {s.name: s for s in okx_series.build_series(_MAP)}


# -- registry shape: names, schemas (identity), cadences ------------------------

def test_asof_registry_matches_binance_names_exactly():
    assert tuple(s.name for s in okx_series.build_series(_MAP)) == \
        cc_cfg.CAPTURE_ASOF_DATASETS


def test_schemas_are_the_binance_schema_objects():
    specs = _specs()
    assert specs["open_interest"].schema is rs.OPEN_INTEREST_SCHEMA
    assert specs["premium_index"].schema is rs.PREMIUM_INDEX_SCHEMA
    for name in ("global_ls_account", "top_ls_account", "top_ls_position"):
        assert specs[name].schema is rs.LS_RATIO_SCHEMA
    assert specs["taker_ls_ratio"].schema is rs.TAKER_LS_SCHEMA
    assert specs["basis"].schema is rs.BASIS_SCHEMA
    assert okx_series.build_klines_spec().schema is ks.KLINES_1H_SCHEMA


def test_binance_cadences_are_kept():
    specs = _specs()
    assert specs["open_interest"].target_cadence_s == 60.0
    assert specs["premium_index"].target_cadence_s == 60.0
    for name in ("global_ls_account", "top_ls_account", "top_ls_position",
                 "taker_ls_ratio", "basis"):
        assert specs[name].target_cadence_s == cc_cfg.FUTURES_DATA_CADENCE_S
    assert okx_series.build_klines_spec().target_cadence_s == \
        cc_cfg.KLINES_MAINT_CADENCE_S


def test_partition_keys_match_binance():
    specs = _specs()
    for name in ("open_interest", "premium_index", "global_ls_account",
                 "top_ls_account", "top_ls_position", "taker_ls_ratio"):
        assert specs[name].symbol_key == "s"
    assert specs["basis"].symbol_key == "pair"
    assert specs["open_interest"].time_key == "time"
    assert specs["basis"].time_key == "timestamp"


def test_per_symbol_requests_use_instid():
    spec = _specs()["global_ls_account"]
    path, params = spec.request("BTC-USDT-SWAP")
    assert "rubik" in path and "long-short-account-ratio-contract" in path
    assert params["instId"] == "BTC-USDT-SWAP"
    assert params["period"] == "5m"


def test_taker_requests_coin_units():
    _, params = _specs()["taker_ls_ratio"].request("BTC-USDT-SWAP")
    assert params["unit"] == "0"             # 0 = coin; default 1 = contracts (trap)


def test_windowed_series_dedup_on_timestamp():
    specs = _specs()
    for name in ("global_ls_account", "top_ls_account", "top_ls_position",
                 "taker_ls_ratio"):
        assert specs[name].dedup_ts_field == "timestamp"
    # point-in-time series: every poll is a fresh observation
    assert specs["open_interest"].dedup_ts_field is None
    assert specs["premium_index"].dedup_ts_field is None
    assert specs["basis"].dedup_ts_field is None


# -- open_interest: one bulk call, oiCcy (coin), universe-filtered ---------------

def test_open_interest_bulk_parse_uses_oiccy_and_filters_universe():
    spec = _specs()["open_interest"]
    path, params = spec.request(None)
    assert path == "/api/v5/public/open-interest" and params == {"instType": "SWAP"}
    payload = [
        {"instId": "BTC-USDT-SWAP", "oi": "3245640.36", "oiCcy": "32456.4036",
         "oiUsd": "2016967499.67", "ts": str(_TS)},
        {"instId": "BTC-USD-SWAP", "oi": "1", "oiCcy": "1", "oiUsd": "1",
         "ts": str(_TS)},                                  # inverse -> dropped
        {"instId": "AAPL-USDT-SWAP", "oi": "2", "oiCcy": "2", "oiUsd": "2",
         "ts": str(_TS)},                                  # equity perp -> dropped
    ]
    rows = spec.parse(payload, None, _RECV_NS)
    assert rows == [{"recv_ts_ns": _RECV_NS, "s": "BTCUSDT",
                     "openInterest": "32456.4036", "time": _TS}]


# -- premium_index: 3-call join ---------------------------------------------------

def _premium_payload():
    return {
        "mark": [{"instId": "BTC-USDT-SWAP", "markPx": "62143.9", "ts": str(_TS)},
                 {"instId": "PEPE-USDT-SWAP", "markPx": "0.0000105", "ts": str(_TS)},
                 {"instId": "BTC-USD-SWAP", "markPx": "1", "ts": str(_TS)}],
        "index": [{"instId": "BTC-USDT", "idxPx": "62150.1", "ts": str(_TS)}],
        "funding": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0000493",
                     "interestRate": "0.0001", "fundingTime": "1783123200000",
                     "nextFundingTime": "1783152000000", "ts": str(_TS)}],
    }


def test_premium_index_join_maps_fields():
    rows = _specs()["premium_index"].parse(_premium_payload(), None, _RECV_NS)
    by_s = {r["s"]: r for r in rows}
    btc = by_s["BTCUSDT"]
    assert btc == {
        "recv_ts_ns": _RECV_NS, "s": "BTCUSDT", "markPrice": "62143.9",
        "indexPrice": "62150.1", "estimatedSettlePrice": "",
        "lastFundingRate": "0.0000493", "interestRate": "0.0001",
        "nextFundingTime": 1783123200000,       # OKX fundingTime = Binance nextFundingTime
        "time": _TS,
    }


def test_premium_index_join_tolerates_missing_legs():
    # PEPE has a mark but no index/funding leg in the fixture: row still emitted
    # with honest-empty venue strings (Binance parser convention) and 0 fallback
    # for the int field; the inverse instId is dropped by the universe filter.
    rows = _specs()["premium_index"].parse(_premium_payload(), None, _RECV_NS)
    by_s = {r["s"]: r for r in rows}
    assert set(by_s) == {"BTCUSDT", "PEPEUSDT"}
    pepe = by_s["PEPEUSDT"]
    assert pepe["indexPrice"] == "" and pepe["lastFundingRate"] == ""
    assert pepe["nextFundingTime"] == 0


# -- ratio series: [ts, ratio] -> derived long/short ------------------------------

def test_ls_ratio_derives_long_short_from_r():
    spec = _specs()["global_ls_account"]
    rows = spec.parse([[str(_TS), "1.5"], [str(_TS - 300_000), "1.0"]],
                      "BTC-USDT-SWAP", _RECV_NS)
    assert rows[0] == {"recv_ts_ns": _RECV_NS, "s": "BTCUSDT",
                       "longAccount": "0.6", "shortAccount": "0.4",
                       "longShortRatio": "1.5", "timestamp": _TS}
    assert rows[1]["longAccount"] == "0.5" and rows[1]["shortAccount"] == "0.5"


# -- taker: [ts, sellVol, buyVol] (SELL FIRST), ratio computed --------------------

def test_taker_parse_sell_first_and_ratio_derived():
    rows = _specs()["taker_ls_ratio"].parse([[str(_TS), "8", "10"]],
                                            "BTC-USDT-SWAP", _RECV_NS)
    assert rows == [{"recv_ts_ns": _RECV_NS, "s": "BTCUSDT",
                     "buySellRatio": "1.25", "buyVol": "10", "sellVol": "8",
                     "timestamp": _TS}]


def test_taker_zero_sell_gives_empty_ratio():
    rows = _specs()["taker_ls_ratio"].parse([[str(_TS), "0", "10"]],
                                            "BTC-USDT-SWAP", _RECV_NS)
    assert rows[0]["buySellRatio"] == ""     # honest-missing, _safe_float -> None


# -- basis: computed at poll time from tickers + index-tickers --------------------

def test_basis_computed_from_last_minus_idx():
    payload = {
        "tickers": [{"instId": "BTC-USDT-SWAP", "last": "62310", "ts": str(_TS)},
                    {"instId": "AAPL-USDT-SWAP", "last": "5", "ts": str(_TS)}],
        "index": [{"instId": "BTC-USDT", "idxPx": "62000", "ts": str(_TS)}],
    }
    rows = _specs()["basis"].parse(payload, None, _RECV_NS)
    assert rows == [{
        "recv_ts_ns": _RECV_NS, "pair": "BTCUSDT", "contractType": "PERPETUAL",
        "indexPrice": "62000", "futuresPrice": "62310",
        "basis": "310", "basisRate": "0.005", "annualizedBasisRate": "",
        "timestamp": _TS,
    }]


def test_basis_skips_symbols_without_both_legs():
    payload = {
        "tickers": [{"instId": "PEPE-USDT-SWAP", "last": "0.00001", "ts": str(_TS)}],
        "index": [],                          # no index leg -> no basis row
    }
    assert _specs()["basis"].parse(payload, None, _RECV_NS) == []


# -- klines: closed-only via confirm, volCcy mapping, NULL unfillables ------------

def _candles():
    # OKX returns newest-first; confirm "0" = in-progress (never persisted)
    return [
        [str(_TS + 3_600_000), "62200", "62300", "62100", "62250",
         "27186.69", "271.8669", "16891751.4", "0"],
        [str(_TS), "62100", "62250", "62050", "62200",
         "30000.00", "300.0000", "18600000.0", "1"],
    ]


def test_klines_parse_closed_only_with_null_unfillables():
    spec = okx_series.build_klines_spec()
    path, params = spec.request("BTC-USDT-SWAP")
    assert path == "/api/v5/market/candles"
    assert params["instId"] == "BTC-USDT-SWAP" and params["bar"] == "1H"
    assert params["limit"] == cc_cfg.KLINES_MAINT_LIMIT
    rows = spec.parse(_candles(), "BTC-USDT-SWAP", _RECV_NS)
    assert rows == [{
        "recv_ts_ns": _RECV_NS, "s": "BTCUSDT",
        "openTime": _TS, "open": "62100", "high": "62250", "low": "62050",
        "close": "62200",
        "volume": "300.0000",                 # volCcy = base-coin volume
        "closeTime": _TS + 3_599_999,         # openTime + 1h - 1ms (Binance shape)
        "quoteVolume": "18600000.0",          # volCcyQuote
        "trades": None, "takerBuyBase": None, "takerBuyQuote": None,
    }]


def test_klines_dedup_field_is_opentime():
    assert okx_series.build_klines_spec().dedup_ts_field == "openTime"
