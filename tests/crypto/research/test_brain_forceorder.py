"""Tests for the brain forceOrder source: reader + within-window primitive + schema.

forceOrder = liquidations, sparse events like trades. Apply the trades lesson:
split by side ``S`` (BUY/SELL), keep base volume and per-event notional
(price*qty, irrecoverable) separate, plus counts. A side with no liquidation in
the window is a raw 0, not null-skipped. Inverting the side mapping must fail.
"""
from __future__ import annotations

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader, forceorder, store


_CADENCE_NS = 60 * 1_000_000_000
_T0_MS = 1_781_640_000_000          # 2026-06-16 20:00:00 UTC, a 60s boundary
_R0 = _T0_MS * 1_000_000

_EXPECTED_COLUMNS = [
    "recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",
    "liq_buy_vol", "liq_sell_vol",
    "liq_buy_quote_vol", "liq_sell_quote_vol",
    "liq_buy_count", "liq_sell_count",
]
_FORBIDDEN = [
    "ratio", "imbalance", "zscore", "z_score", "rank", "norm",
    "threshold", "thresh", "flag", "signal", "vwap", "ofi", "cvd",
    "skew", "pct", "percent",
]


def _fo_capture_row(symbol="BTCUSDT", *, recv_ns, E_ms, S, q, p, T_ms=None):
    return {
        "recv_ts_ns": recv_ns, "E": E_ms, "s": symbol, "S": S, "o": "LIMIT",
        "f": "IOC", "q": q, "p": p, "ap": p, "X": "FILLED", "l": q, "z": q,
        "T": E_ms if T_ms is None else T_ms,
    }


def _write_capture(root, rows):
    w = capture_store.forceorder_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def _clean(*, recv_ns, E_ms, side, qty, price, symbol="BTCUSDT"):
    return {
        "recv_ts_ns": recv_ns, "symbol": symbol, "event_time_ms": E_ms,
        "trade_time_ms": E_ms, "side": side, "qty": qty, "price": price,
    }


# -- reader --

def test_reader_casts_varchar_and_keeps_side(tmp_path):
    _write_capture(tmp_path, [
        _fo_capture_row("HMSTRUSDT", recv_ns=10, E_ms=_T0_MS + 1000, S="SELL", q="46873", p="0.0001595"),
    ])
    (row,) = reader.read_new_forceorder(str(tmp_path))
    assert set(row) == {"recv_ts_ns", "symbol", "event_time_ms",
                        "trade_time_ms", "side", "qty", "price"}
    assert row["side"] == "SELL"
    assert row["qty"] == 46873.0 and isinstance(row["qty"], float)
    assert row["price"] == 0.0001595 and row["symbol"] == "HMSTRUSDT"


def test_reader_recv_order_cursor_and_both_sides(tmp_path):
    _write_capture(tmp_path, [
        _fo_capture_row("ROSEUSDT", recv_ns=20, E_ms=_T0_MS + 2000, S="BUY", q="4951", p="0.00736"),
        _fo_capture_row("ROSEUSDT", recv_ns=10, E_ms=_T0_MS + 1000, S="SELL", q="100", p="0.0073"),
    ])
    rows = reader.read_new_forceorder(str(tmp_path))
    assert [r["recv_ts_ns"] for r in rows] == [10, 20]
    assert {r["side"] for r in rows} == {"BUY", "SELL"}
    assert reader.read_new_forceorder(str(tmp_path), after_recv_ts_ns=15)[0]["recv_ts_ns"] == 20


# -- primitive --

def test_side_split_volume_notional_and_counts():
    rows = [
        _clean(recv_ns=_R0 + 1, E_ms=_T0_MS + 1000, side="BUY", qty=2.0, price=100.0),   # notional 200
        _clean(recv_ns=_R0 + 2, E_ms=_T0_MS + 2000, side="BUY", qty=3.0, price=50.0),    # notional 150
        _clean(recv_ns=_R0 + 3, E_ms=_T0_MS + 3000, side="SELL", qty=4.0, price=10.0),   # notional 40
    ]
    (snap,) = forceorder.bucket_forceorder(rows, cadence_ns=_CADENCE_NS)
    assert snap["liq_buy_vol"] == 5.0 and snap["liq_sell_vol"] == 4.0
    assert snap["liq_buy_quote_vol"] == 350.0 and snap["liq_sell_quote_vol"] == 40.0
    assert snap["liq_buy_count"] == 2 and snap["liq_sell_count"] == 1
    assert snap["recv_ts_ns"] == _R0 + 3
    assert snap["window_start_ns"] == _T0_MS * 1_000_000


def test_side_mapping_is_not_inverted():
    rows = [
        _clean(recv_ns=_R0 + 1, E_ms=_T0_MS + 1000, side="BUY", qty=7.0, price=1.0),
        _clean(recv_ns=_R0 + 2, E_ms=_T0_MS + 2000, side="BUY", qty=4.0, price=1.0),
        _clean(recv_ns=_R0 + 3, E_ms=_T0_MS + 3000, side="SELL", qty=5.0, price=1.0),
    ]
    (snap,) = forceorder.bucket_forceorder(rows, cadence_ns=_CADENCE_NS)
    assert snap["liq_buy_vol"] == 11.0    # 7 + 4 on the BUY side
    assert snap["liq_sell_vol"] == 5.0


def test_single_side_window_zeros_the_absent_side_not_null():
    rows = [_clean(recv_ns=_R0 + 1, E_ms=_T0_MS + 1000, side="SELL", qty=10.0, price=1.0)]
    (snap,) = forceorder.bucket_forceorder(rows, cadence_ns=_CADENCE_NS)
    # absent BUY side is a raw 0, not null/missing
    assert snap["liq_buy_vol"] == 0.0
    assert snap["liq_buy_quote_vol"] == 0.0
    assert snap["liq_buy_count"] == 0
    assert snap["liq_sell_vol"] == 10.0 and snap["liq_sell_count"] == 1


def test_multiple_symbols_and_empty():
    rows = [
        _clean(recv_ns=1, E_ms=_T0_MS + 1000, side="BUY", qty=1.0, price=1.0, symbol="BTCUSDT"),
        _clean(recv_ns=2, E_ms=_T0_MS + 1000, side="SELL", qty=1.0, price=1.0, symbol="ETHUSDT"),
    ]
    snaps = forceorder.bucket_forceorder(rows, cadence_ns=_CADENCE_NS)
    assert {s["symbol"] for s in snaps} == {"BTCUSDT", "ETHUSDT"}
    assert forceorder.bucket_forceorder([], cadence_ns=_CADENCE_NS) == []


# -- no-bias schema --

def test_no_bias_primitive_and_schema():
    rows = [_clean(recv_ns=1, E_ms=_T0_MS + 1000, side="BUY", qty=1.0, price=1.0)]
    (snap,) = forceorder.bucket_forceorder(rows, cadence_ns=_CADENCE_NS)
    assert set(snap.keys()) == set(_EXPECTED_COLUMNS)
    assert list(store.FORCEORDER_SNAPSHOT_SCHEMA.names) == _EXPECTED_COLUMNS
    for name in snap:
        low = name.lower()
        for bad in _FORBIDDEN:
            assert bad not in low, f"forbidden token {bad!r} in {name!r}"
