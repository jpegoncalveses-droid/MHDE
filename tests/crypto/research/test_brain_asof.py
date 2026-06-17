"""Tests for the brain generic AS-OF machinery: reader + primitive.

The REST present-state series (open interest, funding/premium, long/short ratios,
basis) are point-in-time values valid AS OF a timestamp, sampled sparsely (one
new value every 5–34 min, never more than one per 60s window). The as-of
primitive buckets by the venue time-key and keeps the latest (as-of) observation
per window — the raw value, NOT an OHLC summary (which would be redundant for a
single-observation window).

NO-BIAS: venue-native fields (incl. native ratios/rates like longShortRatio,
buySellRatio, basisRate) ARE raw information. The guard is "only native fields,
nothing computed" — engineered tokens (zscore/rank/norm/threshold/imbalance/…)
must never appear, but native ratio/rate names are allowed.
"""
from __future__ import annotations

from crypto.research.capture_core import store as capture_store, rest_series
from crypto.research.brain import reader, asof


_CADENCE_NS = 60 * 1_000_000_000
_T0_MS = 1_781_640_000_000
_R0 = _T0_MS * 1_000_000

# Engineered-signal tokens that must never appear in an as-of column (native
# ratio/rate are NOT here — they are raw venue fields).
_ENGINEERED = [
    "imbalance", "zscore", "z_score", "rank", "norm", "threshold", "thresh",
    "flag", "signal", "vwap", "ofi", "cvd", "skew", "pct", "percent",
]


def _oi_clean(*, recv_ns, time_ms, oi, symbol="BTCUSDT"):
    return {"recv_ts_ns": recv_ns, "symbol": symbol, "event_time_ms": time_ms,
            "open_interest": oi}


# -- generic reader --

def test_reader_casts_varchar_and_clean_names_open_interest(tmp_path):
    w = capture_store.dataset_writer(str(tmp_path), "open_interest",
                                     rest_series.OPEN_INTEREST_SCHEMA, symbol_key="s", time_key="time")
    w.append({"recv_ts_ns": 10, "s": "BTCUSDT", "openInterest": "98672.073", "time": _T0_MS + 1000})
    w.flush_all()
    (row,) = reader.read_new_asof(str(tmp_path), "open_interest",
                                  value_map={"open_interest": "openInterest"},
                                  time_col="time", symbol_col="s")
    assert set(row) == {"recv_ts_ns", "symbol", "event_time_ms", "open_interest"}
    assert row["open_interest"] == 98672.073 and isinstance(row["open_interest"], float)
    assert row["symbol"] == "BTCUSDT" and row["event_time_ms"] == _T0_MS + 1000


def test_reader_empty_string_numeric_becomes_none_basis(tmp_path):
    w = capture_store.dataset_writer(str(tmp_path), "basis",
                                     rest_series.BASIS_SCHEMA, symbol_key="pair", time_key="timestamp")
    w.append({"recv_ts_ns": 10, "pair": "BTCUSDT", "contractType": "PERPETUAL",
              "indexPrice": "63826.4", "futuresPrice": "63807.7", "basis": "-18.7",
              "basisRate": "-0.0003", "annualizedBasisRate": "", "timestamp": _T0_MS + 1000})
    w.flush_all()
    (row,) = reader.read_new_asof(str(tmp_path), "basis",
                                  value_map={"index_price": "indexPrice", "futures_price": "futuresPrice",
                                             "basis": "basis", "basis_rate": "basisRate",
                                             "annualized_basis_rate": "annualizedBasisRate"},
                                  time_col="timestamp", symbol_col="pair")
    assert row["symbol"] == "BTCUSDT"                 # symbol_col='pair'
    assert row["basis"] == -18.7 and row["basis_rate"] == -0.0003
    assert row["annualized_basis_rate"] is None        # '' -> None


def test_reader_int_map_keeps_next_funding_time_as_int(tmp_path):
    w = capture_store.dataset_writer(str(tmp_path), "premium_index",
                                     rest_series.PREMIUM_INDEX_SCHEMA, symbol_key="s", time_key="time")
    w.append({"recv_ts_ns": 10, "s": "BTCUSDT", "markPrice": "63715.4", "indexPrice": "63744.5",
              "estimatedSettlePrice": "63918.1", "lastFundingRate": "0.00006033",
              "interestRate": "0.0001", "nextFundingTime": _T0_MS + 8 * 3600_000, "time": _T0_MS + 1000})
    w.flush_all()
    (row,) = reader.read_new_asof(str(tmp_path), "premium_index",
                                  value_map={"mark_price": "markPrice", "index_price": "indexPrice",
                                             "estimated_settle_price": "estimatedSettlePrice",
                                             "last_funding_rate": "lastFundingRate", "interest_rate": "interestRate"},
                                  int_map={"next_funding_time": "nextFundingTime"},
                                  time_col="time", symbol_col="s")
    assert row["last_funding_rate"] == 0.00006033 and isinstance(row["last_funding_rate"], float)
    assert row["next_funding_time"] == _T0_MS + 8 * 3600_000 and isinstance(row["next_funding_time"], int)


