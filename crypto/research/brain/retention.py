"""Brain-store retention: bound the store to what discovery needs.

The brain store had NO time bound while every capture dataset does, so it grew
unbounded (35G+ parquet, a ~9.5G registry). Two independent windows (see config):

  * PARQUET (labels + primitive datasets) — whole ``date=`` partitions older than
    ``BRAIN_STORE_RETENTION_DAYS`` expire, oldest-first, never today's.
  * REGISTRY ``snapshot_bookkeeping`` — rows older than ``BRAIN_REGISTRY_RETENTION_DAYS``
    are DELETEd (shorter window: bookkeeping serves only the tick's write-dedup, never
    discovery), then the file is reclaimed by a free-space-GUARDED VACUUM.

Filesystem + registry only; never opens DuckDB, the engine DB, or the capture store.
Mirrors ``capture_core.maintenance.expire_firehose_partitions`` (the house idiom).
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from crypto.research.brain import config as cfg

logger = logging.getLogger("mhde.crypto.brain.retention")

_DAY_MS = 86_400_000
_DAY_NS = 86_400 * 1_000_000_000


def brain_datasets() -> list[str]:
    """Canonical brain store datasets: the primitive sources + the labels dataset.
    Sourced from the specs so a newly-added source is retained automatically."""
    from crypto.research.brain import labels, sources
    primitives = sorted({spec.dataset for spec in sources.SOURCES.values()})
    return primitives + [labels.LABEL_DATASET]


def _date_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _list_date_partitions(root: str, datasets: Sequence[str]) -> list[tuple[str, str]]:
    """``(date, path)`` for every ``<root>/<dataset>/symbol=*/date=*`` dir in ``datasets``."""
    out: list[tuple[str, str]] = []
    base = pathlib.Path(root)
    for ds in datasets:
        ds_dir = base / ds
        if not ds_dir.is_dir():
            continue
        for sym_dir in ds_dir.glob("symbol=*"):
            for date_dir in sym_dir.glob("date=*"):
                if date_dir.is_dir():
                    out.append((date_dir.name.split("date=", 1)[-1], str(date_dir)))
    return out


def expire_brain_partitions(store_root: str, *, days: int, datasets: Sequence[str],
                            now_ms: Optional[int] = None) -> list[str]:
    """Delete brain store ``date=`` partitions older than ``days`` across ``datasets``.

    Keeps partitions whose date is >= the cutoff (now - days), so today's is always kept;
    removes older ones oldest-first. Filesystem-only. Returns the removed dirs."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = _date_str(now_ms - days * _DAY_MS)
    parts = _list_date_partitions(store_root, datasets)
    removed: list[str] = []
    for date, path in sorted(parts):                 # oldest-first (ISO dates sort lexically)
        if date < cutoff:
            shutil.rmtree(path)
            removed.append(path)
    if removed:
        logger.info("brain retention: expired %d partitions older than %s",
                    len(removed), cutoff)
    return removed


def prune_registry_bookkeeping(registry_path: str, *, days: int,
                               now_ns: Optional[int] = None) -> int:
    """DELETE ``snapshot_bookkeeping`` rows with ``window_start_ns`` older than ``days``.

    ``reader_cursor`` is NEVER touched (it is the monotonic cursor, tiny). Returns the
    number of rows deleted. No-op if the registry is missing."""
    if not os.path.exists(registry_path):
        return 0
    now_ns = now_ns if now_ns is not None else time.time_ns()
    cutoff_ns = now_ns - days * _DAY_NS
    conn = sqlite3.connect(registry_path)
    try:
        cur = conn.execute(
            "DELETE FROM snapshot_bookkeeping WHERE window_start_ns < ?", (cutoff_ns,))
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    if deleted:
        logger.info("brain retention: pruned %d bookkeeping rows older than %d days",
                    deleted, days)
    return deleted


def _free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def vacuum_registry_if_space(registry_path: str, *, headroom_factor: float = 1.2,
                             min_bloat_ratio: float = 0.20,
                             free_fn: Callable[[str], int] = _free_bytes,
                             ) -> tuple[bool, str]:
    """Reclaim pages freed by the prune via VACUUM — but ONLY when it is worth it AND the
    volume has room. Two gates, in order:

      * BLOAT — a full VACUUM rewrites the whole file under an EXCLUSIVE lock (stalling the
        live tick for its duration), so it only pays off when the file is actually
        fragmented. SQLite reuses freed pages for new inserts, so in steady state the
        freelist stays small (daily deletes and inserts balance) and we SKIP. VACUUM only
        when ``freelist_count / page_count >= min_bloat_ratio`` — i.e. after a big backlog
        prune, not every day.
      * SPACE — a full VACUUM transiently needs ~1x the DB size free; require
        ``headroom_factor``x, else skip. The DELETE already freed pages for reuse so growth
        stays bounded without it.
    Returns ``(vacuumed, reason)``."""
    if not os.path.exists(registry_path):
        return False, "registry missing"
    conn = sqlite3.connect(registry_path)
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        bloat = (freelist / page_count) if page_count else 0.0
        if bloat < min_bloat_ratio:
            reason = (f"skipped VACUUM: bloat {bloat:.0%} < {min_bloat_ratio:.0%} "
                      f"(registry not fragmented enough to rewrite)")
            logger.info("brain retention: %s", reason)
            return False, reason
        db_size = os.path.getsize(registry_path)
        free = free_fn(os.path.dirname(registry_path) or ".")
        need = int(db_size * headroom_factor)
        if free < need:
            reason = (f"skipped VACUUM: free {free / 1e9:.1f}G < needed {need / 1e9:.1f}G "
                      f"({headroom_factor}x the {db_size / 1e9:.1f}G registry)")
            logger.warning("brain retention: %s", reason)
            return False, reason
        conn.execute("VACUUM")
    finally:
        conn.close()
    logger.info("brain retention: VACUUMed registry (bloat %.0f%%, %.1fG)",
                bloat * 100, db_size / 1e9)
    return True, "vacuumed"


def run_retention(store_root: Optional[str] = None, registry_path: Optional[str] = None,
                  *, store_days: Optional[int] = None, registry_days: Optional[int] = None,
                  datasets: Optional[Sequence[str]] = None, now_ms: Optional[int] = None,
                  free_fn: Callable[[str], int] = _free_bytes,
                  vacuum_min_bloat_ratio: float = 0.20) -> dict:
    """Expire parquet partitions -> prune registry bookkeeping -> VACUUM (space-guarded).
    Defaults come from config. Returns a summary dict."""
    store_root = store_root if store_root is not None else cfg.BRAIN_STORE_ROOT
    registry_path = registry_path if registry_path is not None else cfg.BRAIN_REGISTRY_PATH
    store_days = store_days if store_days is not None else cfg.BRAIN_STORE_RETENTION_DAYS
    registry_days = (registry_days if registry_days is not None
                     else cfg.BRAIN_REGISTRY_RETENTION_DAYS)
    datasets = list(datasets) if datasets is not None else brain_datasets()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    now_ns = now_ms * 1_000_000

    removed = expire_brain_partitions(store_root, days=store_days, datasets=datasets,
                                      now_ms=now_ms)
    pruned = prune_registry_bookkeeping(registry_path, days=registry_days, now_ns=now_ns)
    vacuumed, reason = vacuum_registry_if_space(
        registry_path, free_fn=free_fn, min_bloat_ratio=vacuum_min_bloat_ratio)
    return {
        "partitions_expired": len(removed),
        "bookkeeping_pruned": pruned,
        "vacuumed": vacuumed,
        "vacuum_reason": reason,
    }
