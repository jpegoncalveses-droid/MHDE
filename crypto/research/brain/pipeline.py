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

from typing import Optional, Sequence

from crypto.research.brain import config as cfg
from crypto.research.brain import registry, store

_MS_TO_NS = 1_000_000


def _window_end_ns(event_time_ms: int, cadence_ns: int) -> int:
    t_ns = event_time_ms * _MS_TO_NS
    return (t_ns // cadence_ns) * cadence_ns + cadence_ns


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

        horizon_ns = now_ns - watermark_ns
        event_time_key = spec.event_time_key
        settled, pending = [], []
        for r in rows:
            window_end = _window_end_ns(r[event_time_key], cadence_ns)
            (settled if window_end <= horizon_ns else pending).append(r)

        # Cursor: never skip a pending row (gap-free); never regress (monotonic).
        if settled:
            max_settled = max(r["recv_ts_ns"] for r in settled)
            if pending:
                new_cursor = min(max_settled, min(r["recv_ts_ns"] for r in pending) - 1)
            else:
                new_cursor = max_settled
        else:
            new_cursor = cursor_before
        new_cursor = max(new_cursor, cursor_before)

        # Summarize settled windows; drop any window already recorded (idempotent).
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
                "dataset": spec.dataset,
                "symbol": s["symbol"],
                "window_start_ns": s["window_start_ns"],
                "window_end_ns": s["window_end_ns"],
                "recv_ts_ns": s["recv_ts_ns"],
                "n_events": spec.count_fn(s),
            }
            for s in fresh
        ]
        registry.advance(conn, spec.reader_name, new_recv_ts_ns=new_cursor,
                         bookkeeping=bookkeeping, now_ns=now_ns)

        return {
            "rows_read": len(rows),
            "settled_rows": len(settled),
            "pending_rows": len(pending),
            "snapshots_written": len(fresh),
            "files_written": len(files),
            "cursor_before": cursor_before,
            "cursor_after": registry.get_cursor(conn, spec.reader_name),
        }
    finally:
        conn.close()