def test_reader_recv_order_and_cursor(tmp_path):
    w = capture_store.dataset_writer(str(tmp_path), "open_interest",
                                     rest_series.OPEN_INTEREST_SCHEMA, symbol_key="s", time_key="time")
    for recv, t in [(300, _T0_MS + 3000), (100, _T0_MS + 1000), (200, _T0_MS + 2000)]:
        w.append({"recv_ts_ns": recv, "s": "BTCUSDT", "openInterest": "1", "time": t})
    w.flush_all()
    rows = reader.read_new_asof(str(tmp_path), "open_interest",
                               value_map={"open_interest": "openInterest"}, time_col="time", symbol_col="s")
    assert [r["recv_ts_ns"] for r in rows] == [100, 200, 300]
    after = reader.read_new_asof(str(tmp_path), "open_interest",
                                 value_map={"open_interest": "openInterest"}, time_col="time",
                                 symbol_col="s", after_recv_ts_ns=150)
    assert [r["recv_ts_ns"] for r in after] == [200, 300]


# -- generic primitive --

def test_bucket_keeps_latest_asof_value_per_window():
    rows = [_oi_clean(recv_ns=_R0 + 1, time_ms=_T0_MS + 1000, oi=100.0)]
    (snap,) = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS, value_fields=["open_interest"])
    assert snap["window_start_ns"] == _T0_MS * 1_000_000
    assert snap["window_end_ns"] == _T0_MS * 1_000_000 + _CADENCE_NS
    assert snap["asof_event_time_ms"] == _T0_MS + 1000
    assert snap["open_interest"] == 100.0
    assert snap["recv_ts_ns"] == _R0 + 1


def test_bucket_dedups_duplicate_timestamp_keeping_latest_recv():
    # Overlapping fetches deliver the same (symbol, time) twice; keep the latest
    # recv_ts_ns observation as the as-of value.
    rows = [
        _oi_clean(recv_ns=_R0 + 1, time_ms=_T0_MS + 1000, oi=100.0),
        _oi_clean(recv_ns=_R0 + 9, time_ms=_T0_MS + 1000, oi=111.0),  # newer fetch, same time
    ]
    (snap,) = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS, value_fields=["open_interest"])
    assert snap["open_interest"] == 111.0
    assert snap["recv_ts_ns"] == _R0 + 9


def test_bucket_passes_through_null_value():
    rows = [{"recv_ts_ns": _R0 + 1, "symbol": "BTCUSDT", "event_time_ms": _T0_MS + 1000,
             "basis": -18.7, "annualized_basis_rate": None}]
    (snap,) = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS,
                               value_fields=["basis", "annualized_basis_rate"])
    assert snap["basis"] == -18.7
    assert snap["annualized_basis_rate"] is None


def test_bucket_multiple_symbols_and_empty():
    rows = [
        _oi_clean(recv_ns=1, time_ms=_T0_MS + 1000, oi=1.0, symbol="BTCUSDT"),
        _oi_clean(recv_ns=2, time_ms=_T0_MS + 1000, oi=2.0, symbol="ETHUSDT"),
    ]
    snaps = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS, value_fields=["open_interest"])
    assert {s["symbol"] for s in snaps} == {"BTCUSDT", "ETHUSDT"}
    assert asof.bucket_asof([], cadence_ns=_CADENCE_NS, value_fields=["open_interest"]) == []


def test_no_bias_keys_are_provenance_plus_native_fields_only():
    rows = [{"recv_ts_ns": 1, "symbol": "BTCUSDT", "event_time_ms": _T0_MS + 1000,
             "long_account": 0.66, "short_account": 0.34, "long_short_ratio": 1.94}]
    fields = ["long_account", "short_account", "long_short_ratio"]
    (snap,) = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS, value_fields=fields)
    assert set(snap.keys()) == {"recv_ts_ns", "symbol", "window_start_ns",
                                "window_end_ns", "asof_event_time_ms"} | set(fields)
    # native ratio is allowed; no ENGINEERED token may appear
    assert "long_short_ratio" in snap
    for name in snap:
        for bad in _ENGINEERED:
            assert bad not in name.lower(), f"engineered token {bad!r} in {name!r}"
