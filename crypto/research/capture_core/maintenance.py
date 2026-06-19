"""Capture-core firehose maintenance: compaction, rolling retention, migration.

Three filesystem-only operations on the raw firehose store (Phase 0):

  * :func:`compact_partition` — merge the many small ``part-*.parquet`` of ONE
    ``symbol=/date=`` partition into a single verified file. Rows are kept in
    ``recv_ts_ns`` order so the incremental cursor stays monotonic; the small parts
    are removed only AFTER the merged file is written and row-count-verified.
  * :func:`expire_firehose_partitions` — prune whole ``date=`` partitions older than
    the rolling window, oldest-first, never today's, firehose datasets only.
  * :func:`migrate_compact` — one-shot driver that compacts every (selected) firehose
    partition with pre/post row-count parity, reporting any mismatch instead of
    deleting on it.

Like the rest of capture-core this NEVER opens DuckDB and writes only under the
given root. Compaction preserves the read contract verbatim: the ``symbol=/date=``
event-time partitioning, the per-stream field names/types, and ``recv_ts_ns``.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import disk_guard as dg
from crypto.research.capture_core import store as capture_store

import logging

logger = logging.getLogger("mhde.crypto.capture_core.maintenance")

#: Field whose order defines the incremental read cursor; preserved on compaction.
_CURSOR_FIELD = "recv_ts_ns"

#: Reason tag for a gap recorded when compaction skips an unreadable fragment.
_SKIP_REASON = "compaction_skipped_corrupt"


def _read_part(path: str):
    """``(table, ok)``. ``ok=False`` -> a corrupt/truncated/unreadable fragment, logged
    and SKIPPED (the PR #53 reader tolerance applied to the compactor)."""
    try:
        return pq.ParquetFile(path).read(), True
    except (pa.ArrowInvalid, OSError) as exc:
        logger.warning("compaction: skipping unreadable fragment (data absent for this "
                       "span): %s (%s: %s)", path, type(exc).__name__, exc)
        return None, False


def _recv_bounds(table) -> tuple:
    """``(min_recv, max_recv)`` over the cursor field, or ``(None, None)`` if absent/empty."""
    if _CURSOR_FIELD not in table.schema.names or table.num_rows == 0:
        return None, None
    col = table.column(_CURSOR_FIELD)
    return pc.min(col).as_py(), pc.max(col).as_py()


def _quarantine(path: str) -> None:
    """Rename a corrupt fragment OUT of the ``*.parquet`` namespace (``.corrupt``) so it is
    preserved for forensics but never re-read by a reader or re-processed by compaction."""
    try:
        os.replace(path, path + ".corrupt")
    except OSError as exc:
        logger.warning("compaction: could not quarantine %s (%s)", path, exc)


def _parse_part_dir(part_dir: str) -> tuple:
    """``(symbol, dataset)`` from ``<root>/<dataset>/symbol=<S>/date=<D>``."""
    parts = pathlib.Path(part_dir).parts
    symbol = parts[-2].split("symbol=", 1)[-1] if len(parts) >= 2 else ""
    dataset = parts[-3] if len(parts) >= 3 else ""
    return symbol, dataset


def _infer_gaps(part_dir: str, entries: Sequence[tuple]) -> list:
    """A gap per corrupt fragment, span inferred from the NEIGHBOR bounds (the only signal a
    footerless file still carries is its flush mtime). ``entries`` are
    ``(mtime, ok, min_recv, max_recv, path)``; ordered by mtime, a corrupt fragment's gap is
    ``prev_readable.max_recv -> next_readable.min_recv``. Degenerate (no neighbor) -> 0 ms."""
    ordered = sorted(entries, key=lambda e: e[0])
    symbol, dataset = _parse_part_dir(part_dir)
    gaps = []
    for i, e in enumerate(ordered):
        if e[1]:                                  # ok fragment -> not a gap
            continue
        prev_ok = next((ordered[j] for j in range(i - 1, -1, -1) if ordered[j][1]), None)
        next_ok = next((ordered[j] for j in range(i + 1, len(ordered)) if ordered[j][1]), None)
        start_recv = prev_ok[3] if prev_ok else (next_ok[2] if next_ok else None)
        end_recv = next_ok[2] if next_ok else (prev_ok[3] if prev_ok else None)
        gaps.append({
            "symbol": symbol, "stream": dataset, "reason": _SKIP_REASON,
            "gap_start_ms": start_recv // 1_000_000 if start_recv is not None else 0,
            "gap_end_ms": end_recv // 1_000_000 if end_recv is not None else 0,
        })
    return gaps


def _count_rows_tolerant(files: Sequence[str]) -> int:
    """Sum ``num_rows`` over ``files``, skipping any that fail to open (corrupt)."""
    total = 0
    for f in files:
        try:
            total += pq.read_metadata(f).num_rows
        except (pa.ArrowInvalid, OSError):
            pass
    return total


def _persist_gaps(root: str, gaps: Sequence[dict], *, now_ms: Optional[int] = None) -> None:
    """Append compaction-inferred gaps to the capture ``_gaps`` manifest (flag-don't-drop)."""
    if not gaps:
        return
    now_ns = (now_ms if now_ms is not None else int(time.time() * 1000)) * 1_000_000
    w = capture_store.gap_writer(str(root))
    for g in gaps:
        w.append({**g, "recorded_recv_ts_ns": now_ns})
    w.flush_all()


def _date_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _part_files(part_dir: str) -> list[str]:
    """The ``*.parquet`` part files in a partition dir (sorted, .tmp excluded)."""
    if not os.path.isdir(part_dir):
        return []
    return sorted(
        os.path.join(part_dir, n) for n in os.listdir(part_dir)
        if n.endswith(".parquet")
    )


# -- compaction primitive -----------------------------------------------------

@dataclass
class CompactionResult:
    rows_before: int
    rows_after: int
    files_before: int
    files_after: int
    out_path: Optional[str] = None
    gaps: list = field(default_factory=list)   # gaps inferred for SKIPPED corrupt fragments


def _merge_files(part_dir: str, files: Sequence[str], *, out_base: str
                 ) -> CompactionResult:
    """Crash-safe, CORRUPTION-TOLERANT merge of ``files`` into one
    ``<out_base>-<uuid>.parquet`` in-place.

    Read each file (skipping any unreadable/truncated fragment — a partial flush) →
    concatenate the READABLE tables → sort by ``recv_ts_ns`` (monotonic cursor preserved) →
    write a ``.tmp`` sibling → verify the row count equals the sum of the READABLE inputs →
    delete the readable originals → promote the tmp → QUARANTINE each corrupt fragment
    (``.corrupt``) and RECORD a gap for it (neighbor-inferred span, flag-don't-drop). A
    crash mid-merge never loses data (worst case: the originals plus an ignored orphan
    ``.tmp``). The parity baseline EXCLUDES the corrupt fragments' (unreadable) rows.

    Reads each file by its PHYSICAL schema (``ParquetFile.read()``); ``pq.read_table``
    on a path under ``symbol=/date=/`` would infer hive partitioning and bake
    ``symbol``/``date`` into the output, colliding with the path-derived partition
    columns on a hive-dataset read.
    """
    entries: list = []                 # (mtime, ok, min_recv, max_recv, path)
    tables, readable, corrupt = [], [], []
    for p in files:
        table, ok = _read_part(p)
        if ok:
            mn, mx = _recv_bounds(table)
            entries.append((os.stat(p).st_mtime, True, mn, mx, p))
            tables.append(table)
            readable.append(p)
        else:
            entries.append((os.stat(p).st_mtime, False, None, None, p))
            corrupt.append(p)

    gaps = _infer_gaps(part_dir, entries) if corrupt else []

    if not tables:                     # nothing readable -> only quarantine + flag
        for p in corrupt:
            _quarantine(p)
        return CompactionResult(rows_before=0, rows_after=0, files_before=len(files),
                                files_after=0, out_path=None, gaps=gaps)

    merged = pa.concat_tables(tables)
    rows_before = merged.num_rows
    if _CURSOR_FIELD in merged.schema.names:
        merged = merged.sort_by(_CURSOR_FIELD)

    tmp_path = os.path.join(part_dir, f"{out_base}-{uuid4().hex}.parquet.tmp")
    pq.write_table(merged, tmp_path, compression=cfg.PARQUET_COMPRESSION)

    rows_after = pq.read_metadata(tmp_path).num_rows
    if rows_after != rows_before:
        os.remove(tmp_path)
        raise ValueError(
            f"compaction row-count mismatch in {part_dir}: "
            f"{rows_before} in != {rows_after} out — originals left intact")

    for p in readable:                 # verified -> safe to drop the readable originals
        os.remove(p)
    out_path = tmp_path[:-len(".tmp")]
    os.replace(tmp_path, out_path)
    for p in corrupt:                  # only after the merge has landed
        _quarantine(p)
    return CompactionResult(rows_before=rows_before, rows_after=rows_after,
                            files_before=len(files), files_after=1, out_path=out_path,
                            gaps=gaps)


def compact_partition(part_dir: str) -> CompactionResult:
    """Merge ALL ``*.parquet`` in one ``symbol=/date=`` partition into one file.

    The OFFLINE one-shot migration primitive (whole-partition merge). Emits a
    ``compact-migrated-*`` file — a namespace distinct from the writer's ``part-*`` so
    its output is never re-bucketed by live closed-hour compaction. Run offline only;
    NEVER concurrently with the live :func:`compact_partition_closed_hours` (which
    merges per-closed-hour subsets). A 0/1-file partition is a no-op (a lone CORRUPT file
    is quarantined + gap-flagged).
    """
    parts = _part_files(part_dir)
    if len(parts) <= 1:
        if parts:
            _table, ok = _read_part(parts[0])
            if not ok:                 # lone corrupt file -> quarantine + flag, no merge
                gaps = _infer_gaps(
                    part_dir, [(os.stat(parts[0]).st_mtime, False, None, None, parts[0])])
                _quarantine(parts[0])
                return CompactionResult(0, 0, 1, 0, None, gaps=gaps)
            rows = _table.num_rows
            return CompactionResult(rows, rows, 1, 1, parts[0])
        return CompactionResult(0, 0, 0, 0, None)
    return _merge_files(part_dir, parts, out_base="compact-migrated")


# -- ADR-038 closed-hour compaction (the write-then-compact merge step) --------

def _writer_parts_with_mtime(part_dir: str) -> list[tuple[str, float]]:
    """``(path, mtime)`` for the writer's small ``part-*.parquet`` only — excludes
    already-compacted ``compact-*`` files and any in-progress ``.tmp``."""
    out: list[tuple[str, float]] = []
    if not os.path.isdir(part_dir):
        return out
    for n in os.listdir(part_dir):
        if n.startswith("part-") and n.endswith(".parquet"):
            p = os.path.join(part_dir, n)
            out.append((p, os.stat(p).st_mtime))
    return out


def compact_partition_closed_hours(
    part_dir: str,
    *,
    now_ts: float,
    grace_s: float = cfg.CAPTURE_COMPACTION_GRACE_S,
) -> list[CompactionResult]:
    """Merge the writer's small ``part-*`` files of each CLOSED clock-hour into one
    ``compact-h<hour>-<uuid>.parquet``, SKIPPING the open hour and any hour still
    within ``grace_s`` of its end.

    Files are bucketed by their **flush (mtime) hour**, which is > the flush interval
    after the data was received. Because ``grace_s >> flush_interval``, a closed hour's
    files are provably all on disk (the writer has moved on) before it is compacted, so
    the compactor never races an in-flight file. A row that arrives LATE (event time in
    an already-sealed hour) is flushed in the *current* hour and compacted with *that*
    hour — never folded into the sealed hour and never lost (it stays under the correct
    ``symbol=/date=`` event-date partition). Already-compacted ``compact-*`` files are
    not re-processed. Reuses the crash-safe :func:`_merge_files`.
    """
    buckets: dict[int, list[str]] = {}
    for path, mtime in _writer_parts_with_mtime(part_dir):
        buckets.setdefault(int(mtime // 3600), []).append(path)
    results: list[CompactionResult] = []
    for hour in sorted(buckets):
        hour_end = (hour + 1) * 3600
        if hour_end + grace_s > now_ts:       # open hour or still within grace -> leave
            continue
        files = sorted(buckets[hour])
        if len(files) < 2:                     # 0/1 file -> nothing to merge
            continue
        results.append(_merge_files(part_dir, files, out_base=f"compact-h{hour}"))
    return results


@dataclass
class FirehoseCompactionReport:
    partitions_scanned: int = 0
    hours_compacted: int = 0
    files_before: int = 0
    files_after: int = 0
    rows_before: int = 0
    rows_after: int = 0
    mismatches: list[str] = field(default_factory=list)


def compact_firehose_closed_hours(
    root: str,
    *,
    datasets: Sequence[str] = cfg.FIREHOSE_PRUNABLE_DATASETS,
    now_ts: Optional[float] = None,
    grace_s: float = cfg.CAPTURE_COMPACTION_GRACE_S,
) -> FirehoseCompactionReport:
    """Run closed-hour compaction across every firehose ``symbol=/date=`` partition.

    The hourly-timer entry point. Filesystem-only; never opens the DB. A per-hour
    row-count mismatch is recorded (and its originals left intact by
    :func:`_merge_files`) rather than dropping rows.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    parts = dg.list_firehose_partitions(root, tuple(datasets))
    report = FirehoseCompactionReport()
    gap_records: list = []
    for p in parts:
        report.partitions_scanned += 1
        try:
            results = compact_partition_closed_hours(
                p.path, now_ts=now_ts, grace_s=grace_s)
        except ValueError as exc:
            report.mismatches.append(str(exc))
            continue
        for r in results:
            report.hours_compacted += 1
            report.files_before += r.files_before
            report.files_after += r.files_after
            report.rows_before += r.rows_before
            report.rows_after += r.rows_after
            gap_records.extend(r.gaps)
    _persist_gaps(root, gap_records)
    logger.info(
        "firehose closed-hour compaction: scanned %d partitions, compacted %d hours, "
        "files %d -> %d, rows %d -> %d, mismatches %d",
        report.partitions_scanned, report.hours_compacted, report.files_before,
        report.files_after, report.rows_before, report.rows_after,
        len(report.mismatches))
    return report


# -- rolling retention --------------------------------------------------------

def expire_firehose_partitions(
    root: str,
    *,
    days: int = cfg.CAPTURE_RAW_RETENTION_DAYS,
    datasets: Sequence[str] = cfg.FIREHOSE_PRUNABLE_DATASETS,
    now_ms: Optional[int] = None,
) -> list[str]:
    """Delete firehose ``date=`` partitions older than ``days`` (rolling window).

    Keeps partitions whose date is >= the cutoff (now - days) — so today's partition
    is always kept — and removes older ones, oldest-first, across the firehose
    datasets only. ``klines_1h``, the REST present-state series, and ``_gaps`` are
    never touched (they are simply not in ``datasets``). Returns removed dirs.
    Filesystem-only; no DB.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = _date_str(now_ms - days * 86_400_000)
    parts = dg.list_firehose_partitions(root, tuple(datasets))
    removed: list[str] = []
    for p in sorted(parts, key=lambda x: (x.date, x.path)):  # oldest-first
        if p.date < cutoff:                                  # ISO dates sort lexically
            shutil.rmtree(p.path)
            removed.append(p.path)
    if removed:
        logger.info("firehose retention: expired %d partitions older than %s",
                    len(removed), cutoff)
    return removed


def expire_depth_state_partitions(
    root: str,
    *,
    days: int = cfg.DEPTH_STATE_RETENTION_DAYS,
    now_ms: Optional[int] = None,
) -> list[str]:
    """Delete ``depth_state`` ``date=`` partitions older than ``days``.

    depth_state is a SHORT consumption buffer (the online top-N book states), not a
    history tape, so it prunes on its OWN short retention — separate from the 7-day
    firehose window — and is deliberately NOT in ``FIREHOSE_PRUNABLE_DATASETS``.
    Reuses the same symbol=/date= partition enumerator. Filesystem-only; no DB.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = _date_str(now_ms - days * 86_400_000)
    parts = dg.list_firehose_partitions(root, (cfg.DEPTH_STATE_DATASET,))
    removed: list[str] = []
    for p in sorted(parts, key=lambda x: (x.date, x.path)):  # oldest-first
        if p.date < cutoff:                                  # ISO dates sort lexically
            shutil.rmtree(p.path)
            removed.append(p.path)
    if removed:
        logger.info("depth_state retention: expired %d partitions older than %s",
                    len(removed), cutoff)
    return removed


# -- one-shot migration -------------------------------------------------------

@dataclass
class MigrationReport:
    partitions_scanned: int = 0
    partitions_compacted: int = 0
    files_before: int = 0
    files_after: int = 0
    rows_before: int = 0
    rows_after: int = 0
    mismatches: list[str] = field(default_factory=list)


def migrate_compact(
    root: str,
    *,
    datasets: Sequence[str] = cfg.FIREHOSE_PRUNABLE_DATASETS,
    dates: Optional[Sequence[str]] = None,
    now_ms: Optional[int] = None,
    dry_run: bool = False,
) -> MigrationReport:
    """One-shot compaction of surviving firehose days into the bounded-file layout.

    Compacts every selected ``symbol=/date=`` partition that holds more than one part
    file, verifying per-partition pre/post row-count parity; a mismatch is recorded
    (and the partition's originals left intact by :func:`compact_partition`) rather
    than silently dropping rows. ``dates`` (a set of ``YYYY-MM-DD``) restricts the
    sweep — e.g. the surviving raw days. TODAY's partition is always skipped (mirrors
    retention's never-today rule; never race the live writer). ``dry_run`` measures
    only (no writes).
    """
    date_filter = set(dates) if dates is not None else None
    today = _date_str(now_ms if now_ms is not None else int(time.time() * 1000))
    parts = dg.list_firehose_partitions(root, tuple(datasets))
    report = MigrationReport()
    gap_records: list = []
    for p in sorted(parts, key=lambda x: (x.date, x.path)):
        if p.date == today:                              # never touch the live day
            continue
        if date_filter is not None and p.date not in date_filter:
            continue
        files = _part_files(p.path)
        report.partitions_scanned += 1
        report.files_before += len(files)
        rows = _count_rows_tolerant(files)               # READABLE rows (skip corrupt)
        report.rows_before += rows

        if dry_run or len(files) <= 1:
            report.files_after += len(files)
            report.rows_after += rows
            continue

        try:
            res = compact_partition(p.path)
        except ValueError as exc:
            report.mismatches.append(str(exc))
            report.files_after += len(files)      # untouched on mismatch
            report.rows_after += rows
            continue
        report.partitions_compacted += 1
        report.files_after += res.files_after
        report.rows_after += res.rows_after
        gap_records.extend(res.gaps)

    if not dry_run:
        _persist_gaps(root, gap_records, now_ms=now_ms)

    logger.info(
        "firehose migration: scanned %d partitions, compacted %d, files %d -> %d, "
        "rows %d -> %d, mismatches %d%s",
        report.partitions_scanned, report.partitions_compacted, report.files_before,
        report.files_after, report.rows_before, report.rows_after,
        len(report.mismatches), " (DRY RUN)" if dry_run else "")
    return report
