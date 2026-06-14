"""Capture-core compaction primitive round-trip tests.

The ``compact_partition`` primitive (used by the one-shot migration and reused by
ADR-038 closed-hour compaction) must round-trip every row with fields intact, a
still-monotonic ``recv_ts_ns`` cursor, and NO baked-in partition columns — the read
contract the brain Phase 1 reader is built against. (Writer flush cadence + bounded
files are covered in ``test_capture_core_write_then_compact.py``.)
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow.dataset as pads
import pyarrow.parquet as pq

from crypto.research.capture_core import maintenance
from crypto.research.capture_core import store

_DAY = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _aggtrade_row(symbol="BTCUSDT", *, recv_ns=1, a=10, p="100.5", q="2.0"):
    e_ms = _ms(_DAY)
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": e_ms, "a": a, "s": symbol,
            "p": p, "q": q, "f": 1, "l": 2, "T": e_ms, "m": False}


def _read_all(root, dataset):
    files = sorted(pathlib.Path(root, dataset).rglob("*.parquet"))
    rows = []
    for fp in files:
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return files, rows


# -- compaction round-trip preserves the read contract ------------------------

def test_compact_partition_preserves_rows_fields_and_recv_cursor(tmp_path):
    # 20 tiny size-capped parts in one symbol=/date= partition.
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for i in range(20):
        w.append(_aggtrade_row("BTCUSDT", recv_ns=1000 + i, a=i, p=f"{100 + i}.0"))
        w.flush_due()
    part_dir = pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT", "date=2026-05-29")
    assert len(list(part_dir.glob("*.parquet"))) == 20

    res = maintenance.compact_partition(str(part_dir))
    assert res.rows_before == 20 and res.rows_after == 20
    assert res.files_before == 20 and res.files_after == 1

    files, rows = _read_all(str(tmp_path), "aggTrade")
    assert len(files) == 1                                  # collapsed to one file
    # the compacted file carries ONLY the writer's physical fields — NO baked-in
    # symbol=/date= partition columns (which would collide with the path-derived
    # partition columns on a hive-dataset read).
    assert pq.read_schema(str(files[0])).names == list(store.AGGTRADE_SCHEMA.names)
    assert len(rows) == 20                                  # every row preserved
    assert {r["p"] for r in rows} == {f"{100 + i}.0" for i in range(20)}  # str-lossless
    recv = [r["recv_ts_ns"] for r in rows]
    assert recv == sorted(recv)                             # monotonic cursor preserved
    after = [r for r in rows if r["recv_ts_ns"] > 1009]     # incremental cursor read
    assert len(after) == 10 and min(r["recv_ts_ns"] for r in after) == 1010


def test_compacted_tree_reads_as_hive_dataset(tmp_path):
    # The brain Phase 1 reader opens the tree as a HIVE DATASET (symbol=/date= are
    # virtual partition columns). A compacted partition must round-trip through that
    # access pattern unchanged — the exact contract the writer fix must preserve.
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for sym in ("BTCUSDT", "ETHUSDT"):
        for i in range(6):
            w.append(_aggtrade_row(sym, recv_ns=2000 + i, a=i))
            w.flush_due()
    maintenance.migrate_compact(str(tmp_path), datasets=["aggTrade"])

    ds_dir = str(pathlib.Path(tmp_path, "aggTrade"))
    for fp in pathlib.Path(ds_dir).rglob("*.parquet"):       # physical files: no keys baked
        assert pq.read_schema(str(fp)).names == list(store.AGGTRADE_SCHEMA.names)

    table = pads.dataset(ds_dir, partitioning="hive").to_table()   # the real read pattern
    assert table.num_rows == 12                              # every row preserved
    assert set(table.column("symbol").to_pylist()) == {"BTCUSDT", "ETHUSDT"}
    assert set(table.column("date").to_pylist()) == {"2026-05-29"}
    rows = table.to_pylist()                                 # per-partition cursor intact
    for sym in ("BTCUSDT", "ETHUSDT"):
        recv = [r["recv_ts_ns"] for r in rows if r["symbol"] == sym]
        assert recv == sorted(recv) and len(recv) == 6


def test_compact_partition_single_file_is_noop(tmp_path):
    w = store.aggtrade_writer(str(tmp_path))
    w.append(_aggtrade_row("BTCUSDT"))
    w.flush_all()
    part_dir = pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT", "date=2026-05-29")
    res = maintenance.compact_partition(str(part_dir))
    assert res.files_before == 1 and res.files_after == 1
    assert res.rows_before == res.rows_after == 1
