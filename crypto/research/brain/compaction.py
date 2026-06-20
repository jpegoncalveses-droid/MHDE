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
import pyarrow.compute as pc
import pyarrow.parquet as pq

from crypto.research.brain import config as cfg
from crypto.research.brain import registry
from crypto.research.brain import sources

logger = logging.getLogger("mhde.crypto.brain.compaction")

_MS_PER_DAY = 86_400_000
_NS_PER_DAY = 86_400 * 1_000_000_000
#: One event-time hour in ns — the bucketing grain of the intra-day closed-hour compactor.
HOUR_NS = 3_600 * 1_000_000_000
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


def _hour_floor(window_start_ns: int) -> int:
    """The event-time hour ``window_start_ns`` falls in (a multiple of :data:`HOUR_NS`)."""
    return (window_start_ns // HOUR_NS) * HOUR_NS


def _is_hour_closed(hour_ns: int, now_ns: int, watermark_ns: int) -> bool:
    """``True`` once no new window with ``window_start`` in ``[hour_ns, hour_ns+HOUR_NS)`` can
    still be written: the hour's last window ends at ``hour_ns+HOUR_NS`` and settles a watermark
    later, so the hour is sealed at ``hour_ns + HOUR_NS + watermark_ns`` (``<=`` is the precise
    boundary). Cadence is intentionally NOT subtracted — using the hour END as the settle
    reference is the conservative (later-closing) choice and is sound (cadence << watermark)."""
    return (hour_ns + HOUR_NS + watermark_ns) <= now_ns


def _filter_table_to_hour(table: "pa.Table", hour_ns: int) -> "pa.Table":
    """Rows of ``table`` whose ``window_start_ns`` falls in ``[hour_ns, hour_ns+HOUR_NS)``."""
    ws = table.column("window_start_ns")
    mask = pc.and_(pc.greater_equal(ws, hour_ns), pc.less(ws, hour_ns + HOUR_NS))
    return table.filter(mask)


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

def _registry_mismatches_in_range(registry_path: str, dataset: str, symbol: str,
                                  start_ns: int, end_ns: int, rows: Sequence[dict],
                                  *, scope: str) -> list[str]:
    """Cross-check the merged ``rows`` against the registry roster for window-starts in
    ``[start_ns, end_ns)`` (a whole day for :func:`compact_partition`, one hour for the
    intra-day path). ``scope`` only labels the message.

    Returns a list of mismatch strings (empty == clean):
      * a registry-recorded window MISSING from the merged rows (completeness / truncation);
      * a present window whose in-row ``count_fn`` count != the registry ``n_events``.
    Runs registry -> store only (un-recorded store windows are legitimate; see module doc)."""
    spec = sources.SOURCES.get(dataset)
    if spec is None:
        return []
    count_fn = spec.count_fn
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
                f"{dataset}/{symbol}/{scope}: registry window {ws} MISSING from store "
                f"(recorded n_events {n_exp}) — truncated/lost part before compaction")
            continue
        actual = int(count_fn(row))
        if actual != n_exp:
            mismatches.append(
                f"{dataset}/{symbol}/{scope}: window {ws} in-row n_events {actual} != "
                f"registry n_events {n_exp} — corrupted count")
    return mismatches


def _registry_mismatches(registry_path: str, dataset: str, symbol: str, date: str,
                         rows: Sequence[dict]) -> list[str]:
    """The whole-DAY registry parity check (the sealed-partition oracle). A thin wrapper over
    :func:`_registry_mismatches_in_range` passing the partition's UTC-day bounds."""
    start_ns, end_ns = _day_bounds_ns(date)
    return _registry_mismatches_in_range(registry_path, dataset, symbol, start_ns, end_ns,
                                         rows, scope=date)


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


