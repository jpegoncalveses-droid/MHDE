"""Chunked, subprocess-bounded compaction — the timer's permanent run model.

The closed-hour merge-phase accrues anon memory ~per-merge (pyarrow pool retention), so a
single process compacting a whole hour (~thousands of merges) sits ON the 1G cap, and a
multi-hour backlog OOMs. The fix bounds memory by RUN SIZE: process partitions in chunks
of ~`merges_per_chunk`, each chunk in its OWN subprocess, so process exit resets the pool
between chunks (in-process release doesn't reliably). Same model for the steady-state hour
AND the backlog — a downtime catch-up is just more chunks, each bounded.

`_compact_chunk` is the shared per-chunk unit (compacts partitions until the merge budget,
persists that chunk's gaps); the worker module runs it in a subprocess; the driver loops,
advancing past the partitions each chunk completed. Tests drive it with an in-process
runner (fast) plus one real-subprocess test for the isolation path.
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timezone

from crypto.research.capture_core import store as capture_store
from crypto.research.capture_core import maintenance

_E_MS = int(datetime(2026, 5, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
_HOUR = 1_000_000                                       # an arbitrary closed clock-hour
_NOW = (_HOUR + 1) * 3600 + 10_000                      # well past the hour + grace


def _agg_row(symbol, *, recv_ns):
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": _E_MS, "a": 1, "s": symbol,
            "p": "100.5", "q": "2.0", "f": 1, "l": 2, "T": _E_MS, "m": False}


def _mk_partition(root, symbol, n=3):
    """A partition with n part files all in ONE closed mtime-hour (-> 1 merge)."""
    w = capture_store.aggtrade_writer(str(root), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for i in range(n):
        w.append(_agg_row(symbol, recv_ns=1000 + i))
        w.flush_due()
    w.flush_all()
    pd = pathlib.Path(root, "aggTrade", f"symbol={symbol}", "date=2026-05-01")
    mt = _HOUR * 3600 + 1
    for f in pd.glob("part-*.parquet"):
        os.utime(f, (mt, mt))
    return pd


def _make_universe(root, n_partitions):
    for k in range(n_partitions):
        _mk_partition(root, f"SYM{k:03d}USDT")


def _inprocess_runner(calls):
    """A chunk_runner that runs `_compact_chunk` IN-PROCESS (no subprocess) and records
    the (remaining-paths, budget) it was handed — for asserting the chunking."""
    def _run(root, paths, budget, now_ts, grace_s):
        calls.append((len(paths), budget))
        return maintenance._compact_chunk(root, list(paths), budget, now_ts, grace_s)
    return _run


def _compact_h_files(root):
    return sorted(pathlib.Path(root, "aggTrade").rglob("compact-h*.parquet"))


# -- the chunk unit ------------------------------------------------------------

def test_compact_chunk_stops_at_merge_budget(tmp_path):
    _make_universe(tmp_path, 5)                          # 5 partitions, 1 merge each
    paths = [str(p) for p in pathlib.Path(tmp_path, "aggTrade").glob("symbol=*/date=*")]
    res = maintenance._compact_chunk(str(tmp_path), sorted(paths), 2, _NOW, 300.0)
    assert res["completed"] == 2 and res["merges"] == 2   # stopped after the budget


# -- the driver: chunked, covers everything, bounded per chunk -----------------

def test_chunked_compacts_all_partitions_in_bounded_chunks(tmp_path):
    _make_universe(tmp_path, 5)
    calls = []
    rep = maintenance.compact_firehose_chunked(
        str(tmp_path), datasets=["aggTrade"], merges_per_chunk=2,
        now_ts=_NOW, chunk_runner=_inprocess_runner(calls))
    assert len(_compact_h_files(tmp_path)) == 5           # every partition compacted once
    assert len(calls) == 3                                # 5 merges / budget 2 -> 3 chunks
    assert rep.hours_compacted == 5


def test_chunked_advances_by_completed_no_skip_no_dup(tmp_path):
    _make_universe(tmp_path, 7)
    rep = maintenance.compact_firehose_chunked(
        str(tmp_path), datasets=["aggTrade"], merges_per_chunk=3,
        now_ts=_NOW, chunk_runner=_inprocess_runner([]))
    # exactly one compact-h per partition (none skipped, none double-compacted)
    assert len(_compact_h_files(tmp_path)) == 7 and rep.partitions_scanned == 7
    assert not list(pathlib.Path(tmp_path, "aggTrade").rglob("part-*.parquet"))


def test_chunked_result_matches_single_run(tmp_path):
    a, b = tmp_path / "chunked", tmp_path / "single"
    _make_universe(a, 4); _make_universe(b, 4)
    maintenance.compact_firehose_chunked(str(a), datasets=["aggTrade"], merges_per_chunk=1,
                                         now_ts=_NOW, chunk_runner=_inprocess_runner([]))
    maintenance.compact_firehose_closed_hours(str(b), datasets=["aggTrade"], now_ts=_NOW)
    def rows(root):
        import pyarrow.parquet as pq
        return sorted(r["recv_ts_ns"] for f in _compact_h_files(root)
                      for r in pq.read_table(str(f)).to_pylist())
    assert rows(a) == rows(b) and len(_compact_h_files(a)) == len(_compact_h_files(b))


def test_chunk_failure_advances_and_does_not_infinite_loop(tmp_path):
    _make_universe(tmp_path, 3)
    def _failing_runner(root, paths, budget, now_ts, grace_s):
        return {"completed": 0, "merges": 0, "files_before": 0, "files_after": 0, "gaps": 0}
    # completed=0 every time would loop forever; the driver must still advance by >=1.
    rep = maintenance.compact_firehose_chunked(
        str(tmp_path), datasets=["aggTrade"], merges_per_chunk=2,
        now_ts=_NOW, chunk_runner=_failing_runner)
    assert rep.partitions_scanned >= 0                    # returns (no hang)


# -- a parity mismatch must surface in the report, not vanish ------------------

def test_chunked_surfaces_parity_mismatch_and_compacts_the_rest(tmp_path, monkeypatch):
    _make_universe(tmp_path, 3)
    real = maintenance.compact_partition_closed_hours
    seen = {"n": 0}

    def _flaky(path, **kw):
        seen["n"] += 1
        if seen["n"] == 2:               # 2nd partition: simulate a row-count mismatch
            raise ValueError("compaction row-count mismatch in P2: 5 in != 4 out")
        return real(path, **kw)

    monkeypatch.setattr(maintenance, "compact_partition_closed_hours", _flaky)
    rep = maintenance.compact_firehose_chunked(
        str(tmp_path), datasets=["aggTrade"], merges_per_chunk=10,
        now_ts=_NOW, chunk_runner=_inprocess_runner([]))
    # the parity-guard signal is preserved (not swallowed as a generic chunk failure)...
    assert len(rep.mismatches) == 1 and "row-count mismatch" in rep.mismatches[0]
    # ...and the other two partitions still compact (one bad partition isn't fatal).
    assert len(_compact_h_files(tmp_path)) == 2


# -- the real subprocess isolation path ----------------------------------------

def test_real_subprocess_worker_compacts(tmp_path):
    _make_universe(tmp_path, 2)
    rep = maintenance.compact_firehose_chunked(           # default runner = real subprocess
        str(tmp_path), datasets=["aggTrade"], merges_per_chunk=10, now_ts=_NOW)
    assert len(_compact_h_files(tmp_path)) == 2 and rep.hours_compacted == 2
