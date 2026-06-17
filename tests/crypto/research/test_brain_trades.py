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


# NO-BIAS line is INFORMATION vs INTERPRETATION (not "presence of a product"):
#   * ALLOWED  — raw per-event quantities that cannot be reconstructed from the
#     stored summaries (e.g. notional Σ(price*qty) per side, irrecoverable from
#     Σqty + price OHLC), and within-window single-field summaries.
#   * FORBIDDEN (Phase 3) — engineered signals computed OVER the window
#     summaries: ratios/imbalance, normalized/rank/z-score, thresholds, selection.
#
# The canonical, hand-maintained whitelist. Hardcoded here (NOT imported from the
# module under test) so the assertion is an independent guard: adding any
# interpretation column to the primitive breaks exact equality.
_EXPECTED_COLUMNS = {
    # provenance / immutable bounds (not features)
    "recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",
    # raw separable primitives (per-event raw quantities + single-field summaries,
    # taker split kept SEPARATE)
    "taker_buy_vol", "taker_sell_vol",
    "taker_buy_quote_vol", "taker_sell_quote_vol",  # raw notional, irrecoverable downstream
    "buy_trade_count", "sell_trade_count", "trade_count",
    "price_open", "price_high", "price_low", "price_close",
    "qty_sum", "qty_max", "qty_mean",
}

# Substrings that betray an engineered signal computed OVER the window summaries
# (interpretation). Raw quantities like notional/quote-vol are NOT here — they
# carry information, not a hypothesis.
_FORBIDDEN_SUBSTRINGS = [
    "ratio", "imbalance", "zscore", "z_score", "rank", "norm",
    "threshold", "thresh", "flag", "signal", "vwap", "ofi", "cvd",
    "skew", "pct", "percent",
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


def test_taker_quote_vol_is_per_event_notional_split_by_side():
    # Notional is summed per-event (price*qty per trade) within the window and
    # split by taker side. It is RAW: it cannot be reconstructed from the stored
    # qty/price summaries (different per-trade price*qty pairings share the same
    # qty_sum and price OHLC but differ in notional).
    rows = [
        _trade(recv_ns=1, T_ms=_T0_MS + 1_000, price=100.0, qty=2.0, m=False),  # BUY  notional 200
        _trade(recv_ns=2, T_ms=_T0_MS + 2_000, price=50.0, qty=3.0, m=False),   # BUY  notional 150
        _trade(recv_ns=3, T_ms=_T0_MS + 3_000, price=10.0, qty=4.0, m=True),    # SELL notional 40
    ]
    (snap,) = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    assert snap["taker_buy_quote_vol"] == 350.0   # 200 + 150 on the m=False (BUY) side
    assert snap["taker_sell_quote_vol"] == 40.0   # 40 on the m=True (SELL) side


def test_no_bias_allows_raw_primitives_only():
    rows = [
        _trade(recv_ns=1, T_ms=_T0_MS + 1_000, price=100.0, qty=2.0, m=False),
        _trade(recv_ns=2, T_ms=_T0_MS + 2_000, price=101.0, qty=3.0, m=True),
    ]
    (snap,) = trades.bucket_trades(rows, cadence_ns=_CADENCE_NS)
    # Exact whitelist: any interpretation key OR any missing key fails.
    assert set(snap.keys()) == _EXPECTED_COLUMNS
    # No column name may betray an engineered signal over the window summaries.
    for name in snap:
        low = name.lower()
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad not in low, f"forbidden interpretation token {bad!r} in column {name!r}"


def test_no_bias_scan_catches_interpretation_columns():
    # Adversarial: the forbidden scan MUST reject engineered signals over the
    # summaries, and MUST NOT reject the new raw notional columns.
    interpretation = [
        "taker_imbalance", "buy_sell_ratio", "qty_zscore", "price_rank",
        "vol_threshold", "ret_vwap", "norm_qty", "flow_skew", "buy_pct",
    ]
    for name in interpretation:
        low = name.lower()
        assert any(bad in low for bad in _FORBIDDEN_SUBSTRINGS), f"{name} should be rejected"
    for name in ["taker_buy_quote_vol", "taker_sell_quote_vol", "qty_sum", "price_open"]:
        low = name.lower()
        assert not any(bad in low for bad in _FORBIDDEN_SUBSTRINGS), f"{name} should pass"