def _merge_tables_to_file(tables: Sequence["pa.Table"], out_path: str) -> tuple[int, int]:
    """Concat + cursor-sort ``tables``, write ``out_path`` atomically, return ``(rows_before,
    rows_after)``.

    Writes a ``.tmp`` sibling, verifies ``rows in == rows out`` (raises ``ValueError`` and
    removes the tmp on a mechanical mismatch — the SOURCE originals are untouched, so the caller
    can leave them in place), then ``os.replace`` promotes it. The caller deletes the source
    originals ONLY AFTER this returns (replace-then-delete: a crash in the gap leaves a valid
    compact file beside its originals — a duplicate that self-heals on re-read/re-run — rather
    than originals-gone next to an unpromoted ``.tmp`` nothing reads)."""
    merged = pa.concat_tables(tables)
    if _CURSOR_FIELD in merged.schema.names:
        merged = merged.sort_by(_CURSOR_FIELD)
    rows_before = merged.num_rows
    tmp_path = out_path + ".tmp"
    pq.write_table(merged, tmp_path, compression=cfg.PARQUET_COMPRESSION)
    rows_after = pq.read_metadata(tmp_path).num_rows
    if rows_after != rows_before:
        os.remove(tmp_path)
        raise ValueError(
            f"brain compaction row-count mismatch writing {out_path}: "
            f"{rows_before} in != {rows_after} out — originals left intact")
    os.replace(tmp_path, out_path)
    return rows_before, rows_after


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
    if len(files) >= 2 and tables:                       # an actual merge to perform
        out_path = os.path.join(part_dir, f"compact-migrated-{uuid4().hex}.parquet")
        _, rows_after = _merge_tables_to_file(tables, out_path)  # ValueError leaves originals
        for p in readable:                               # verified+promoted -> drop originals
            os.remove(p)

    for p in corrupt:                                    # only after the merge has landed
        _quarantine(p)

    files_after = (1 if out_path is not None else len(readable))
    return BrainCompactionResult(
        rows_before=rows_before, rows_after=rows_after, files_before=len(files),
        files_after=files_after, out_path=out_path,
        corrupt_skipped=list(corrupt), registry_mismatches=registry_mismatches)


# -- the intra-day CLOSED-HOUR compactor (TODAY's live partition) --------------
#
# Bounds TODAY's part-file fan-out before the next midnight (when #62 can seal the whole
# partition). Where #62 merges a whole sealed (symbol,date) partition, this merges CLOSED
# EVENT-TIME HOURS within the live partition and runs the FULL registry oracle PER HOUR (a
# closed hour's registry roster is complete, so per-hour COMPLETENESS catches a missing window
# event-count alone can't). It reuses the merge primitive + corruption tolerance + the oracle.
#
# THE STRADDLE RULE. ``store.write_snapshots`` splits a pass only by DATE, so a catch-up pass
# writes ONE part file spanning many event-hours. A part file is CONSUMABLE only when its MAX
# ``window_start_ns`` hour is itself closed (then every row it holds is in a closed hour);
# otherwise it straddles the open hour and is DEFERRED WHOLE — never deleted (would lose the
# open-hour rows), never partially merged (would risk a double count). A consumable file that
# shares any event-hour with a deferred file is itself deferred (fixpoint): a hour's roster must
# travel together so the per-hour completeness check is sound. A closed hour is compacted +
# audited only when no deferred file still holds a window in it.
#
# LATE WRITES are accepted as tolerance (no re-merge, no markers). ``seen_windows`` dedup
# guarantees a window_start_ns is written at most once across all part files for a
# (dataset,symbol), so a late post-watermark window for an already-compacted hour is a NEW
# part file, not a duplicate; it is swept into a SECOND ``compact-h<hour>`` file and the sealed
# one is never touched. Downstream readers dedup on (dataset,symbol,window_start_ns).
#
# NOTE: never run this and :func:`compact_partition` (whole-partition) on the SAME partition
# concurrently — the runner gate owns that exclusion (mirrors capture's maintenance docstring).


@dataclass
class ClosedHourResult:
    """One processed CLOSED hour of a partition (compacted, no-op-merged, or self-healed)."""
    hour_ns: int
    rows_before: int
    rows_after: int
    files_before: int
    files_after: int
    out_path: Optional[str] = None
    corrupt_skipped: list = field(default_factory=list)
    registry_mismatches: list = field(default_factory=list)


