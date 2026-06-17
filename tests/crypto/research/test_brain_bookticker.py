"""Tests for the brain bookTicker source: reader + within-window primitive + schema.

bookTicker is top-of-book, high-frequency within a window. The primitive keeps
within-window summaries of each native field (bid/ask OHLC, bid/ask qty
last/min/max/mean) PLUS the bid-ask SPREAD (a - b) summaries — a raw cross-field
observable that is IRRECOVERABLE from separate bid/ask summaries, which the
spread-irrecoverability test demonstrates. No engineered ratios.
"""
from __future__ import annotations

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader, bookticker, store


_CADENCE_NS = 60 * 1_000_000_000
_T0_MS = 1_781_640_000_000          # 2026-06-16 20:00:00 UTC, a 60s boundary
_R0 = _T0_MS * 1_000_000

_EXPECTED_COLUMNS = [
    "recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",
    "bid_open", "bid_high", "bid_low", "bid_close",
    "ask_open", "ask_high", "ask_low", "ask_close",
    "bid_qty_last", "bid_qty_min", "bid_qty_max", "bid_qty_mean",
    "ask_qty_last", "ask_qty_min", "ask_qty_max", "ask_qty_mean",
    "spread_max", "spread_min", "spread_mean", "spread_last",
    "update_count",
]
_FORBIDDEN = [
    "ratio", "imbalance", "zscore", "z_score", "rank", "norm",
    "threshold", "thresh", "flag", "signal", "vwap", "ofi", "cvd",
    "skew", "pct", "percent",
]


def _bt_capture_row(symbol="BTCUSDT", *, recv_ns, E_ms, b, B, a, A, T_ms=None, u=1):
    return {
        "recv_ts_ns": recv_ns, "e": "bookTicker", "u": u, "s": symbol,
        "b": b, "B": B, "a": a, "A": A,
        "T": E_ms if T_ms is None else T_ms, "E": E_ms,
    }


def _write_capture(root, rows):
    w = capture_store.bookticker_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def _clean(*, recv_ns, E_ms, bid, bid_qty, ask, ask_qty, symbol="BTCUSDT"):
    return {
        "recv_ts_ns": recv_ns, "symbol": symbol, "event_time_ms": E_ms,
        "transaction_time_ms": E_ms, "bid": bid, "bid_qty": bid_qty,
        "ask": ask, "ask_qty": ask_qty,
    }


# -- reader --

def test_reader_casts_varchar_and_uses_clean_names(tmp_path):
    _write_capture(tmp_path, [
        _bt_capture_row("0GUSDT", recv_ns=10, E_ms=_T0_MS + 1000,
                        b="0.3005000", B="643", a="0.3006000", A="215"),
    ])
    (row,) = reader.read_new_bookticker(str(tmp_path))
    assert set(row) == {"recv_ts_ns", "symbol", "event_time_ms",
                        "transaction_time_ms", "bid", "bid_qty", "ask", "ask_qty"}
    assert row["bid"] == 0.3005 and isinstance(row["bid"], float)
    assert row["ask"] == 0.3006 and row["bid_qty"] == 643.0 and row["ask_qty"] == 215.0
    assert row["symbol"] == "0GUSDT"


def test_reader_recv_order_cursor_and_cjk_symbol(tmp_path):
    _write_capture(tmp_path, [
        _bt_capture_row("我踏马来了USDT", recv_ns=300, E_ms=_T0_MS + 3000, b="3", B="1", a="4", A="1"),
        _bt_capture_row("我踏马来了USDT", recv_ns=100, E_ms=_T0_MS + 1000, b="1", B="1", a="2", A="1"),
        _bt_capture_row("我踏马来了USDT", recv_ns=200, E_ms=_T0_MS + 2000, b="2", B="1", a="3", A="1"),
    ])
    rows = reader.read_new_bookticker(str(tmp_path))
    assert [r["recv_ts_ns"] for r in rows] == [100, 200, 300]
    assert all(r["symbol"] == "我踏马来了USDT" for r in rows)
    after = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=150)
    assert [r["recv_ts_ns"] for r in after] == [200, 300]


# -- primitive --

