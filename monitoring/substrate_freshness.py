"""Monitor: the brain + Binance capture substrate is still being WRITTEN.

Fires when the brain stops advancing its cursors (no new labels/primitives) or any
capture dataset stops receiving writes, measured by write RECENCY — never by
process-liveness. The 2026-08-08 outage kept every systemd unit "active" while the
firehose write-halt latched and the brain tick had died; nothing was written for
~14h and no liveness check noticed. This monitor closes that gap:

  - capture datasets  — newest ``part-*.parquet`` mtime under ``date={today,yesterday}``
  - brain liveness    — ``MAX(reader_cursor.updated_at_ns)`` in the registry (advances
                        every tick while the brain is producing; freezes when it stalls)
  - brain FRONTIER    — per-dataset ``MAX(snapshot_bookkeeping.window_end_ns)``: the
                        settlement frontier, the exact signal discovery consumes
                        (labels._markprice_frontier_ns). KI-166: during the 2026-08-15
                        stall the cursors kept advancing (updated_at_ns seconds-fresh,
                        quiet-gap skip moving last_recv) while four datasets' frontiers
                        froze for 5 days — liveness-shaped signals cannot see a
                        progress stall; only the frontier can.

Any stream stale beyond its threshold => a ``fail`` MonitorResult => Telegram (via the
throttled ``monitoring.alert.send_alert`` path). It never opens DuckDB and never calls
``systemctl``. Wired as ``main.py monitor substrate-freshness`` on a short timer.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from crypto.research.brain import config as brain_cfg
from crypto.research.capture_core import config as cap_cfg
from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.substrate_freshness")

_NS = 1_000_000_000

# WS firehose datasets flush every CAPTURE_FIREHOSE_FLUSH_S (~30s); 10 min stale is
# unambiguously broken. REST as-of datasets poll on a ~20 min cadence; 60 min stale.
# The brain tick advances cursors every ~60s; 15 min stale means it has stopped.
_WS_DATASETS = ("aggTrade", "depth", "bookTicker", "markPrice", "forceOrder")
_REST_DATASETS = ("open_interest", "premium_index", "global_ls_account",
                  "top_ls_account", "top_ls_position", "taker_ls_ratio", "basis")
# `depth_snapshot` is intentionally NOT monitored: it is the snapshot-owner's periodic
# full-book REST snapshot, written on an irregular, budget-gated cadence that a fixed
# freshness threshold would false-positive on. Write-liveness for the order book is
# already covered by the `depth` diff stream above (if diffs stop, capture stopped).
WS_THRESHOLD_S = 600.0
REST_THRESHOLD_S = 3600.0
BRAIN_THRESHOLD_S = 900.0

# Per-dataset frontier thresholds (KI-166). Dense firehose-derived frontiers settle
# within minutes; 2h absorbs discovery-pass load stretch. REST-derived frontiers are
# legitimately bursty (open_interest observed ~3.5h healthy lag); 6h. A single 900s
# constant would flap on the slow eight.
_FRONTIER_DENSE = (brain_cfg.MARKPRICE_DATASET, brain_cfg.BOOKTICKER_DATASET,
                   brain_cfg.TRADES_DATASET, brain_cfg.FORCEORDER_DATASET)
_FRONTIER_SLOW = (brain_cfg.BASIS_DATASET, brain_cfg.KLINES_DATASET,
                  brain_cfg.GLOBAL_LS_ACCOUNT_DATASET, brain_cfg.OPEN_INTEREST_DATASET,
                  brain_cfg.PREMIUM_INDEX_DATASET, brain_cfg.TAKER_LS_RATIO_DATASET,
                  brain_cfg.TOP_LS_ACCOUNT_DATASET, brain_cfg.TOP_LS_POSITION_DATASET)
FRONTIER_DENSE_THRESHOLD_S = 7200.0
FRONTIER_SLOW_THRESHOLD_S = 21600.0


@dataclass(frozen=True)
class FreshnessSample:
    name: str
    newest_ns: Optional[int]      # newest observed write; None => never written
    threshold_s: float


@dataclass(frozen=True)
class StaleStream:
    name: str
    age_s: Optional[float]        # None => never written at all
    threshold_s: float


# -- pure evaluator ------------------------------------------------------------

def evaluate_freshness(samples, now_ns: int):
    """Return the samples that are stale: never written (``newest_ns is None``) or
    strictly older than their threshold. Pure — the gathering is separate."""
    stale = []
    for s in samples:
        if s.newest_ns is None:
            stale.append(StaleStream(name=s.name, age_s=None, threshold_s=s.threshold_s))
            continue
        age_s = (now_ns - s.newest_ns) / _NS
        if age_s > s.threshold_s:
            stale.append(StaleStream(name=s.name, age_s=age_s, threshold_s=s.threshold_s))
    return stale


# -- gatherers (injectable-free; hit the real filesystem / registry) ----------

def newest_parquet_mtime_ns(dataset_dir: str, dates) -> Optional[int]:
    """Newest ``*.parquet`` mtime (ns) under ``dataset_dir/symbol=*/date={dates}``.

    Bounded to the given ``dates`` (today + yesterday in production) so it never walks
    the full multi-million-file tape — fresh writes always land in the newest date
    partition. Returns ``None`` when nothing matches (dataset absent or no writes)."""
    newest: Optional[int] = None
    try:
        sym_entries = list(os.scandir(dataset_dir))
    except (FileNotFoundError, NotADirectoryError):
        return None
    for sym in sym_entries:
        if not sym.name.startswith("symbol=") or not sym.is_dir():
            continue
        for d in dates:
            part = os.path.join(sym.path, f"date={d}")
            try:
                with os.scandir(part) as it:
                    for f in it:
                        if f.name.endswith(".parquet"):
                            m = f.stat().st_mtime_ns
                            if newest is None or m > newest:
                                newest = m
            except (FileNotFoundError, NotADirectoryError):
                continue
    return newest


def brain_cursor_recency_ns(registry_path: str) -> Optional[int]:
    """``MAX(reader_cursor.updated_at_ns)`` — the last time ANY brain cursor advanced.
    Read-only; returns ``None`` if the registry is missing/unreadable."""
    if not os.path.exists(registry_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(updated_at_ns) FROM reader_cursor").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("substrate_freshness: registry read failed: %s", exc)
        return None
    return int(row[0]) if row and row[0] is not None else None


def brain_frontier_ns(registry_path: str) -> dict:
    """Per-dataset settlement frontier: ``{dataset: MAX(window_end_ns)}`` from
    ``snapshot_bookkeeping``. Read-only; ``{}`` when the registry is missing/unreadable
    (every expected dataset then reads as never-written => stale)."""
    if not os.path.exists(registry_path):
        return {}
    try:
        conn = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT dataset, MAX(window_end_ns) FROM snapshot_bookkeeping "
                "GROUP BY dataset").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("substrate_freshness: frontier read failed: %s", exc)
        return {}
    return {str(d): int(v) for d, v in rows if v is not None}


def brain_frontier_samples(registry_path: str):
    """One ``brain/frontier/<dataset>`` sample per EXPECTED dataset — an expected
    dataset with no bookkeeping at all surfaces as never-written, not silence."""
    got = brain_frontier_ns(registry_path)
    samples = []
    for ds in _FRONTIER_DENSE:
        samples.append(FreshnessSample(name=f"brain/frontier/{ds}",
                                       newest_ns=got.get(ds),
                                       threshold_s=FRONTIER_DENSE_THRESHOLD_S))
    for ds in _FRONTIER_SLOW:
        samples.append(FreshnessSample(name=f"brain/frontier/{ds}",
                                       newest_ns=got.get(ds),
                                       threshold_s=FRONTIER_SLOW_THRESHOLD_S))
    return samples


def _recent_dates(now_ns: int):
    today = datetime.fromtimestamp(now_ns / _NS, tz=timezone.utc).date()
    return [today.isoformat(), (today - timedelta(days=1)).isoformat()]


def gather(now_ns: Optional[int] = None):
    """Build the live sample set from the capture tape + brain registry."""
    if now_ns is None:
        now_ns = time.time_ns()
    dates = _recent_dates(now_ns)
    samples = []
    for ds in _WS_DATASETS:
        samples.append(FreshnessSample(
            name=f"capture/{ds}",
            newest_ns=newest_parquet_mtime_ns(os.path.join(cap_cfg.RAW_DIR, ds), dates),
            threshold_s=WS_THRESHOLD_S))
    for ds in _REST_DATASETS:
        samples.append(FreshnessSample(
            name=f"capture/{ds}",
            newest_ns=newest_parquet_mtime_ns(os.path.join(cap_cfg.RAW_DIR, ds), dates),
            threshold_s=REST_THRESHOLD_S))
    samples.append(FreshnessSample(
        name="brain/cursors",
        newest_ns=brain_cursor_recency_ns(brain_cfg.BRAIN_REGISTRY_PATH),
        threshold_s=BRAIN_THRESHOLD_S))
    samples.extend(brain_frontier_samples(brain_cfg.BRAIN_REGISTRY_PATH))
    return samples


# -- monitor entry point -------------------------------------------------------

def run(samples=None, now_ns: Optional[int] = None) -> MonitorResult:
    started = datetime.now(timezone.utc)
    if now_ns is None:
        now_ns = time.time_ns()
    if samples is None:
        samples = gather(now_ns=now_ns)
    stale = evaluate_freshness(samples, now_ns=now_ns)
    finished = datetime.now(timezone.utc)
    metrics = {"checked_count": len(samples), "stale_count": len(stale)}

    if not stale:
        return MonitorResult(
            monitor="substrate_freshness", status="ok", severity="info",
            title=f"substrate fresh ({len(samples)} streams writing)",
            metrics=metrics, started_at=started, finished_at=finished)

    lines = []
    for s in stale:
        if s.age_s is None:
            lines.append(f"- {s.name}: NO writes (threshold {s.threshold_s:.0f}s)")
        else:
            lines.append(f"- {s.name}: {s.age_s:.0f}s stale (threshold {s.threshold_s:.0f}s)")
    return MonitorResult(
        monitor="substrate_freshness", status="fail", severity="critical",
        title=f"{len(stale)} substrate stream(s) stale — capture/brain not writing",
        body="\n".join(lines), metrics=metrics,
        started_at=started, finished_at=finished)


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