def compact_partition_closed_hours(
    part_dir: str,
    *,
    now_ns: int,
    registry_path: Optional[str] = None,
    watermark_ns: int = cfg.BRAIN_WATERMARK_NS,
) -> list[ClosedHourResult]:
    """Compact every CLOSED, fully-resolvable event-hour of one ``(symbol,date)`` partition.

    Returns one :class:`ClosedHourResult` per hour processed this pass (open hours and
    deferred hours yield nothing). Per hour: merge the consumable parts' rows for that hour into
    one ``compact-h<hour_ns>-<uuid>.parquet`` (a single part wholly within the hour with no prior
    compact is a no-op merge, still audited), then run the per-hour registry oracle if a
    ``registry_path`` is given. Corruption-tolerant, crash-safe (replace-then-delete), and
    idempotent (a re-run finds no fresh part rows for an already-compacted hour)."""
    part_paths = _writer_parts(part_dir)
    if not part_paths:
        return []
    dataset, symbol, date = _parse_part_dir(part_dir)
    spec = sources.SOURCES.get(dataset)

    # 1. Read every writer part (corruption-tolerant); classify by MAX event-hour. A part whose
    #    max hour is still OPEN straddles the open hour -> defer the whole file.
    by_path: dict[str, tuple] = {}                # path -> (table, file_hours:set)
    deferred_paths: set = set()
    corrupt: list[str] = []
    for p in part_paths:
        table, ok = _read_part(p)
        if not ok:
            corrupt.append(p)
            continue
        ws_list = table.column("window_start_ns").to_pylist()
        file_hours = {_hour_floor(int(ws)) for ws in ws_list}
        by_path[p] = (table, file_hours)
        if file_hours and not _is_hour_closed(max(file_hours), now_ns, watermark_ns):
            deferred_paths.add(p)

    # 2. Fixpoint deferral: a consumable file sharing ANY event-hour with a deferred file is
    #    itself deferred (its slice of that hour's roster must be swept together with the rest).
    changed = True
    while changed:
        changed = False
        deferred_hours: set = set()
        for p in deferred_paths:
            deferred_hours |= by_path[p][1]
        for p, (_t, fh) in by_path.items():
            if p not in deferred_paths and (fh & deferred_hours):
                deferred_paths.add(p)
                changed = True

    consumable = [p for p in by_path if p not in deferred_paths]

    # 3. Route consumable rows by event-hour: hour -> contributing part paths.
    hour_files: dict[int, set] = {}
    for p in consumable:
        for h in by_path[p][1]:
            hour_files.setdefault(h, set()).add(p)

    results: list[ClosedHourResult] = []
    noop_home: set = set()                            # files kept as a no-op hour's canonical home
    schema = spec.schema if spec is not None else None
    for h in sorted(hour_files):
        if not _is_hour_closed(h, now_ns, watermark_ns):
            continue                                  # defensive (consumable hours are all closed)
        src_paths = sorted(hour_files[h])

        # windows already living in an existing compact-h<h>-* (a prior pass, a crash leftover,
        # or a sealed hour receiving a late write) — never re-merged, only deduped against.
        existing_rows, existing_windows = [], set()
        for ef in sorted(pathlib.Path(part_dir).glob(f"compact-h{h}-*.parquet")):
            t, ok = _read_part(str(ef))
            if ok:
                er = t.to_pylist()
                existing_rows.extend(er)
                existing_windows |= {int(r["window_start_ns"]) for r in er}

        src_hour_rows = {p: _filter_table_to_hour(by_path[p][0], h).to_pylist()
                         for p in src_paths}
        total_hour_rows = sum(len(v) for v in src_hour_rows.values())
        new_rows, contributing = [], set()
        for p, rws in src_hour_rows.items():
            fresh = [r for r in rws if int(r["window_start_ns"]) not in existing_windows]
            if fresh:
                new_rows.extend(fresh)
                contributing.add(p)

        single = next(iter(contributing)) if len(contributing) == 1 else None
        single_wholly_in_h = single is not None and len(by_path[single][1]) == 1

        out_path: Optional[str] = None
        rows_after = total_hour_rows
        if not new_rows:
            pass                                      # self-heal: rows already in a compact-h<h>
        elif not existing_windows and single_wholly_in_h:
            noop_home.add(single)                     # one part wholly in h, no prior compact
        else:
            out_path = os.path.join(part_dir, f"compact-h{h}-{uuid4().hex}.parquet")
            _, rows_after = _merge_tables_to_file(
                [pa.Table.from_pylist(new_rows, schema=schema)], out_path)

        registry_mismatches: list[str] = []
        if registry_path is not None:
            registry_mismatches = _registry_mismatches_in_range(
                registry_path, dataset, symbol, h, h + HOUR_NS, existing_rows + new_rows,
                scope=f"{date} h{h}")
        results.append(ClosedHourResult(
            hour_ns=h, rows_before=total_hour_rows, rows_after=rows_after,
            files_before=len(src_paths),
            files_after=(1 if out_path is not None else len(src_paths)),
            out_path=out_path, corrupt_skipped=list(corrupt),
            registry_mismatches=registry_mismatches))

    # 4. Delete consumed parts (every consumable file except a no-op hour's home), then
    #    quarantine corrupt fragments (only after the merges have landed).
    for p in consumable:
        if p not in noop_home:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
    for p in corrupt:
        _quarantine(p)
    return results


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
    #: chunk-level failures (e.g. an OOM-killed subprocess) — surfaced, never a silent "0".
    chunk_failures: list[str] = field(default_factory=list)


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


