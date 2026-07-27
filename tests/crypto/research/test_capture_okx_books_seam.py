"""OKX Stage C — books routing through the venue seam + depth_state sampling.

`books` reuses the Stage B seam with ZERO ws_client change: decode_envelope classifies it as a
data frame keyed on arg.instId; the collector's on_frame 'books' branch feeds a per-instId
maintainer (snapshot vs update detected from prevSeqId==-1 inside the element) and, only when
persist_raw_depth is set, writes the raw depth tape. A 5s sample loop writes depth_state from
synced books. Maintainers are dropped (unsynced) on a socket gap.
"""
from __future__ import annotations

import json
from decimal import Decimal

from crypto.research.capture_core_okx import ws_client as wc
from crypto.research.capture_core_okx import ws_collector as col
from crypto.research.capture_core_okx.symbols import SymbolMap

_UNIVERSE = ["BTC-USDT-SWAP"]
_CTVAL = {"BTC-USDT-SWAP": Decimal("0.01")}


class _FakeWriter:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(row)

    def flush_all(self):
        pass

    def flush_due(self):
        pass


def _books_collector(persist=False):
    writers = {k: _FakeWriter() for k in ("depth", "depth_state", "_gaps")}
    c = col.OkxWsCollector(symbol_map=SymbolMap(_UNIVERSE), ctval_table=_CTVAL,
                           writers=writers, persist_raw_depth=persist, depth_top_n=20)
    return c, writers


def _snapshot(seq=10, n=22):
    return {"bids": [[str(200 - i), "3", "0", "1"] for i in range(n)],
            "asks": [[str(201 + i), "2", "0", "1"] for i in range(n)],
            "ts": "1700000000123", "seqId": seq, "prevSeqId": -1}


def test_books_frame_decodes_through_the_seam_unchanged():
    f = wc.decode_envelope(json.dumps(
        {"arg": {"channel": "books", "instId": "BTC-USDT-SWAP"}, "data": [_snapshot()]}))
    assert f.kind == "data" and f.channel == "books" and f.inst_id == "BTC-USDT-SWAP"


def test_on_frame_books_feeds_maintainer_snapshot_then_update():
    c, _ = _books_collector()
    c.on_frame("books", "BTC-USDT-SWAP", [_snapshot(seq=10)], 999)
    m = c._books["BTC-USDT-SWAP"]
    assert m.synced is True and m.last_seq_id == 10
    c.on_frame("books", "BTC-USDT-SWAP",
               [{"bids": [["200", "9", "0", "1"]], "asks": [], "ts": "2", "seqId": 11, "prevSeqId": 10}], 1000)
    assert m.last_seq_id == 11


def test_depth_state_sample_writes_top20_row_from_synced_book():
    c, w = _books_collector()
    c.on_frame("books", "BTC-USDT-SWAP", [_snapshot(seq=10)], 999)
    c.sample_depth_state(12345)
    assert len(w["depth_state"].rows) == 1
    r = w["depth_state"].rows[0]
    assert r["update_id"] == 10 and r["valid"] is True
    assert len(r["b"]) == 20 and len(r["a"]) == 20
    assert r["b"][0] == ["200", "0.03"]        # ctVal-converted best bid


def test_unsynced_book_is_not_sampled():
    c, w = _books_collector()
    c.on_frame("books", "BTC-USDT-SWAP",       # update before any snapshot -> never synced
               [{"bids": [["1", "1", "0", "1"]], "asks": [], "ts": "1", "seqId": 5, "prevSeqId": 4}], 1)
    c.sample_depth_state(999)
    assert w["depth_state"].rows == []


def test_persist_raw_depth_flag_gates_the_tape():
    on, w_on = _books_collector(persist=True)
    off, w_off = _books_collector(persist=False)
    for c in (on, off):
        c.on_frame("books", "BTC-USDT-SWAP", [_snapshot(seq=10)], 1)
    assert len(w_on["depth"].rows) == 1        # raw tape written when enabled
    assert w_off["depth"].rows == []           # maintainer-only default: no raw tape


def test_maintainer_dropped_on_socket_gap():
    c, _ = _books_collector()
    c.on_frame("books", "BTC-USDT-SWAP", [_snapshot(seq=10)], 1)
    assert c._books["BTC-USDT-SWAP"].synced is True
    c._on_socket_gap("socket_break")           # never apply across a gap
    assert c._books["BTC-USDT-SWAP"].synced is False


def test_non_universe_book_frame_dropped():
    c, w = _books_collector()
    c.on_frame("books", "DOGE-USDT-SWAP", [_snapshot(seq=10)], 1)   # not in universe/ctval
    assert "DOGE-USDT-SWAP" not in c._books and w["depth_state"].rows == []
