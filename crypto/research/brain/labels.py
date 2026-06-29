"""Forward-path LABEL STORE (the label factory).

Reads the brain ``markprice`` primitive FORWARD and computes the exit-agnostic
forward-path label per ``(symbol, window_start_ns, horizon)``, writing a TALL,
append-only store. This is OUTCOME information, not a signal: three numbers and a
validity bit, nothing that bakes in an exit rule, threshold, or strategy.

For an entry window ``t`` (window on the 60s grid; ``ref = mark_close[t]``) and a
horizon of ``H`` minutes == ``H`` windows forward:

    return = mark_close[t+H] / ref - 1
    MFE    = max(mark_high[t+1 .. t+H]) / ref - 1        (t+1 .. t+H INCLUSIVE)
    MAE    = min(mark_low [t+1 .. t+H]) / ref - 1

``horizons_min`` defaults to ``[5,15,30,60,120,240,480,720]``.

THREE invariants, each load-bearing and each pinned by a test:

  * FORWARD-ONLY SETTLEMENT — a ``(symbol, window, H)`` record is emitted ONLY once
    ``window_end_ns + H*60s <= frontier``, where ``frontier`` is the latest settled
    markprice window's ``window_end_ns`` (``MAX(window_end_ns) WHERE dataset='markprice'``
    in the registry's ``snapshot_bookkeeping``). This is a SEPARATE watermark from the
    90s feature one; nothing un-settled is ever written.

  * PER-HORIZON VALIDITY — a horizon ``H`` is INVALID if a FLEET-WIDE markPrice gap
    overlaps its forward span ``[window_end_ns, window_end_ns + H*60s]``. Mark gaps are
    recorded under the array stream whose name STARTS WITH ``!markPrice@arr`` (the full
    ``!markPrice@arr@<speed>`` string lands in both the symbol and stream columns — see
    capture ``service._on_gap``), so the gap is symbol-less and an overlap invalidates
    that ``(window, H)`` for EVERY symbol. Per-horizon, not whole-window. UNITS: ``_gaps``
    is MILLISECONDS, windows are NANOSECONDS — converted before any comparison.

    Validity is additionally False when the forward span is INCOMPLETE in the markprice
    store (a missing window). Missing data is treated exactly like a gap — never allowed
    to look like a quiet window and get a falsely-valid label (the no-bias / "a skipped
    fragment is a gap" requirement). This clause is a hardening beyond the locked five
    validity cases; see the module's test (f).

  * IDEMPOTENT APPEND — re-running appends only newly-settled records and never
    duplicates an existing ``(symbol, window, H)``. The store's parquet write path has no
    primary key, so settled keys are tracked in this module's OWN ``label_bookkeeping``
    table (PK ``(symbol, window_start_ns, horizon_min)`` — the tall key, which the
    registry's window-keyed ``snapshot_bookkeeping`` cannot express). Write-then-record
    ordering (parquet first, bookkeeping second) matches the pipeline house pattern.
    Dedup is a BOOKKEEPING-layer guarantee — the parquet path has no primary key, so a
    crash strictly between the write and the record can leave a duplicate physical row
    that a re-run re-appends; ``label_bookkeeping`` (and readers) are the dedup authority,
    exactly as in ``pipeline.py``. A normal re-run is fully idempotent via ``_seen_labels``.

This module writes ONLY under its label store root and the registry file; it NEVER opens
DuckDB, the engine DB, or capture's store for writing.
"""
from __future__ import annotations

import pathlib
import sqlite3
from typing import Mapping, Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from crypto.research.brain import registry
from crypto.research.brain import store

#: Per-record horizons (minutes == windows on the 60s grid). The plural config knob
#: ``horizons_min`` selects which to materialize; ``horizon_min`` is the per-row key.
HORIZONS_MIN = [5, 15, 30, 60, 120, 240, 480, 720]

#: The brain dataset the labels are written under, and the markprice source dataset.
LABEL_DATASET = "labels"
MARKPRICE_DATASET = "markprice"