# -- chunked driver for the intra-day CLOSED-HOUR compactor --------------------

def _compact_closed_hours_chunk(root: str, paths: Sequence[str], budget: int, now_ns: int,
                                registry_path: Optional[str], *,
                                watermark_ns: int = cfg.BRAIN_WATERMARK_NS) -> dict:
    """Closed-hour compact partitions until ~``budget`` hour-merges are done; marshal the
    summary. ``compacted`` counts PARTITIONS with >=1 compacted hour (mirrors #62), ``merges``
    counts compact files written (the budget unit). EVERY finding is returned (the PR #60
    lesson — an un-marshalled mismatch would surface as a silent "0"); ``failed`` flags an
    aborted chunk so the driver surfaces it rather than advancing past unaudited partitions."""
    merges = files_before = files_after = compacted = completed = 0
    mismatches: list[str] = []
    registry_mismatches: list[str] = []
    corrupt_skipped: list[str] = []
    for path in paths:
        try:
            hour_results = compact_partition_closed_hours(
                path, now_ns=now_ns, registry_path=registry_path, watermark_ns=watermark_ns)
        except ValueError as exc:        # mechanical row-count mismatch (originals intact)
            mismatches.append(str(exc))
            completed += 1
            if merges >= budget:
                break
            continue
        part_merged = False
        seen_corrupt: set = set()
        for hr in hour_results:
            files_before += hr.files_before
            files_after += hr.files_after
            registry_mismatches.extend(hr.registry_mismatches)
            for c in hr.corrupt_skipped:                 # repeated across this partition's hours
                if c not in seen_corrupt:
                    seen_corrupt.add(c)
                    corrupt_skipped.append(c)
            if hr.out_path is not None:
                merges += 1
                part_merged = True
        if part_merged:
            compacted += 1
        completed += 1
        if merges >= budget:             # finish the current partition, then stop
            break
    return {"completed": completed, "compacted": compacted, "merges": merges,
            "files_before": files_before, "files_after": files_after,
            "mismatches": mismatches, "registry_mismatches": registry_mismatches,
            "corrupt_skipped": corrupt_skipped, "failed": False}


def _inprocess_closed_hours_chunk_runner():
    """A ``chunk_runner`` that runs :func:`_compact_closed_hours_chunk` IN-PROCESS (tests)."""
    def _run(root, paths, budget, now_ns, registry_path):
        return _compact_closed_hours_chunk(root, list(paths), budget, now_ns, registry_path)
    return _run


