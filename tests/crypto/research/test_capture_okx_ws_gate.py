"""OKX Stage B WS — offline reader-parity gate.

Writes OKX-normalized rows via the SHARED store writers under an OKX scratch root, then reads
them back through the ACTUAL brain WS readers (the same functions the tick loop uses). Proves:
fragments parse (non-empty, correct types), per-source cursors advance, D1 (markPrice P=markPx
does not crash the reader), and the forceorder plumbing-not-liveness rule (a synthetic/replayed
liquidation row is returned + advances the cursor, with zero live liquidations).

This is the offline half of the Stage B gate; the live-WS half runs the daemon against OKX.
"""
from __future__ import annotations

from decimal import Decimal

from crypto.research.capture_core import store
from crypto.research.brain import reader
from crypto.research.capture_core_okx import ws_normalize as wn

_RECV_NS = 1_700_000_000_123_456_789
_TS = "1700000000123"
_CT = Decimal("0.01")


def _write(root, writer_fn, rows):
    w = writer_fn(root)
    for r in rows:
        w.append(r)
    w.flush_all()


def test_forceorder_gate_synthetic_liquidation_parses_and_cursor_advances(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    frame = {"instId": "BTC-USDT-SWAP",
             "details": [{"side": "sell", "posSide": "long", "bkPx": "41000", "sz": "5", "ts": _TS}]}
    rows = wn.okx_liquidation_rows(frame, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS)
    _write(root, store.forceorder_writer, rows)

    out = reader.read_new_forceorder(root, after_recv_ts_ns=0)
    assert len(out) == 1                                   # replayed liq row is returned
    r = out[0]
    assert r["symbol"] == "BTCUSDT" and r["side"] == "SELL"
    assert r["qty"] == 0.05 and r["price"] == 41000.0      # correct types (floats)

    cursor = max(o["recv_ts_ns"] for o in out)
    assert cursor == _RECV_NS
    assert reader.read_new_forceorder(root, after_recv_ts_ns=cursor) == []   # cursor advanced


def test_markprice_gate_settle_proxy_does_not_crash_reader(tmp_path):
    # D1: P == markPx (not '') so read_new_markprice's float(r['P']) never raises.
    root = str(tmp_path / "capture_core_okx")
    row = wn.okx_markprice_merge_row(
        symbol="BTCUSDT", mark={"markPx": "42310.6", "ts": _TS}, index={"idxPx": "42309.1"},
        funding={"fundingRate": "0.00012", "fundingTime": "1700000800000"}, recv_ns=_RECV_NS)
    _write(root, store.markprice_writer, [row])

    out = reader.read_new_markprice(root, after_recv_ts_ns=0)   # must not raise
    assert len(out) == 1
    r = out[0]
    assert r["mark"] == 42310.6 and r["settle"] == 42310.6      # settle proxy == mark
    assert r["index"] == 42309.1 and r["next_funding_time_ms"] == 1700000800000


def test_aggtrades_gate_parses_and_cursor_advances(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    d = {"instId": "BTC-USDT-SWAP", "tradeId": "42", "px": "42000", "sz": "3",
         "side": "sell", "ts": _TS, "count": "1"}
    _write(root, store.aggtrade_writer,
           [wn.okx_trades_row(d, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS)])

    out = reader.read_new_aggtrades(root, after_recv_ts_ns=0)
    assert len(out) == 1
    assert out[0]["symbol"] == "BTCUSDT" and out[0]["qty"] == 0.03 and out[0]["is_buyer_maker"] is True
    assert reader.read_new_aggtrades(root, after_recv_ts_ns=out[0]["recv_ts_ns"]) == []


def test_bookticker_gate_parses_and_cursor_advances(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    d = {"asks": [["42001", "5", "0", "1"]], "bids": [["42000", "1", "0", "1"]],
         "ts": _TS, "seqId": 7}
    _write(root, store.bookticker_writer,
           [wn.okx_bbo_row(d, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS)])

    out = reader.read_new_bookticker(root, after_recv_ts_ns=0)
    assert len(out) == 1
    assert out[0]["bid_qty"] == 0.01 and out[0]["ask_qty"] == 0.05 and out[0]["bid"] == 42000.0
    assert reader.read_new_bookticker(root, after_recv_ts_ns=out[0]["recv_ts_ns"]) == []
