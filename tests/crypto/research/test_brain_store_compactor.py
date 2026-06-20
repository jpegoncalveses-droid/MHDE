"""Brain-store compactor — merge ``part-*`` per ``(symbol,date)``, registry-parity-checked.

The brain store re-inherits capture's fragment wall one layer down: ``write_snapshots``
emits one brand-new ``part-<uuid>.parquet`` per ``(symbol,date)`` per pass, so a continuous
runner fans out unboundedly. This compactor is the STRUCTURAL runner-gate (the date-prune
in :mod:`store` is the optimisation): it merges a sealed ``(symbol,date)`` partition's many
small parts into one verified file.

What makes it stronger than capture's compactor is the REGISTRY parity oracle. Capture only
checks ``sum(input rows) == output rows`` — self-referential, so a part file truncated
BEFORE compaction is read at face value and passes. The brain registry
(``snapshot_bookkeeping``) is an INDEPENDENT record of every window that was written, with
its ``n_events`` count, so the compactor cross-checks:
  * COMPLETENESS — every registry-recorded window for the partition's date is present in the
    merged file (catches a truncated/lost part — the case input-sum misses); and
  * EVENT COUNT — each present window's in-row count (via the dataset's ``count_fn``) equals
    the registry ``n_events`` (catches a corrupted in-row count).

Subprocess-isolated + chunked (the PR #60 memory model) with the registry-mismatch signal
MARSHALLED back across the process boundary — never swallowed into a silent "0".
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timezone

import pyarrow.parquet as pq

from crypto.research.brain import compaction
from crypto.research.brain import config as cfg
from crypto.research.brain import registry
from crypto.research.brain import store

_DATASET = cfg.MARKPRICE_DATASET                       # has a clean count_fn = update_count
_SCHEMA = store.MARKPRICE_SNAPSHOT_SCHEMA
_SEALED = "2026-06-17"                                  # a sealed (past) date partition
_TODAY = "2026-06-19"
_NOW_MS = int(datetime(2026, 6, 19, 12, tzinfo=timezone.utc).timestamp() * 1000)


def _ns(date_str, minute=0):
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1_000_000_000) + minute * 60_000_000_000


def _snap(symbol, window_start_ns, *, update_count=5):
    """A full markprice snapshot row; only the fields the test asserts on vary."""
    row = {name: 0 for name in _SCHEMA.names}
    row.update(symbol=symbol, window_start_ns=window_start_ns,
               window_end_ns=window_start_ns + 60_000_000_000,
               recv_ts_ns=window_start_ns, mark_close=100.0,
               update_count=update_count)
    return row


def _write_pass(root, symbol, window_start_ns, *, update_count=5):
    """One write pass -> one part file in the (symbol,date) partition."""
    store.write_snapshots(str(root), _DATASET, _SCHEMA,
                          [_snap(symbol, window_start_ns, update_count=update_count)])


def _part_dir(root, symbol, date=_SEALED):
    return pathlib.Path(root, _DATASET, f"symbol={symbol}", f"date={date}")


def _part_files(root, symbol, date=_SEALED):
    return sorted(_part_dir(root, symbol, date).glob("part-*.parquet"))


def _compact_files(root, symbol, date=_SEALED):
    return sorted(_part_dir(root, symbol, date).glob("compact-*.parquet"))


def _reg_path(root):
    return str(pathlib.Path(root, "registry.sqlite"))


def _record(root, symbol, windows, *, date=_SEALED):
    """Record bookkeeping rows for ``windows`` = [(minute, n_events), ...]."""
    conn = registry.connect(_reg_path(root))
    bk = [{"dataset": _DATASET, "symbol": symbol, "window_start_ns": _ns(date, m),
           "window_end_ns": _ns(date, m) + 60_000_000_000, "recv_ts_ns": _ns(date, m),
           "n_events": nev} for m, nev in windows]
    registry.record_windows(conn, bk, now_ns=1)
    conn.close()


def _poison(part_dir):
    (part_dir / "part-poison.parquet").write_bytes(b"not a parquet at all")


# -- the merge primitive -------------------------------------------------------

def test_compact_partition_merges_parts_into_one(tmp_path):
    for m in range(3):                                  # 3 passes -> 3 part files, 3 windows
        _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, m))
    assert len(_part_files(tmp_path, "BTCUSDT")) == 3
    res = compaction.compact_partition(str(_part_dir(tmp_path, "BTCUSDT")))
    assert res.files_before == 3 and res.files_after == 1
    assert res.rows_before == 3 and res.rows_after == 3
    assert _part_files(tmp_path, "BTCUSDT") == []        # writer parts consumed
    # read by PHYSICAL schema (ParquetFile, not read_table) — a hive read would infer the
    # path's symbol= as a dictionary column and collide with the in-row string symbol.
    merged = pq.ParquetFile(str(_compact_files(tmp_path, "BTCUSDT")[0])).read().to_pylist()
    assert sorted(r["window_start_ns"] for r in merged) == [_ns(_SEALED, m) for m in range(3)]


def test_compact_partition_tolerates_corrupt_fragment(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, 0))
    _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, 1))
    _poison(_part_dir(tmp_path, "BTCUSDT"))              # a third, unreadable part
    res = compaction.compact_partition(str(_part_dir(tmp_path, "BTCUSDT")))
    assert res.rows_after == 2                           # readable rows survive
    assert len(res.corrupt_skipped) == 1                # corrupt fragment marshalled
    # quarantined OUT of the *.parquet namespace, never re-read
    assert (_part_dir(tmp_path, "BTCUSDT") / "part-poison.parquet.corrupt").exists()
    assert not (_part_dir(tmp_path, "BTCUSDT") / "part-poison.parquet").exists()


# -- the registry parity oracle (strictly stronger than input-sum) -------------

def test_registry_completeness_catches_a_missing_window(tmp_path):
    # registry recorded THREE windows; the store only has TWO (one part lost before us).
    _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, 0))
    _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, 1))
    _record(tmp_path, "BTCUSDT", [(0, 5), (1, 5), (2, 5)])
    res = compaction.compact_partition(str(_part_dir(tmp_path, "BTCUSDT")),
                                       registry_path=_reg_path(tmp_path))
    # the merge is mechanically faithful (2 in == 2 out) but the registry flags the gap
    assert res.rows_after == 2
    assert len(res.registry_mismatches) == 1
    assert str(_ns(_SEALED, 2)) in res.registry_mismatches[0]


def test_registry_event_count_catches_a_corrupted_count(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, 0), update_count=4)   # row says 4
    _record(tmp_path, "BTCUSDT", [(0, 5)])                              # registry says 5
    res = compaction.compact_partition(str(_part_dir(tmp_path, "BTCUSDT")),
                                       registry_path=_reg_path(tmp_path))
    assert len(res.registry_mismatches) == 1
    assert "n_events" in res.registry_mismatches[0]


def test_registry_clean_partition_has_no_mismatch(tmp_path):
    for m in range(3):
        _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, m), update_count=5)
    _record(tmp_path, "BTCUSDT", [(0, 5), (1, 5), (2, 5)])
    res = compaction.compact_partition(str(_part_dir(tmp_path, "BTCUSDT")),
                                       registry_path=_reg_path(tmp_path))
    assert res.registry_mismatches == []


def test_compact_partition_is_idempotent(tmp_path):
    for m in range(3):
        _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, m))
    compaction.compact_partition(str(_part_dir(tmp_path, "BTCUSDT")))
    again = compaction.compact_partition(str(_part_dir(tmp_path, "BTCUSDT")))
    # re-run is a no-op: no writer parts remain, so nothing is merged or rewritten
    assert again.files_before == 0 and again.out_path is None
    assert len(_compact_files(tmp_path, "BTCUSDT")) == 1   # still exactly one compact file


# -- the chunked, sealed-only driver -------------------------------------------

def test_chunked_compacts_sealed_partitions_not_today(tmp_path):
    for k in range(4):
        for m in range(2):
            _write_pass(tmp_path, f"SYM{k}USDT", _ns(_SEALED, m))
    _write_pass(tmp_path, "TODAYUSDT", _ns(_TODAY, 0), update_count=5)   # today's live partition
    _write_pass(tmp_path, "TODAYUSDT", _ns(_TODAY, 1), update_count=5)
    rep = compaction.compact_brain_chunked(
        str(tmp_path), datasets=[_DATASET], merges_per_chunk=2, now_ms=_NOW_MS,
        chunk_runner=compaction._inprocess_chunk_runner())
    assert rep.partitions_compacted == 4
    for k in range(4):
        assert len(_compact_files(tmp_path, f"SYM{k}USDT")) == 1
    # today's partition is never raced
    assert len(_part_files(tmp_path, "TODAYUSDT", _TODAY)) == 2
    assert _compact_files(tmp_path, "TODAYUSDT", _TODAY) == []


def test_chunked_marshals_registry_mismatch_back(tmp_path):
    # one sealed partition is short a registry window; the driver must SURFACE it.
    _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, 0))   # store has windows 0 and 2 (2 parts)
    _write_pass(tmp_path, "BTCUSDT", _ns(_SEALED, 2))
    _record(tmp_path, "BTCUSDT", [(0, 5), (1, 5), (2, 5)])   # registry knows window 1 too
    _write_pass(tmp_path, "ETHUSDT", _ns(_SEALED, 0))   # a clean second partition (2 parts)
    _write_pass(tmp_path, "ETHUSDT", _ns(_SEALED, 1))
    _record(tmp_path, "ETHUSDT", [(0, 5), (1, 5)])
    rep = compaction.compact_brain_chunked(
        str(tmp_path), datasets=[_DATASET], merges_per_chunk=10, now_ms=_NOW_MS,
        registry_path=_reg_path(tmp_path),
        chunk_runner=compaction._inprocess_chunk_runner())
    assert len(rep.registry_mismatches) == 1            # not swallowed as a silent 0
    assert str(_ns(_SEALED, 1)) in rep.registry_mismatches[0]
    assert rep.partitions_compacted == 2                # both still compact


def test_chunked_real_subprocess_isolation(tmp_path):
    for k in range(2):
        for m in range(2):
            _write_pass(tmp_path, f"SYM{k}USDT", _ns(_SEALED, m))
    _record(tmp_path, "SYM0USDT", [(0, 5), (1, 5)])
    _record(tmp_path, "SYM1USDT", [(0, 5), (1, 5)])
    rep = compaction.compact_brain_chunked(           # default runner = real subprocess
        str(tmp_path), datasets=[_DATASET], merges_per_chunk=10, now_ms=_NOW_MS,
        registry_path=_reg_path(tmp_path))
    assert rep.partitions_compacted == 2
    assert rep.registry_mismatches == []
    for k in range(2):
        assert len(_compact_files(tmp_path, f"SYM{k}USDT")) == 1
