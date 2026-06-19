"""Brain pipeline (Phase 1): one INERT pass over the capture tape, generic over a
:class:`sources.SourceSpec`.

Orchestrates the vertical slice for ANY source — read new rows past that source's
cursor, summarize the SETTLED windows into raw primitives, persist them to the
source's brain store dataset, and advance the cursor + bookkeeping atomically. No
continuous runner, no systemd: :func:`run_once` does exactly one pass and returns.

Settled-window watermark
------------------------
A window ``[w, w+cadence)`` is *settled* once ``window_end_ns <= now - watermark``
(watermark = capture flush interval + one window), which guarantees every
trailing trade of the window has been flushed to disk. Only settled windows are
emitted; the rest are left for a later pass.

Gap-free, no-double-count cursor
--------------------------------
Rows split into ``settled`` and ``pending`` by their window. The cursor advances
to ``min(max recv among settled, min recv among pending - 1)`` so every pending
row stays strictly above the cursor (re-read next pass -> no gap), while settled
rows below it are never re-read (no double count). The registry's per-window
bookkeeping (``INSERT OR IGNORE`` on ``(dataset, symbol, window_start)``) is the
backstop: a window already recorded is skipped, so even a re-read settled row
cannot double-count.

Isolation: reads capture READ-ONLY; writes only the brain store + registry.
"""
from __future__ import annotations

import pathlib
from typing import Iterable, NamedTuple, Optional, Sequence

from crypto.research.brain import config as cfg
from crypto.research.brain import registry, store

_MS_TO_NS = 1_000_000


