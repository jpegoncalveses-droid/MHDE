"""Columnar reader (option B) — ``store.read_snapshots_columnar`` returns the SAME selection
as ``read_snapshots`` but as a pyarrow Table (columnar, ~10-30x smaller than the list-of-dicts),
reusing the identical fragment-discovery / date-prune / mtime-skip / row-window / race-retry
logic. ``read_snapshots`` itself must stay byte-identical (the live tick loop shares it).
"""
from __future__ import annotations

import pyarrow.compute as pc

from crypto.research.brain import store

_W = 60_000_000_000


def _trade_snap(sym, i, buy, sell):
    ws = i * _W
    return {"recv_ts_ns": ws + _W, "symbol": sym, "window_start_ns": ws, "window_end_ns": ws + _W,
            "taker_buy_vol": float(buy), "taker_sell_vol": float(sell),
            "taker_buy_quote_vol": float(buy) * 10, "taker_sell_quote_vol": float(sell) * 10,
            "buy_trade_count": buy, "sell_trade_count": sell, "trade_count": buy + sell,
            "price_open": 100.0 + i, "price_high": 101.0 + i, "price_low": 99.0 + i,
            "price_close": 100.5 + i, "qty_sum": 1.0, "qty_max": 1.0, "qty_mean": 1.0}


def _write(root, snaps):
    store.write_snapshots(str(root), "trades", store.TRADES_SNAPSHOT_SCHEMA, snaps)


def _rows_from_table(tbl):
    return tbl.to_pylist()


def test_columnar_read_matches_read_snapshots_full(tmp_path):
    snaps = [_trade_snap("BTCUSDT", i, i + 1, i + 2) for i in range(5)] + \
            [_trade_snap("ETHUSDT", i, i + 3, i + 4) for i in range(5)]
    _write(tmp_path, snaps)
    scalar = store.read_snapshots(str(tmp_path), "trades")
    tbl = store.read_snapshots_columnar(str(tmp_path), "trades")
    # same rows, same order, same values (the table decodes to the identical dicts)
    assert _rows_from_table(tbl) == scalar


def test_columnar_read_honours_column_projection(tmp_path):
    _write(tmp_path, [_trade_snap("BTCUSDT", i, i + 1, i + 2) for i in range(4)])
    cols = ["symbol", "window_start_ns", "taker_buy_vol"]
    tbl = store.read_snapshots_columnar(str(tmp_path), "trades", columns=cols)
    assert tbl.column_names == cols
    assert tbl.num_rows == 4


def test_columnar_read_honours_window_floor_and_row_filter(tmp_path):
    _write(tmp_path, [_trade_snap("BTCUSDT", i, i + 1, i + 2) for i in range(10)])
    floor = 5 * _W                                   # keep window_end_ns >= 5*_W  (windows i>=4)
    scalar = store.read_snapshots(str(tmp_path), "trades", window_end_floor_ns=floor,
                                  row_filter=pc.field("taker_buy_vol") >= 7.0)
    tbl = store.read_snapshots_columnar(str(tmp_path), "trades", window_end_floor_ns=floor,
                                        row_filter=pc.field("taker_buy_vol") >= 7.0)
    assert _rows_from_table(tbl) == scalar


def test_read_snapshots_still_returns_list_of_dicts(tmp_path):
    # the tick-loop-shared read path is unchanged: still a list[dict], not a Table.
    _write(tmp_path, [_trade_snap("BTCUSDT", i, i + 1, i + 2) for i in range(3)])
    out = store.read_snapshots(str(tmp_path), "trades")
    assert isinstance(out, list) and all(isinstance(r, dict) for r in out)
    assert out[0]["taker_buy_vol"] == 1.0 and out[0]["symbol"] == "BTCUSDT"