_MIN_NS = 60_000_000_000          # 60s grid == 1 window == 1 minute
_MS_TO_NS = 1_000_000
#: The 1-day margin the markprice DATE-prune already applies (``store._DATE_PRUNE_MARGIN_NS``).
#: The window_end pushdown (Fix 2b) must subtract the SAME margin, so it never drops a window the
#: date-prune keeps (a recv just over a UTC midnight sits in the prior day's partition — kept by
#: the margin and still labelable on the run that first sees it). Pushing at the bare edge would
#: drop those within-margin windows and lose their first-seen labels (the no-bias contract,
#: test_brain_labels (f)). With the margin the pushdown is byte-identical to the date-prune in
#: every regime the continuous runner operates — it only trims windows already >1 day below the
#: floor (every horizon settled AND written on a prior tick), exactly the date-prune's documented
#: steady-state bound.
_READ_MARGIN_NS = 86_400 * 1_000_000_000

#: Mark gaps are recorded under ``!markPrice@arr@<speed>``; match by this PREFIX so the
#: speed suffix (MARKPRICE_SPEED, default "1s") never silently defeats the filter.
MARK_GAP_STREAM_PREFIX = "!markPrice@arr"

#: TALL label schema: the shared provenance/bounds prefix + the tall key + the label.
#: ``valid`` is the only bool in a brain store schema, matching capture's own ``valid``
#: flag convention. NOTHING engineered (no exit/threshold/strategy/signal) — pinned by
#: ``test_label_schema_carries_only_path_label_and_provenance``.
LABEL_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),       # provenance high-water (entry window)
    ("symbol", pa.string()),
    ("window_start_ns", pa.int64()),  # immutable bound (entry-window floor)
    ("window_end_ns", pa.int64()),    # immutable bound (entry-window ceiling)
    ("horizon_min", pa.int64()),      # tall key
    ("fwd_return", pa.float64()),
    ("mfe", pa.float64()),
    ("mae", pa.float64()),
    ("valid", pa.bool_()),
])

_LABEL_BOOKKEEPING_SCHEMA = """
CREATE TABLE IF NOT EXISTS label_bookkeeping (
    symbol          TEXT    NOT NULL,
    window_start_ns INTEGER NOT NULL,
    horizon_min     INTEGER NOT NULL,
    window_end_ns   INTEGER NOT NULL,
    valid           INTEGER NOT NULL,
    written_at_ns   INTEGER NOT NULL,
    PRIMARY KEY (symbol, window_start_ns, horizon_min)
);
"""


# -- pure label math ------------------------------------------------------------

def label_for_horizon(
    mark_by_window: Mapping[int, Mapping[str, object]],
    t_start_ns: int,
    ref_close: float,
    h_min: int,
) -> dict:
    """Forward-path label for entry window ``t_start_ns`` at horizon ``h_min`` minutes.

    ``mark_by_window`` maps ``window_start_ns -> markprice snapshot``. Best-effort over
    the windows actually present: a metric is ``None`` if its inputs are absent (the
    caller decides validity — an incomplete span is invalidated upstream).
    """
    exit_snap = mark_by_window.get(t_start_ns + h_min * _MIN_NS)
    fwd_return = (
        exit_snap["mark_close"] / ref_close - 1.0 if exit_snap is not None else None
    )
    highs, lows = [], []
    for k in range(1, h_min + 1):                       # t+1 .. t+H INCLUSIVE
        snap = mark_by_window.get(t_start_ns + k * _MIN_NS)
        if snap is not None:
            highs.append(snap["mark_high"])
            lows.append(snap["mark_low"])
    mfe = max(highs) / ref_close - 1.0 if highs else None
    mae = min(lows) / ref_close - 1.0 if lows else None
    return {"fwd_return": fwd_return, "mfe": mfe, "mae": mae}


def _forward_complete(mark_by_window: Mapping[int, object], t_start_ns: int, h_min: int) -> bool:
    """True iff every forward window ``t+1 .. t+H`` is present in the markprice store."""
    return all(
        (t_start_ns + k * _MIN_NS) in mark_by_window for k in range(1, h_min + 1)
    )