def test_buckets_by_event_time_and_summarizes_fields():
    rows = [
        _clean(recv_ns=_R0 + 1, E_ms=_T0_MS + 1000, bid=100.0, bid_qty=10.0, ask=101.0, ask_qty=5.0),
        _clean(recv_ns=_R0 + 2, E_ms=_T0_MS + 2000, bid=99.0, bid_qty=20.0, ask=105.0, ask_qty=8.0),
        _clean(recv_ns=_R0 + 3, E_ms=_T0_MS + 3000, bid=102.0, bid_qty=15.0, ask=103.0, ask_qty=12.0),
    ]
    (snap,) = bookticker.bucket_bookticker(rows, cadence_ns=_CADENCE_NS)
    assert snap["window_start_ns"] == _T0_MS * 1_000_000
    assert snap["bid_open"] == 100.0 and snap["bid_close"] == 102.0
    assert snap["bid_high"] == 102.0 and snap["bid_low"] == 99.0
    assert snap["ask_open"] == 101.0 and snap["ask_close"] == 103.0
    assert snap["ask_high"] == 105.0 and snap["ask_low"] == 101.0
    assert snap["bid_qty_last"] == 15.0 and snap["bid_qty_min"] == 10.0 and snap["bid_qty_max"] == 20.0
    assert snap["bid_qty_mean"] == pytest.approx(15.0)
    assert snap["ask_qty_last"] == 12.0 and snap["ask_qty_min"] == 5.0 and snap["ask_qty_max"] == 12.0
    assert snap["ask_qty_mean"] == pytest.approx(25.0 / 3.0)
    assert snap["spread_max"] == 6.0 and snap["spread_min"] == 1.0
    assert snap["spread_mean"] == pytest.approx(8.0 / 3.0)
    assert snap["spread_last"] == 1.0  # spread of the last observation (102/103)
    assert snap["update_count"] == 3
    assert snap["recv_ts_ns"] == _R0 + 3


def test_spread_is_irrecoverable_from_bid_ask_summaries():
    # Crafted so the per-instant max spread (11) != max(ask) - min(bid) (20).
    rows = [
        _clean(recv_ns=_R0 + 1, E_ms=_T0_MS + 1000, bid=100.0, bid_qty=1.0, ask=101.0, ask_qty=1.0),
        _clean(recv_ns=_R0 + 2, E_ms=_T0_MS + 2000, bid=90.0, bid_qty=1.0, ask=95.0, ask_qty=1.0),
        _clean(recv_ns=_R0 + 3, E_ms=_T0_MS + 3000, bid=99.0, bid_qty=1.0, ask=110.0, ask_qty=1.0),
    ]
    (snap,) = bookticker.bucket_bookticker(rows, cadence_ns=_CADENCE_NS)
    assert snap["spread_max"] == 11.0
    recoverable_guess = snap["ask_high"] - snap["bid_low"]   # max(ask) - min(bid) = 20
    assert recoverable_guess == 20.0
    assert snap["spread_max"] != recoverable_guess  # spread is genuinely irrecoverable


def test_multiple_symbols_and_empty():
    rows = [
        _clean(recv_ns=1, E_ms=_T0_MS + 1000, bid=1.0, bid_qty=1.0, ask=2.0, ask_qty=1.0, symbol="BTCUSDT"),
        _clean(recv_ns=2, E_ms=_T0_MS + 1000, bid=1.0, bid_qty=1.0, ask=2.0, ask_qty=1.0, symbol="ETHUSDT"),
    ]
    snaps = bookticker.bucket_bookticker(rows, cadence_ns=_CADENCE_NS)
    assert {s["symbol"] for s in snaps} == {"BTCUSDT", "ETHUSDT"}
    assert bookticker.bucket_bookticker([], cadence_ns=_CADENCE_NS) == []


# -- no-bias schema --

def test_no_bias_primitive_and_schema():
    rows = [_clean(recv_ns=1, E_ms=_T0_MS + 1000, bid=1.0, bid_qty=1.0, ask=2.0, ask_qty=1.0)]
    (snap,) = bookticker.bucket_bookticker(rows, cadence_ns=_CADENCE_NS)
    assert set(snap.keys()) == set(_EXPECTED_COLUMNS)
    assert list(store.BOOKTICKER_SNAPSHOT_SCHEMA.names) == _EXPECTED_COLUMNS
    for name in snap:
        low = name.lower()
        for bad in _FORBIDDEN:
            assert bad not in low, f"forbidden token {bad!r} in {name!r}"


def test_no_bias_scan_would_reject_a_ratio_column():
    # Adversarial: a composed bid/ask ratio name must be caught by the scan.
    for bad_name in ["bid_ask_ratio", "depth_imbalance", "spread_zscore"]:
        assert any(b in bad_name for b in _FORBIDDEN)
    for ok in ["spread_max", "bid_open", "ask_qty_mean"]:
        assert not any(b in ok for b in _FORBIDDEN)
