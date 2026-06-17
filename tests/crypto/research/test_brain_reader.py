"""Tests for the brain capture reader (read-only pyarrow consumer of aggTrade).

Fixtures are written with the REAL capture_core writer, then read back through
``brain.reader`` — proving the brain consumes capture's on-disk format exactly:
VARCHAR price/qty cast to float, clean field names, recv_ts_ns global order
(part files are disjoint but unordered by filename), the cursor filter, and
UTF-8 symbols (no ASCII regex).
"""
from __future__ import annotations

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader


_T0_MS = 1_781_640_000_000  # 2026-06-16 20:00:00 UTC, a clean window boundary


def _agg_row(symbol, *, recv_ns, T_ms, p, q, m, E_ms=None, a=1, f=1, l=1):
    """A raw capture aggTrade row (terse venue field names, p/q as strings)."""
    return {
        "recv_ts_ns": recv_ns, "e": "aggTrade",
        "E": T_ms if E_ms is None else E_ms, "a": a, "s": symbol,
        "p": p, "q": q, "f": f, "l": l, "T": T_ms, "m": m,
    }


def _write_capture(root, rows):
    w = capture_store.aggtrade_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def test_casts_varchar_price_and_qty_to_float(tmp_path):
    _write_capture(tmp_path, [
        _agg_row("0GUSDT", recv_ns=10, T_ms=_T0_MS + 1000, p="0.3000000", q="3769", m=False),
    ])
    (row,) = reader.read_new_aggtrades(str(tmp_path))
    assert row["price"] == 0.3 and isinstance(row["price"], float)
    assert row["qty"] == 3769.0 and isinstance(row["qty"], float)


def test_returns_clean_field_names(tmp_path):
    _write_capture(tmp_path, [
        _agg_row("BTCUSDT", recv_ns=10, T_ms=_T0_MS + 1000, p="100.5", q="2.0", m=False),
    ])
    (row,) = reader.read_new_aggtrades(str(tmp_path))
    assert set(row) == {
        "recv_ts_ns", "symbol", "event_time_ms", "trade_time_ms",
        "agg_id", "price", "qty", "is_buyer_maker", "taker_buy",
    }
    assert row["symbol"] == "BTCUSDT"
    assert row["trade_time_ms"] == _T0_MS + 1000


def test_taker_buy_flag_matches_is_buyer_maker(tmp_path):
    _write_capture(tmp_path, [
        _agg_row("BTCUSDT", recv_ns=10, T_ms=_T0_MS + 1000, p="1", q="1", m=False),  # taker BUY
        _agg_row("BTCUSDT", recv_ns=20, T_ms=_T0_MS + 2000, p="1", q="1", m=True),   # taker SELL
    ])
    rows = reader.read_new_aggtrades(str(tmp_path))
    by_recv = {r["recv_ts_ns"]: r for r in rows}
    assert by_recv[10]["is_buyer_maker"] is False and by_recv[10]["taker_buy"] is True
    assert by_recv[20]["is_buyer_maker"] is True and by_recv[20]["taker_buy"] is False


def test_rows_returned_in_recv_ts_ns_order_across_files(tmp_path):
    # Two separate flushes -> two part files with interleaved recv windows; the
    # reader must globally sort by recv_ts_ns (filenames are random hashes).
    _write_capture(tmp_path, [
        _agg_row("BTCUSDT", recv_ns=300, T_ms=_T0_MS + 3000, p="1", q="1", m=False),
        _agg_row("BTCUSDT", recv_ns=100, T_ms=_T0_MS + 1000, p="1", q="1", m=False),
    ])
    _write_capture(tmp_path, [
        _agg_row("BTCUSDT", recv_ns=200, T_ms=_T0_MS + 2000, p="1", q="1", m=False),
        _agg_row("BTCUSDT", recv_ns=400, T_ms=_T0_MS + 4000, p="1", q="1", m=False),
    ])
    rows = reader.read_new_aggtrades(str(tmp_path))
    assert [r["recv_ts_ns"] for r in rows] == [100, 200, 300, 400]


def test_only_returns_rows_after_cursor(tmp_path):
    _write_capture(tmp_path, [
        _agg_row("BTCUSDT", recv_ns=100, T_ms=_T0_MS + 1000, p="1", q="1", m=False),
        _agg_row("BTCUSDT", recv_ns=200, T_ms=_T0_MS + 2000, p="1", q="1", m=False),
        _agg_row("BTCUSDT", recv_ns=300, T_ms=_T0_MS + 3000, p="1", q="1", m=False),
    ])
    rows = reader.read_new_aggtrades(str(tmp_path), after_recv_ts_ns=150)
    assert [r["recv_ts_ns"] for r in rows] == [200, 300]


def test_utf8_cjk_and_digit_leading_symbols_round_trip(tmp_path):
    _write_capture(tmp_path, [
        _agg_row("我踏马来了USDT", recv_ns=10, T_ms=_T0_MS + 1000, p="1.5", q="2", m=False),
        _agg_row("0GUSDT", recv_ns=20, T_ms=_T0_MS + 2000, p="0.3", q="9", m=True),
    ])
    rows = reader.read_new_aggtrades(str(tmp_path))
    assert {r["symbol"] for r in rows} == {"我踏马来了USDT", "0GUSDT"}


def test_symbol_filter_returns_only_requested(tmp_path):
    _write_capture(tmp_path, [
        _agg_row("BTCUSDT", recv_ns=10, T_ms=_T0_MS + 1000, p="1", q="1", m=False),
        _agg_row("ETHUSDT", recv_ns=20, T_ms=_T0_MS + 2000, p="1", q="1", m=False),
    ])
    rows = reader.read_new_aggtrades(str(tmp_path), symbols=["BTCUSDT"])
    assert {r["symbol"] for r in rows} == {"BTCUSDT"}


def test_missing_capture_dir_returns_empty(tmp_path):
    assert reader.read_new_aggtrades(str(tmp_path / "nope")) == []
