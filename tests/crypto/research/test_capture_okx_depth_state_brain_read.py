"""OKX Stage C — depth_state round-trips through the SHARED store and the REAL brain reader.

An OKX-maintained book -> okx_book_state_row -> store.depth_state_writer under the OKX root ->
reader.read_new_depth_state parses it (>=20 levels/side, update_id monotonic, cursor advances).
This is the brain's only depth input; byte-identity to the Binance depth_state schema is what
lets the existing reader consume it unchanged.
"""
from __future__ import annotations

import glob
from decimal import Decimal

import pyarrow.parquet as pq

from crypto.research.brain import reader
from crypto.research.capture_core import store
from crypto.research.capture_core_okx import ws_normalize as wn
from crypto.research.capture_core_okx.book_okx import OkxBookMaintainer

_CT = Decimal("0.01")


def _synced_book(seq, base=200):
    m = OkxBookMaintainer("BTCUSDT")
    m.on_snapshot(seq,
                  bids=[[str(base - i), "3"] for i in range(22)],
                  asks=[[str(base + 1 + i), "2"] for i in range(22)])
    return m


def test_brain_reads_okx_depth_state(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    w = store.depth_state_writer(root)
    # two samples of the same symbol, advancing seqId + recv_ts_ns
    for i, seq in enumerate((500, 501)):
        row = wn.okx_book_state_row(_synced_book(seq), symbol="BTCUSDT", ct_val=_CT,
                                    recv_ns=1_700_000_000_000_000_000 + i, top_n=20)
        w.append(row)
    w.flush_all()

    # physical parquet columns are byte-identical to the Binance depth_state schema
    f = glob.glob(f"{root}/depth_state/symbol=BTCUSDT/date=*/*.parquet")[0]
    assert pq.ParquetFile(f).schema_arrow.names == store.DEPTH_STATE_SCHEMA.names

    rows = reader.read_new_depth_state(root, after_recv_ts_ns=0)
    assert len(rows) == 2
    r = rows[0]
    assert set(("recv_ts_ns", "symbol", "update_id", "bids", "asks")).issubset(r)
    assert r["symbol"] == "BTCUSDT"
    assert len(r["bids"]) == 20 and len(r["asks"]) == 20       # >=20 levels/side
    assert [rr["update_id"] for rr in rows] == [500, 501]      # monotonic
    assert r["bids"][0][1] == 0.03                             # ctVal-converted best bid qty (float)

    cursor = max(rr["recv_ts_ns"] for rr in rows)
    assert reader.read_new_depth_state(root, after_recv_ts_ns=cursor) == []   # cursor advances