def _window_end_ns(event_time_ms: int, cadence_ns: int) -> int:
    t_ns = event_time_ms * _MS_TO_NS
    return (t_ns // cadence_ns) * cadence_ns + cadence_ns


class _Slice(NamedTuple):
    """The outcome of summarizing+persisting one slice of rows, minus the cursor."""
    max_settled: Optional[int]   # max recv among settled rows (None if none)
    min_pending: Optional[int]   # min recv among pending rows (None if none)
    settled_n: int
    pending_n: int
    fresh: list                  # newly-written snapshots (deduped vs seen windows)
    files: list                  # parquet paths written
    bookkeeping: list            # bookkeeping rows for the fresh windows


def _process_slice(conn, spec, rows, *, horizon_ns, cadence_ns, store_root) -> _Slice:
    """Settle/bucketize/dedup/WRITE one slice of rows; return its cursor extremes +
    bookkeeping. Does NOT touch the cursor — the caller decides when to advance."""
    settled, pending = [], []
    for r in rows:
        window_end = _window_end_ns(r[spec.event_time_key], cadence_ns)
        (settled if window_end <= horizon_ns else pending).append(r)

    snapshots = spec.bucket_fn(settled, cadence_ns=cadence_ns)
    seen_by_symbol: dict[str, set] = {}
    fresh = []
    for snap in snapshots:
        sym = snap["symbol"]
        if sym not in seen_by_symbol:
            seen_by_symbol[sym] = registry.seen_windows(conn, spec.dataset, sym)
        if snap["window_start_ns"] not in seen_by_symbol[sym]:
            fresh.append(snap)

    files = store.write_snapshots(store_root, spec.dataset, spec.schema, fresh)
    bookkeeping = [
        {
            "dataset": spec.dataset, "symbol": s["symbol"],
            "window_start_ns": s["window_start_ns"], "window_end_ns": s["window_end_ns"],
            "recv_ts_ns": s["recv_ts_ns"], "n_events": spec.count_fn(s),
        }
        for s in fresh
    ]
    return _Slice(
        max_settled=max((r["recv_ts_ns"] for r in settled), default=None),
        min_pending=min((r["recv_ts_ns"] for r in pending), default=None),
        settled_n=len(settled), pending_n=len(pending),
        fresh=fresh, files=files, bookkeeping=bookkeeping,
    )


def _next_cursor(max_settled, min_pending, cursor_before: int) -> int:
    """Gap-free, monotonic cursor over a slice's (or a whole pass's) settled/pending.

    Never skips a pending row (``<= min_pending - 1``); never regresses below
    ``cursor_before``. Settled windows are recorded in bookkeeping regardless, so a
    re-read above the cursor is a deduped no-op (never a double count)."""
    if max_settled is None:
        new_cursor = cursor_before
    elif min_pending is None:
        new_cursor = max_settled
    else:
        new_cursor = min(max_settled, min_pending - 1)
    return max(new_cursor, cursor_before)


def _merge_opt(fn, acc, value):
    """Fold ``value`` into the running ``acc`` with ``fn`` (min/max), skipping None."""
    if value is None:
        return acc
    return value if acc is None else fn(acc, value)


def _batched(seq: Sequence[str], size: int) -> Iterable[list]:
    items = list(seq)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _enumerate_universe(capture_root: str, capture_dataset: str) -> list[str]:
    """The symbol universe present on disk for ``capture_dataset`` — the values of the
    Hive ``symbol=`` partition dirs (a directory listing, no parquet opened). UTF-8
    safe (CJK / digit-leading symbols), returned sorted for deterministic batching."""
    base = pathlib.Path(capture_root, capture_dataset)
    if not base.exists():
        return []
    prefix = "symbol="
    return sorted(d.name[len(prefix):] for d in base.glob(f"{prefix}*") if d.is_dir())


def run_once(
    spec,
    *,
    capture_root: str,
    store_root: str,
    registry_path: str,
    now_ns: int,
    cadence_ns: int = cfg.BRAIN_BASE_CADENCE_NS,
    watermark_ns: int = cfg.BRAIN_WATERMARK_NS,
    symbols: Optional[Sequence[str]] = None,
) -> dict:
    """Run one pass for ``spec`` (a :class:`sources.SourceSpec`).

    Returns a summary dict (counts + cursor before/after). The source supplies
    its reader, primitive, store schema, bucket field, and event-count function;
    the settled-window watermark + gap-free cursor logic is identical for all.
    """
    conn = registry.connect(registry_path)
    try:
        cursor_before = registry.get_cursor(conn, spec.reader_name)
        rows = spec.read_fn(capture_root, after_recv_ts_ns=cursor_before, symbols=symbols)
        sl = _process_slice(conn, spec, rows, horizon_ns=now_ns - watermark_ns,
                            cadence_ns=cadence_ns, store_root=store_root)
        new_cursor = _next_cursor(sl.max_settled, sl.min_pending, cursor_before)
        registry.advance(conn, spec.reader_name, new_recv_ts_ns=new_cursor,
                         bookkeeping=sl.bookkeeping, now_ns=now_ns)
        return {
            "rows_read": len(rows),
            "settled_rows": sl.settled_n,
            "pending_rows": sl.pending_n,
            "snapshots_written": len(sl.fresh),
            "files_written": len(sl.files),
            "cursor_before": cursor_before,
            "cursor_after": registry.get_cursor(conn, spec.reader_name),
        }
    finally:
        conn.close()


def run_pass(
    spec,
    *,
    capture_root: str,
    store_root: str,
    registry_path: str,
    now_ns: int,
    cadence_ns: int = cfg.BRAIN_BASE_CADENCE_NS,
    watermark_ns: int = cfg.BRAIN_WATERMARK_NS,
    symbols: Optional[Sequence[str]] = None,
    batch_size: int = cfg.BRAIN_PASS_BATCH_SIZE,
) -> dict:
    """One MEMORY-SAFE full-universe pass for ``spec``: process the symbol universe in
    batches of ``batch_size``, each batch a bounded (symbol + date-pruned) read, so
    peak memory is one batch rather than every symbol's slice at once.

    The per-source cursor is the load-bearing invariant: EVERY batch is read at the
    SAME ``cursor_before``, and the cursor advances exactly ONCE after all batches, to
    the GLOBAL frontier over the union of all batches' settled/pending rows. Advancing
    between batches would make a later batch (a different symbol set) read past its own
    unprocessed rows — a silent permanent gap, since ``seen_windows`` is per-symbol.
    Each batch records its settled windows immediately (:func:`registry.record_windows`,
    no cursor move), so a mid-pass crash (cursor unmoved) re-does the pass and the
    completed batches dedup via ``seen_windows`` — no duplicate primitives.

    ``symbols=None`` enumerates the universe from the capture ``symbol=`` partitions;
    an explicit list batches exactly those. Returns a summary dict.
    """
    conn = registry.connect(registry_path)
    try:
        cursor_before = registry.get_cursor(conn, spec.reader_name)
        universe = (list(symbols) if symbols is not None
                    else _enumerate_universe(capture_root, spec.capture_dataset))
        horizon_ns = now_ns - watermark_ns

        g_max_settled = g_min_pending = None
        rows_read = settled_rows = pending_rows = snapshots_written = files_written = 0
        n_batches = 0
        for batch in _batched(universe, batch_size):
            # SAME cursor_before for every batch (never advance mid-pass).
            rows = spec.read_fn(capture_root, after_recv_ts_ns=cursor_before, symbols=batch)
            sl = _process_slice(conn, spec, rows, horizon_ns=horizon_ns,
                                cadence_ns=cadence_ns, store_root=store_root)
            if sl.bookkeeping:  # record THIS batch now (re-run safety), cursor untouched
                registry.record_windows(conn, sl.bookkeeping, now_ns=now_ns)
            g_max_settled = _merge_opt(max, g_max_settled, sl.max_settled)
            g_min_pending = _merge_opt(min, g_min_pending, sl.min_pending)
            rows_read += len(rows)
            settled_rows += sl.settled_n
            pending_rows += sl.pending_n
            snapshots_written += len(sl.fresh)
            files_written += len(sl.files)
            n_batches += 1

        # Advance the per-source cursor ONCE, to the global frontier of the whole pass.
        new_cursor = _next_cursor(g_max_settled, g_min_pending, cursor_before)
        registry.advance(conn, spec.reader_name, new_recv_ts_ns=new_cursor,
                         bookkeeping=(), now_ns=now_ns)
        return {
            "universe_size": len(universe),
            "batches": n_batches,
            "rows_read": rows_read,
            "settled_rows": settled_rows,
            "pending_rows": pending_rows,
            "snapshots_written": snapshots_written,
            "files_written": files_written,
            "cursor_before": cursor_before,
            "cursor_after": registry.get_cursor(conn, spec.reader_name),
        }
    finally:
        conn.close()
