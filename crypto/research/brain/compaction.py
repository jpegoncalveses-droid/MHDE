"""Brain-store compaction: merge a sealed ``(symbol,date)`` partition's many small
``part-*.parquet`` into one verified file, registry-parity-checked.

The brain store (:mod:`crypto.research.brain.store`) re-inherits capture's fragment wall
one layer down: ``write_snapshots`` emits one brand-new ``part-<uuid>.parquet`` per
``(symbol,date)`` PER PASS, so a continuous runner fans out unboundedly. This module is the
STRUCTURAL runner-gate; the cursor-driven date prune in ``store.read_snapshots`` is the
optimisation. It mirrors ``capture_core/maintenance.py`` (corruption-tolerant, atomic,
idempotent merge) with ONE thing capture could not have:

  THE REGISTRY PARITY ORACLE. Capture's only check is ``sum(input rows) == output rows`` —
  self-referential, so a part file truncated BEFORE compaction is read at face value and
  passes (a masked loss). The brain registry's ``snapshot_bookkeeping`` is an INDEPENDENT
  record of every window written, with its ``n_events`` count, so the compactor cross-checks
  it and catches what input-sum cannot:
    * COMPLETENESS — every registry-recorded window for the partition's date must be present
      in the merged file (a truncated/lost part shows up as a missing window); and
    * EVENT COUNT — each present window's in-row count (the dataset's ``count_fn``) must
      equal the registry ``n_events`` (a corrupted in-row count shows up here).
  The store may legitimately hold windows the registry does NOT yet know (a pass writes
  parquet THEN records bookkeeping — a crash between leaves an un-recorded window), so the
  oracle runs registry -> store (every RECORDED window present), never store -> registry.

Scope (first cut): SEALED partitions only — ``date < today`` — so it never races the live
writer (mirrors ``migrate_compact``'s never-today rule). A registry mismatch means the
inputs were ALREADY short a recorded window before this run; the merge is still mechanically
faithful (rows in == rows out), so we RECORD + surface the mismatch rather than rolling the
merge back (the data was gone before us; flag it, do not pretend). Intra-day closed-hour
compaction of TODAY's accumulating partition is the documented follow-up — the date-prune
makes the runner read today, so bounding today's parts is the next optimisation; it reuses
this module's merge primitive + the event-count half of the oracle.

Subprocess-isolated + chunked (the PR #60 memory model: the merge phase accrues anon memory
~per-merge via pyarrow pool retention, so a whole-universe run in one process drifts onto the
cap — bound peak RSS by RUN SIZE, each chunk in its own process). The PR #60 LESSON applies
in full: the process exit that resets the pool also drops anything the worker does not
explicitly marshal back, so a registry mismatch that is not marshalled surfaces as a clean
"0" — a MASKED data-integrity failure, worse than a crash. Mismatches/skips/counts are
marshalled across the boundary as JSON.

Like the rest of brain this writes ONLY under the given root and NEVER opens DuckDB, the
engine DB, or capture's store. It opens the registry READ-ONLY (parity oracle only).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from crypto.research.brain import config as cfg
from crypto.research.brain import registry
from crypto.research.brain import sources

logger = logging.getLogger("mhde.crypto.brain.compaction")

_MS_PER_DAY = 86_400_000
_NS_PER_DAY = 86_400 * 1_000_000_000
#: Field whose order defines the incremental read cursor; preserved on compaction.
_CURSOR_FIELD = "recv_ts_ns"


# -- filesystem helpers --------------------------------------------------------

def _read_part(path: str):
    """``(table, ok)``. ``ok=False`` -> a corrupt/truncated/unreadable fragment, logged and
    SKIPPED (the capture reader's PR #53 tolerance applied to the brain compactor)."""
    try:
        return pq.ParquetFile(path).read(), True
    except (pa.ArrowInvalid, OSError) as exc:
        logger.warning("brain compaction: skipping unreadable fragment (data absent for this "
                       "span): %s (%s: %s)", path, type(exc).__name__, exc)
        return None, False


def _quarantine(path: str) -> None:
    """Rename a corrupt fragment OUT of the ``*.parquet`` namespace (``.corrupt``) so it is
    preserved for forensics but never re-read or re-compacted."""
    try:
        os.replace(path, path + ".corrupt")
    except OSError as exc:
        logger.warning("brain compaction: could not quarantine %s (%s)", path, exc)


def _writer_parts(part_dir: str) -> list[str]:
    """The writer's small ``part-*.parquet`` only — excludes already-merged ``compact-*``
    files and any in-progress ``.tmp`` (sorted)."""
    if not os.path.isdir(part_dir):
        return []
    return sorted(
        os.path.join(part_dir, n) for n in os.listdir(part_dir)
        if n.startswith("part-") and n.endswith(".parquet"))


def _parse_part_dir(part_dir: str) -> tuple[str, str, str]:
    """``(dataset, symbol, date)`` from ``<root>/<dataset>/symbol=<S>/date=<D>``."""
    parts = pathlib.Path(part_dir).parts
    symbol = parts[-2].split("symbol=", 1)[-1] if len(parts) >= 2 else ""
    date = parts[-1].split("date=", 1)[-1] if parts else ""
    dataset = parts[-3] if len(parts) >= 3 else ""
    return dataset, symbol, date


def _day_bounds_ns(date_str: str) -> tuple[int, int]:
    """``[start_ns, end_ns)`` for the UTC day ``YYYY-MM-DD`` (the partition's window range)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int(d.timestamp()) * 1_000_000_000
    return start, start + _NS_PER_DAY


def _date_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _list_partitions(root: str, datasets: Sequence[str]) -> list[str]:
    """Every ``<root>/<dataset>/symbol=*/date=*`` partition dir for the given datasets."""
    out: list[str] = []
    base = pathlib.Path(root)
    for ds in datasets:
        ds_dir = base / ds
        if not ds_dir.is_dir():
            continue
        for sym_dir in sorted(ds_dir.glob("symbol=*")):
            for date_dir in sorted(sym_dir.glob("date=*")):
                if date_dir.is_dir():
                    out.append(str(date_dir))
    return out


# -- the registry parity oracle ------------------------------------------------

def _registry_mismatches(registry_path: str, dataset: str, symbol: str, date: str,
                         rows: Sequence[dict]) -> list[str]:
    """Cross-check the merged ``rows`` against the registry's record for this partition.

    Returns a list of mismatch strings (empty == clean):
      * a registry-recorded window MISSING from the merged rows (completeness / truncation);
      * a present window whose in-row ``count_fn`` count != the registry ``n_events``.
    Runs registry -> store only (un-recorded store windows are legitimate; see module doc)."""
    spec = sources.SOURCES.get(dataset)
    if spec is None:
        return []
    count_fn = spec.count_fn
    start_ns, end_ns = _day_bounds_ns(date)
    conn = registry.connect(registry_path, read_only=True)
    try:
        expected = {
            int(ws): int(nev)
            for ws, nev in conn.execute(
                "SELECT window_start_ns, n_events FROM snapshot_bookkeeping "
                "WHERE dataset = ? AND symbol = ? "
                "AND window_start_ns >= ? AND window_start_ns < ?",
                (dataset, symbol, start_ns, end_ns))
        }
    finally:
        conn.close()
    by_window: dict[int, dict] = {int(r["window_start_ns"]): r for r in rows}
    mismatches: list[str] = []
    for ws, n_exp in sorted(expected.items()):
        row = by_window.get(ws)
        if row is None:
            mismatches.append(
                f"{dataset}/{symbol}/{date}: registry window {ws} MISSING from store "
                f"(recorded n_events {n_exp}) — truncated/lost part before compaction")
            continue
        actual = int(count_fn(row))
        if actual != n_exp:
            mismatches.append(
                f"{dataset}/{symbol}/{date}: window {ws} in-row n_events {actual} != "
                f"registry n_events {n_exp} — corrupted count")
    return mismatches


# -- the merge primitive -------------------------------------------------------

@dataclass
class BrainCompactionResult:
    rows_before: int
    rows_after: int
    files_before: int
    files_after: int
    out_path: Optional[str] = None
    corrupt_skipped: list = field(default_factory=list)   # quarantined unreadable fragments
    registry_mismatches: list = field(default_factory=list)


def compact_partition(part_dir: str, *, registry_path: Optional[str] = None
                      ) -> BrainCompactionResult:
    """Merge all writer ``part-*`` of one ``(symbol,date)`` partition into one verified file.

    Corruption-tolerant (skip + quarantine an unreadable fragment), crash-safe (write a
    ``.tmp`` sibling, verify ``rows in == rows out``, delete the readable originals, then
    promote), idempotent (already-merged ``compact-*`` files are never re-processed; a
    0/1-part partition is a no-op merge but is still registry-audited). On a ``registry_path``
    the merged rows are cross-checked against the registry (completeness + ``n_events``); any
    mismatch is RECORDED on the result, not raised — the inputs were already short before us.
    A mechanical row-count mismatch (the merge itself losing rows) still raises ``ValueError``
    with the originals intact, exactly as capture does."""
    files = _writer_parts(part_dir)
    if not files:
        return BrainCompactionResult(0, 0, 0, 0, None)

    tables, readable, corrupt = [], [], []
    for p in files:
        table, ok = _read_part(p)
        if ok:
            tables.append(table)
            readable.append(p)
        else:
            corrupt.append(p)

    rows: list[dict] = []
    merged = None
    if tables:
        merged = pa.concat_tables(tables)
        if _CURSOR_FIELD in merged.schema.names:
            merged = merged.sort_by(_CURSOR_FIELD)
        rows = merged.to_pylist()
    rows_before = len(rows)

    dataset, symbol, date = _parse_part_dir(part_dir)
    registry_mismatches: list[str] = []
    if registry_path is not None:
        registry_mismatches = _registry_mismatches(registry_path, dataset, symbol, date, rows)

    out_path: Optional[str] = None
    rows_after = rows_before
    if len(files) >= 2 and merged is not None:           # an actual merge to perform
        tmp_path = os.path.join(part_dir, f"compact-migrated-{uuid4().hex}.parquet.tmp")
        pq.write_table(merged, tmp_path, compression=cfg.PARQUET_COMPRESSION)
        rows_after = pq.read_metadata(tmp_path).num_rows
        if rows_after != rows_before:
            os.remove(tmp_path)
            raise ValueError(
                f"brain compaction row-count mismatch in {part_dir}: "
                f"{rows_before} in != {rows_after} out — originals left intact")
        for p in readable:                               # verified -> drop readable originals
            os.remove(p)
        out_path = tmp_path[: -len(".tmp")]
        os.replace(tmp_path, out_path)

    for p in corrupt:                                    # only after the merge has landed
        _quarantine(p)

    files_after = (1 if out_path is not None else len(readable))
    return BrainCompactionResult(
        rows_before=rows_before, rows_after=rows_after, files_before=len(files),
        files_after=files_after, out_path=out_path,
        corrupt_skipped=list(corrupt), registry_mismatches=registry_mismatches)


# -- chunked, subprocess-bounded driver (the PR #60 memory model) --------------

@dataclass
class BrainCompactionReport:
    partitions_scanned: int = 0
    partitions_compacted: int = 0
    files_before: int = 0
    files_after: int = 0
    mismatches: list[str] = field(default_factory=list)            # mechanical row-count
    registry_mismatches: list[str] = field(default_factory=list)   # the registry oracle
    corrupt_skipped: list[str] = field(default_factory=list)


def _compact_chunk(root: str, paths: Sequence[str], budget: int,
                   registry_path: Optional[str]) -> dict:
    """Compact partitions until ~``budget`` merges are done; marshal the chunk summary.

    ``completed`` is the count of partitions FULLY processed (the caller advances past them).
    The shared unit run by both the subprocess worker and the in-process test runner. EVERY
    finding (mechanical mismatch, registry mismatch, corrupt skip, counts) is returned — the
    PR #60 lesson: the subprocess exit drops anything not marshalled, so an un-marshalled
    registry mismatch would surface as a silent "0"."""
    merges = files_before = files_after = compacted = completed = 0
    mismatches: list[str] = []
    registry_mismatches: list[str] = []
    corrupt_skipped: list[str] = []
    for path in paths:
        try:
            res = compact_partition(path, registry_path=registry_path)
        except ValueError as exc:        # mechanical row-count mismatch (originals intact)
            mismatches.append(str(exc))
            completed += 1
            if merges >= budget:
                break
            continue
        files_before += res.files_before
        files_after += res.files_after
        registry_mismatches.extend(res.registry_mismatches)
        corrupt_skipped.extend(res.corrupt_skipped)
        if res.out_path is not None:
            merges += 1
            compacted += 1
        completed += 1
        if merges >= budget:             # finish the current partition, then stop
            break
    return {"completed": completed, "compacted": compacted, "merges": merges,
            "files_before": files_before, "files_after": files_after,
            "mismatches": mismatches, "registry_mismatches": registry_mismatches,
            "corrupt_skipped": corrupt_skipped}


def _inprocess_chunk_runner():
    """A ``chunk_runner`` that runs :func:`_compact_chunk` IN-PROCESS (tests; no subprocess)."""
    def _run(root, paths, budget, registry_path):
        return _compact_chunk(root, list(paths), budget, registry_path)
    return _run


def _run_chunk_subprocess(root: str, paths: Sequence[str], budget: int,
                          registry_path: Optional[str]) -> dict:
    """Run one chunk in a FRESH subprocess (the memory reset). Partition paths go over stdin
    (no argv length limit). A failed chunk (e.g. an OOM-killed subprocess) returns
    completed=0; the driver still advances by 1 so a single bad partition cannot wedge it."""
    proc = subprocess.run(
        [sys.executable, "-m", "crypto.research.brain._compact_chunk_worker",
         root, str(int(budget)), registry_path or ""],
        input="\n".join(paths), capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        logger.error("brain compaction chunk subprocess failed (rc=%s): %s",
                     proc.returncode, (proc.stderr or "")[-500:])
        return {"completed": 0, "compacted": 0, "merges": 0, "files_before": 0,
                "files_after": 0, "mismatches": [], "registry_mismatches": [],
                "corrupt_skipped": []}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def compact_brain_chunked(
    root: str,
    *,
    datasets: Sequence[str],
    merges_per_chunk: int = cfg.BRAIN_PASS_BATCH_SIZE,
    registry_path: Optional[str] = None,
    now_ms: Optional[int] = None,
    chunk_runner=None,
) -> BrainCompactionReport:
    """Compact every SEALED ``(symbol,date)`` brain partition (``date < today``) in
    subprocess-bounded chunks of ~``merges_per_chunk`` merges (peak RSS bounded by RUN SIZE).

    ``chunk_runner(root, paths, budget, registry_path) -> dict`` runs one chunk; the default
    isolates each in a subprocess. Today's partition is never touched (never race the live
    writer). Registry mismatches are marshalled through every chunk into the report."""
    chunk_runner = chunk_runner or _run_chunk_subprocess
    today = _date_str(now_ms if now_ms is not None else int(time.time() * 1000))
    paths = [p for p in _list_partitions(root, datasets)
             if _parse_part_dir(p)[2] < today]            # sealed only; ISO dates sort lexically
    report = BrainCompactionReport()
    i = 0
    chunks = 0
    while i < len(paths):
        res = chunk_runner(root, paths[i:], merges_per_chunk, registry_path)
        report.partitions_scanned += int(res["completed"])
        report.partitions_compacted += int(res.get("compacted", 0))
        report.files_before += int(res["files_before"])
        report.files_after += int(res["files_after"])
        report.mismatches.extend(res.get("mismatches", []))
        report.registry_mismatches.extend(res.get("registry_mismatches", []))
        report.corrupt_skipped.extend(res.get("corrupt_skipped", []))
        i += max(int(res["completed"]), 1)               # always advance -> no infinite loop
        chunks += 1
    logger.info(
        "brain compaction: scanned %d sealed partitions, compacted %d over %d subprocess "
        "chunks (budget %d), files %d -> %d, mismatches %d, registry-mismatches %d, "
        "corrupt-skipped %d",
        report.partitions_scanned, report.partitions_compacted, chunks, merges_per_chunk,
        report.files_before, report.files_after, len(report.mismatches),
        len(report.registry_mismatches), len(report.corrupt_skipped))
    return report
