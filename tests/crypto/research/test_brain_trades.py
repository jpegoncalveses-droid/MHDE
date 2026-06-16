"""Tests for the brain trades primitive (pure: bucket + within-window raw summaries).

The NO-BIAS guardrail is enforced here: the snapshot must hold ONLY raw,
separable, single-field within-window primitives (plus the mandated taker
buy/sell split) and immutable provenance/bounds — and NONE of the forbidden
composed/normalized/threshold columns. The taker-side mapping test guards the
classic ``isBuyerMaker`` inversion footgun.
"""
from __future__ import annotations

from crypto.research.brain import trades


#: 60s base cadence, in ns.
_CADENCE_NS = 60 * 1_000_000_000
#: A clean 60s-window boundary in ms (1_781_640_000_000 == 60000 * 29_694_000).
_T0_MS = 1_781_640_000_000


def _trade(*, recv_ns, T_ms, price, qty, m, symbol="BTCUSDT"):
    """A clean upstream trade dict, as produced by brain.reader."""
    return {
        "recv_ts_ns": recv_ns,
        "symbol": symbol,
        "trade_time_ms": T_ms,
        "price": price,
        "qty": qty,
        "is_buyer_maker": m,
    }


# The canonical, hand-maintained whitelist. Hardcoded here (NOT imported from
# the module under test) so the assertion is an independent guard: adding any
# composed/normalized/threshold column to the primitive breaks exact equality.
_EXPECTED_COLUMNS = {
    # provenance / immutable bounds (not features)
    "recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",
    # raw separable primitives (single-field within-window + mandated taker split)
    "taker_buy_vol", "taker_sell_vol",
    "buy_trade_count", "sell_trade_count", "trade_count",
    "price_open", "price_high", "price_low", "price_close",
    "qty_sum", "qty_max", "qty_mean",
}

# Substrings that betray a composed/normalized/threshold/selected feature.
_FORBIDDEN_SUBSTRINGS = [
    "ratio", "imbalance", "zscore", "z_score", "rank", "norm",
    "threshold", "thresh", "flag", "signal", "vwap", "ofi", "cvd",
    "skew", "pct", "percent", "quote", "notional", "delta", "net",
]


def test_buckets_trades_into_base_cadence_windows_by_event_time():
    rows = [
        _trade(recv_ns=100, T_ms=_T0_MS + 1_000, price=100.0, qty=2.0, m=False),
        _trade(recv_ns=200, T_ms=_T0_MS + 2_000, price=101.0, qty=3.0, m=True),
        _trade(recv_ns=400, T_ms=_T0_MS + 61_000, price=102.0, qty=5.0, m=True),
    ]
    snaps = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    by_start = {s["window_start_ns"]: s for s in snaps}
    w0 = (_T0_MS) * 1_000_000
    w1 = (_T0_MS + 60_000) * 1_000_000
    assert set(by_start) == {w0, w1}
    assert by_start[w0]["window_end_ns"] == w0 + _CADENCE_NS
    assert by_start[w0]["trade_count"] == 2
    assert by_start[w1]["trade_count"] == 1


def test_taker_side_mapping_is_not_inverted():
    # m=False -> taker BUY ; m=True -> taker SELL. The classic footgun is the
    # inversion. Distinct qty per side proves the mapping direction.
    rows = [
        _trade(recv_ns=1, T_ms=_T0_MS + 1_000, price=100.0, qty=7.0, m=False),  # BUY
        _trade(recv_ns=2, T_ms=_T0_MS + 2_000, price=100.0, qty=11.0, m=True),  # SELL
        _trade(recv_ns=3, T_ms=_T0_MS + 3_000, price=100.0, qty=4.0, m=False),  # BUY
    ]
    (snap,) = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    assert snap["taker_buy_vol"] == 11.0   # 7 + 4 on the m=False (BUY) side
    assert snap["taker_sell_vol"] == 11.0  # 11 on the m=True (SELL) side
    assert snap["buy_trade_count"] == 2
    assert snap["sell_trade_count"] == 1
    # Guard against accidental symmetry hiding an inversion: make the volumes
    # unambiguous.
    rows2 = [
        _trade(recv_ns=1, T_ms=_T0_MS + 1_000, price=100.0, qty=2.0, m=False),  # BUY
        _trade(recv_ns=2, T_ms=_T0_MS + 2_000, price=100.0, qty=9.0, m=True),   # SELL
    ]
    (snap2,) = trades.bucket_trades(rows2, cadence_ns=_CADENCE_NS)
    assert snap2["taker_buy_vol"] == 2.0
    assert snap2["taker_sell_vol"] == 9.0


def test_price_ohlc_follows_event_order_within_window():
    rows = [
        _trade(recv_ns=10, T_ms=_T0_MS + 1_000, price=100.0, qty=1.0, m=False),
        _trade(recv_ns=20, T_ms=_T0_MS + 2_000, price=105.0, qty=1.0, m=False),
        _trade(recv_ns=30, T_ms=_T0_MS + 3_000, price=98.0, qty=1.0, m=False),
        _trade(recv_ns=40, T_ms=_T0_MS + 4_000, price=101.0, qty=1.0, m=False),
    ]
    (snap,) = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    assert snap["price_open"] == 100.0   # first by event time
    assert snap["price_close"] == 101.0  # last by event time
    assert snap["price_high"] == 105.0
    assert snap["price_low"] == 98.0


def test_qty_aggregates_and_recv_provenance():
    rows = [
        _trade(recv_ns=10, T_ms=_T0_MS + 1_000, price=100.0, qty=2.0, m=False),
        _trade(recv_ns=55, T_ms=_T0_MS + 2_000, price=100.0, qty=6.0, m=True),
        _trade(recv_ns=33, T_ms=_T0_MS + 3_000, price=100.0, qty=4.0, m=False),
    ]
    (snap,) = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    assert snap["qty_sum"] == 12.0
    assert snap["qty_max"] == 6.0
    assert snap["qty_mean"] == 4.0
    assert snap["trade_count"] == 3
    assert snap["recv_ts_ns"] == 55  # provenance == max recv_ts_ns in window


def test_multiple_symbols_split_into_separate_snapshots():
    rows = [
        _trade(recv_ns=1, T_ms=_T0_MS + 1_000, price=100.0, qty=1.0, m=False, symbol="BTCUSDT"),
        _trade(recv_ns=2, T_ms=_T0_MS + 1_000, price=50.0, qty=1.0, m=False, symbol="ETHUSDT"),
    ]
    snaps = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    assert {s["symbol"] for s in snaps} == {"BTCUSDT", "ETHUSDT"}
    assert len(snaps) == 2


def test_empty_input_is_empty_output():
    assert trades.bucket_trades([], cadence_ns=_CADENCE_NS) == []


def test_no_bias_snapshot_holds_only_raw_separable_primitives():
    rows = [
        _trade(recv_ns=1, T_ms=_T0_MS + 1_000, price=100.0, qty=2.0, m=False),
        _trade(recv_ns=2, T_ms=_T0_MS + 2_000, price=101.0, qty=3.0, m=True),
    ]
    (snap,) = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    # Exact whitelist: any extra (composed) key OR any missing key fails.
    assert set(snap.keys()) == _EXPECTED_COLUMNS
    # No column name may betray a ratio/normalized/threshold/selected feature.
    for name in snap:
        low = name.lower()
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad not in low, f"forbidden composed/normalized token {bad!r} in column {name!r}"