def _run_closed_hours_chunk_subprocess(root: str, paths: Sequence[str], budget: int,
                                       now_ns: int, registry_path: Optional[str], *,
                                       watermark_ns: int = cfg.BRAIN_WATERMARK_NS) -> dict:
    """Run one closed-hour chunk in a FRESH subprocess (the memory reset). Partition paths go
    over stdin. Unlike the sealed driver, a failed chunk returns ``failed=True`` (not a silent
    completed=0) so the driver SURFACES it; the driver still advances by 1 so a single bad
    partition cannot wedge the run."""
    proc = subprocess.run(
        [sys.executable, "-m", "crypto.research.brain._compact_closed_hours_chunk_worker",
         root, str(int(budget)), str(int(now_ns)), str(int(watermark_ns)), registry_path or ""],
        input="\n".join(paths), capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        logger.error("brain closed-hour compaction chunk subprocess failed (rc=%s): %s",
                     proc.returncode, (proc.stderr or "")[-500:])
        return {"completed": 0, "compacted": 0, "merges": 0, "files_before": 0,
                "files_after": 0, "mismatches": [], "registry_mismatches": [],
                "corrupt_skipped": [], "failed": True}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def compact_brain_closed_hours_chunked(
    root: str,
    *,
    datasets: Sequence[str],
    merges_per_chunk: int = cfg.BRAIN_PASS_BATCH_SIZE,
    now_ns: Optional[int] = None,
    registry_path: Optional[str] = None,
    watermark_ns: int = cfg.BRAIN_WATERMARK_NS,
    chunk_runner=None,
) -> BrainCompactionReport:
    """Closed-hour-compact every TODAY-dated ``(symbol,date)`` brain partition in
    subprocess-bounded chunks (peak RSS bounded by RUN SIZE).

    Scans ``date == today`` only: the closed-hour gate already bounds correctness, and
    ``date < today`` is the SEALED compactor's (:func:`compact_brain_chunked`) domain — keeping
    the two scans disjoint enforces the never-run-both-on-one-partition rule. ``chunk_runner``
    has the fixed signature ``(root, paths, budget, now_ns, registry_path) -> dict``; the
    default isolates each chunk in a subprocess (with ``watermark_ns`` baked in). Registry
    mismatches AND chunk failures are marshalled through every chunk into the report."""
    if chunk_runner is None:
        def chunk_runner(root, paths, budget, now_ns, registry_path):
            return _run_closed_hours_chunk_subprocess(
                root, paths, budget, now_ns, registry_path, watermark_ns=watermark_ns)
    now_ns = now_ns if now_ns is not None else int(time.time() * 1_000_000_000)
    today = _date_str(now_ns // 1_000_000)
    paths = [p for p in _list_partitions(root, datasets)
             if _parse_part_dir(p)[2] == today]
    report = BrainCompactionReport()
    i = chunks = 0
    while i < len(paths):
        res = chunk_runner(root, paths[i:], merges_per_chunk, now_ns, registry_path)
        report.partitions_scanned += int(res["completed"])
        report.partitions_compacted += int(res.get("compacted", 0))
        report.files_before += int(res["files_before"])
        report.files_after += int(res["files_after"])
        report.mismatches.extend(res.get("mismatches", []))
        report.registry_mismatches.extend(res.get("registry_mismatches", []))
        report.corrupt_skipped.extend(res.get("corrupt_skipped", []))
        if res.get("failed"):
            report.chunk_failures.append(
                f"closed-hour compaction chunk failed at partition index {i} (advanced 1 to "
                f"avoid wedge; {len(paths) - i} partitions remained)")
        i += max(int(res["completed"]), 1)               # always advance -> no infinite loop
        chunks += 1
    logger.info(
        "brain closed-hour compaction: scanned %d today partitions, compacted %d over %d "
        "subprocess chunks (budget %d), files %d -> %d, registry-mismatches %d, "
        "corrupt-skipped %d, chunk-failures %d",
        report.partitions_scanned, report.partitions_compacted, chunks, merges_per_chunk,
        report.files_before, report.files_after, len(report.registry_mismatches),
        len(report.corrupt_skipped), len(report.chunk_failures))
    return report