def _overlaps_gap(span_start_ns: int, span_end_ns: int, gaps: Sequence[tuple]) -> bool:
    """Inclusive interval overlap of ``[span_start, span_end]`` against any gap (ns)."""
    return any(gs <= span_end_ns and ge >= span_start_ns for gs, ge in gaps)


# -- registry frontier + fleet gap intervals ------------------------------------

def _markprice_frontier_ns(conn: sqlite3.Connection) -> Optional[int]:
    """Latest settled markprice ``window_end_ns`` (fleet-wide). ``None`` if none yet."""
    row = conn.execute(
        "SELECT MAX(window_end_ns) FROM snapshot_bookkeeping WHERE dataset = ?",
        (MARKPRICE_DATASET,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _markprice_gap_intervals_ns(capture_root: str) -> list[tuple]:
    """Fleet-wide markPrice gap intervals as ``(start_ns, end_ns)`` (converted from the
    manifest's ms). Reads each ``_gaps`` fragment via ``ParquetFile`` (no Hive column
    injection) and tolerates an unreadable fragment by skipping it (a corrupt manifest
    fragment must never crash the label build)."""
    base = pathlib.Path(capture_root, "_gaps")
    if not base.exists() or not any(base.rglob("*.parquet")):
        return []
    intervals: list[tuple] = []
    for fp in sorted(base.rglob("*.parquet")):
        try:
            table = pq.ParquetFile(str(fp)).read(
                columns=["stream", "gap_start_ms", "gap_end_ms"])
        except (pa.ArrowInvalid, OSError):
            continue
        for r in table.to_pylist():
            if str(r["stream"]).startswith(MARK_GAP_STREAM_PREFIX):
                intervals.append(
                    (int(r["gap_start_ms"]) * _MS_TO_NS, int(r["gap_end_ms"]) * _MS_TO_NS))
    return intervals


def _seen_labels(conn: sqlite3.Connection, symbol: str) -> set:
    """``{(window_start_ns, horizon_min)}`` already settled+written for ``symbol``."""
    return {
        (int(w), int(h))
        for w, h in conn.execute(
            "SELECT window_start_ns, horizon_min FROM label_bookkeeping WHERE symbol = ?",
            (symbol,),
        )
    }


def _record_labels(conn: sqlite3.Connection, rows: Sequence[tuple]) -> None:
    """INSERT OR IGNORE the settled tall keys (re-seen keys are no-ops)."""
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO label_bookkeeping "
            "(symbol, window_start_ns, horizon_min, window_end_ns, valid, written_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


# -- the forward-only, idempotent build -----------------------------------------

def run_once(
    *,
    store_root: str,
    capture_root: str,
    registry_path: str,
    label_store_root: Optional[str] = None,
    horizons_min: Sequence[int] = HORIZONS_MIN,
    symbols: Optional[Sequence[str]] = None,
    now_ns: int = 0,
    bound_reads: bool = True,
) -> list[dict]:
    """Materialize newly-settled forward-path labels; return the records written.

    Reads markprice snapshots from ``store_root`` and the settlement frontier +
    label bookkeeping from ``registry_path``; consults fleet markPrice gaps under
    ``capture_root``; appends label rows under ``label_store_root`` (defaults to
    ``store_root``). Idempotent: only ``(symbol, window, H)`` keys that are both
    settled and not previously recorded are emitted.

    LABEL-READ BOUND (``bound_reads=True``, the default): only markprice windows that can
    STILL settle are read. A window with ``window_end <= frontier - max(horizons_min)`` has
    every horizon already settled (and recorded in ``label_bookkeeping``), so it yields no new
    labels; the oldest window that can still settle is exactly at ``window_end = frontier -
    max(horizons_min)``. That edge is passed as :func:`store.read_snapshots`' date-prune floor.
    ``read_snapshots`` ALREADY widens the floor by its own 1-day margin
    (``store._DATE_PRUNE_MARGIN_NS``), so we pass the edge itself — neither doubling that margin
    nor under-shooting it (a naive recv cursor near the frontier would drop the whole
    still-settling backlog and silently lose labels). ``bound_reads=False`` restores the prior
    unbounded full-history read (regression escape hatch). The bound assumes the steady-state
    continuous runner: a window older than the floor that was somehow never labeled (e.g. the
    runner down longer than ``max(horizons_min)``) is intentionally not back-labeled.
    """
    label_store_root = label_store_root or store_root
    conn = registry.connect(str(registry_path))
    conn.executescript(_LABEL_BOOKKEEPING_SCHEMA)
    conn.commit()
    try:
        frontier = _markprice_frontier_ns(conn)
        if frontier is None:
            return []
        gaps = _markprice_gap_intervals_ns(capture_root)

        # The date-prune floor: at-or-below the oldest still-settling window's edge. 0 disables
        # the prune (full read) — both when bound_reads is off and when there is no horizon.
        read_floor_ns = 0
        if bound_reads and horizons_min:
            read_floor_ns = frontier - max(horizons_min) * _MIN_NS
        # The window_end ROW pushdown floor: the date-prune edge minus the SAME 1-day margin, so
        # it can never drop a window the date-prune keeps (see _READ_MARGIN_NS). 0 -> no pushdown.
        window_end_floor_ns = max(0, read_floor_ns - _READ_MARGIN_NS) if read_floor_ns else 0

        if symbols is None:
            # DISCOVERY (Fix 2a): enumerate the markprice symbol= dirs (a directory listing)
            # instead of reading the WHOLE store to harvest the symbol set — the first
            # UTC-midnight crossing would otherwise materialize all ~812 symbols × multi-day
            # toward the 2G cap. A symbol with only below-floor data is listed but yields no new
            # label (its per-symbol read below is floor-bounded -> empty), so the output is
            # unchanged from the read-based discovery.
            symbols = store.list_symbols(str(store_root), MARKPRICE_DATASET)

        written: list[dict] = []
        for symbol in symbols:
            # (Fix 2b) push the same edge as a window_end >= floor ROW filter: a window below the
            # floor has every horizon settled+written, and is never a forward window of a
            # still-settling entry (those sit at window_end >= floor), so dropping it pre-python
            # changes no label. read_floor_ns is 0 when bound_reads is off -> a no-op pushdown.
            snaps = store.read_snapshots(str(store_root), MARKPRICE_DATASET, symbol,
                                         after_recv_ts_ns=read_floor_ns,
                                         window_end_floor_ns=window_end_floor_ns)
            if not snaps:
                continue
            mark_by_window = {int(s["window_start_ns"]): s for s in snaps}
            seen = _seen_labels(conn, symbol)
            new_rows: list[dict] = []
            book_rows: list[tuple] = []
            for t_start in sorted(mark_by_window):
                snap = mark_by_window[t_start]
                window_end = int(snap["window_end_ns"])
                ref_close = snap["mark_close"]
                for h in horizons_min:
                    if (t_start, int(h)) in seen:
                        continue                                  # idempotent: skip written
                    span_end = window_end + h * _MIN_NS
                    if span_end > frontier:
                        continue                                  # not settled yet
                    label = label_for_horizon(mark_by_window, t_start, ref_close, h)
                    gap_ok = not _overlaps_gap(window_end, span_end, gaps)
                    complete = _forward_complete(mark_by_window, t_start, h)
                    valid = bool(gap_ok and complete)
                    new_rows.append({
                        "recv_ts_ns": int(snap["recv_ts_ns"]),
                        "symbol": symbol,
                        "window_start_ns": t_start,
                        "window_end_ns": window_end,
                        "horizon_min": int(h),
                        "fwd_return": label["fwd_return"],
                        "mfe": label["mfe"],
                        "mae": label["mae"],
                        "valid": valid,
                    })
                    book_rows.append(
                        (symbol, t_start, int(h), window_end, int(valid), int(now_ns)))
            if new_rows:
                # Write-then-record: parquet lands before the key is marked settled.
                store.write_snapshots(
                    str(label_store_root), LABEL_DATASET, LABEL_SCHEMA, new_rows)
                _record_labels(conn, book_rows)
                written.extend(new_rows)
        return written
    finally:
        conn.close()
