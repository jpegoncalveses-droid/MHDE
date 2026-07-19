"""Outcome-based check for the brain-store compactor (KI-165).

``mhde-brain-compact`` is an hourly systemd oneshot. When it is OOM-killed (SIGKILL,
unhandleable) or exits nonzero it leaves NO trace the process itself can emit, so the
2026-07-15→19 incident — the compactor OOM-looping every hour at ``MemoryMax=1G`` — ran
silently for ~4 days while the part-file fan-out it was meant to bound drove the disk
soft-floor → capture-buffer shrink → dense-cursor starvation chain (KI-161/162/164).

Detection is therefore EXTERNAL and filesystem-based: a SUCCESSFUL run writes a heartbeat
(``compaction.write_heartbeat`` → ``BRAIN_COMPACT_HEARTBEAT_PATH``) on full completion of both
passes; this check goes RED when that heartbeat is missing or older than ``stale_red_hours``.
A missing/stale heartbeat catches BOTH failure modes (OOM-kill and nonzero exit) — neither
writes it. ``systemctl --user is-failed`` is NOT used: the continuous monitor runs in the
system scope with no user D-Bus session (same reason ``streamlit_freshness`` reads ``/proc``).

The check is pure over an injected ``now`` + heartbeat dict, so every branch is unit-tested.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from crypto.research.brain.config import BRAIN_COMPACT_HEARTBEAT_PATH
from monitoring.pipeline_monitor.core import Status, StepResult

BRAIN_COMPACT_FRESHNESS = "Brain-store compactor freshness"

#: The compactor runs hourly (``mhde-brain-compact.timer`` at :36). Allow ~3 missed runs before
#: RED so a single transient miss (a busy hour, a Persistent catch-up) does not flap; a genuine
#: failure loop still surfaces within ~3 h instead of the ~4 days the KI-165 incident ran.
DEFAULT_STALE_RED_HOURS = 3.0

_NS_PER_HOUR = 3_600 * 1_000_000_000


def evaluate(now: datetime, heartbeat: Optional[dict], *,
             stale_red_hours: float = DEFAULT_STALE_RED_HOURS) -> StepResult:
    """Pure verdict: RED if the heartbeat is absent/unreadable or its ``last_success_ns`` is
    older than ``stale_red_hours``; GREEN otherwise. ``now`` is tz-aware UTC."""
    if heartbeat is None:
        return StepResult(
            BRAIN_COMPACT_FRESHNESS, Status.RED,
            "no brain-compact success heartbeat — compactor has never completed (or is "
            "OOM-killing / failing every run; see KI-165)")
    last_ns = heartbeat.get("last_success_ns")
    if not isinstance(last_ns, int):
        return StepResult(BRAIN_COMPACT_FRESHNESS, Status.RED,
                          f"brain-compact heartbeat malformed (last_success_ns={last_ns!r})")
    now_ns = int(now.timestamp() * 1_000_000_000)
    age_h = (now_ns - last_ns) / _NS_PER_HOUR
    last_utc = datetime.fromtimestamp(last_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if age_h > stale_red_hours:
        return StepResult(
            BRAIN_COMPACT_FRESHNESS, Status.RED,
            f"last successful brain compaction {age_h:.1f}h ago ({last_utc}) > "
            f"{stale_red_hours:.0f}h — compactor stalled/failing (KI-165)")
    compacted = heartbeat.get("sealed_compacted", "?")
    return StepResult(
        BRAIN_COMPACT_FRESHNESS, Status.GREEN,
        f"brain compaction ran {age_h:.1f}h ago ({last_utc}); {compacted} sealed partitions "
        f"compacted last run")


def _read_heartbeat(path: str) -> Optional[dict]:
    """Read the heartbeat JSON; ``None`` on missing / unreadable / non-object (→ RED upstream)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_brain_compact_freshness(now: datetime, *,
                                  heartbeat_path: str = BRAIN_COMPACT_HEARTBEAT_PATH,
                                  stale_red_hours: float = DEFAULT_STALE_RED_HOURS) -> StepResult:
    """I/O wrapper: read the heartbeat file and evaluate it."""
    return evaluate(now, _read_heartbeat(heartbeat_path), stale_red_hours=stale_red_hours)
