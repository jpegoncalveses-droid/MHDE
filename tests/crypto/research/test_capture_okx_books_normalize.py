"""OKX Stage C — books normalizers, byte-identical to the Binance depth schemas.

okx_books_row maps a raw OKX `books` frame element to DEPTH_SCHEMA (the raw ladder tape);
okx_book_state_row maps the maintained book to DEPTH_STATE_SCHEMA (top-N, the brain's only need).
OKX level shape is [px, sz_contracts, liqOrders(deprecated), numOrders]; the trailing two are
dropped and sizes are contracts->coin via ctVal.
"""
from __future__ import annotations

from decimal import Decimal

import pyarrow as pa

from crypto.research.capture_core import store
from crypto.research.capture_core_okx import ws_normalize as wn
from crypto.research.capture_core_okx.book_okx import OkxBookMaintainer

_RECV_NS = 1_700_000_000_123_456_789
_TS = "1700000000123"
_CT = Decimal("0.01")


def test_okx_books_row_matches_depth_schema():
    d = {"bids": [["100.0", "3", "0", "2"], ["99.0", "0", "0", "1"]],
         "asks": [["101.0", "2", "0", "1"]], "ts": _TS, "seqId": 100, "prevSeqId": -1}
    row = wn.okx_books_row(d, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS)

    pa.Table.from_pylist([row], schema=store.DEPTH_SCHEMA)     # byte-identical cast succeeds
    assert set(row) == set(store.DEPTH_SCHEMA.names)
    assert row["pu"] == -1 and row["U"] == 100 and row["u"] == 100   # snapshot: seqId->U/u, prevSeqId->pu
    assert row["E"] == 1700000000123 and row["T"] == 1700000000123
    assert row["s"] == "BTCUSDT" and row["e"] == "depthUpdate"
    assert row["b"] == [["100.0", "0.03"], ["99.0", "0.00"]]   # sz*ctVal, liq/num dropped, zero kept
    assert row["a"] == [["101.0", "0.02"]]


def test_okx_book_state_row_matches_depth_state_schema():
    m = OkxBookMaintainer("BTCUSDT")
    m.on_snapshot(500,
                  bids=[[str(200 - i), "3"] for i in range(22)],
                  asks=[[str(201 + i), "2"] for i in range(22)])
    row = wn.okx_book_state_row(m, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS, top_n=20)

    tbl = pa.Table.from_pylist([row], schema=store.DEPTH_STATE_SCHEMA)
    assert tbl.schema == store.DEPTH_STATE_SCHEMA               # pyarrow schema equality
    assert set(row) == set(store.DEPTH_STATE_SCHEMA.names)
    assert row["update_id"] == 500 and row["valid"] is True
    assert len(row["b"]) == 20 and len(row["a"]) == 20         # top-20 each side
    assert row["b"][0] == ["200", "0.03"]                      # best bid, ctVal-converted
    assert row["a"][0] == ["201", "0.02"]                      # best ask


def test_books_level_sizes_contracts_to_coin():
    from crypto.research.capture_core_okx.ctval import contracts_to_coin
    ct = Decimal("10000000")                                   # PEPE multiplier
    d = {"bids": [["0.1", "4", "0", "1"], ["0.09", "7", "0", "1"]],
         "asks": [["0.11", "2", "0", "1"]], "ts": _TS, "seqId": 1, "prevSeqId": -1}
    row = wn.okx_books_row(d, symbol="PEPEUSDT", ct_val=ct, recv_ns=_RECV_NS)
    assert [lvl[1] for lvl in row["b"]] == [contracts_to_coin("4", ct), contracts_to_coin("7", ct)]
    assert row["b"] == [["0.1", "40000000"], ["0.09", "70000000"]]   # exact, no sci-notation
