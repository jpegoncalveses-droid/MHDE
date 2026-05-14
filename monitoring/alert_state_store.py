"""JSON sidecar for monitor alert-throttle state (KI-150 part 2/3).

Replaces the DuckDB ``monitor_alert_state`` table for the throttle/dedup
path in ``monitoring/alert.py``. The DuckDB table is left in place as
harmless residue (no migration), but the throttle no longer needs a
writable DB connection.

Concurrency: ``save_state`` takes an ``fcntl.LOCK_EX`` on the JSON file
across the read-modify-write window. Multiple monitor services writing
simultaneously will serialize cleanly; ``load_state`` takes ``LOCK_SH``
so a reader sees a consistent snapshot.

Path resolution order (first wins):
  1. ``path=`` kwarg (used by tests)
  2. ``$MHDE_MONITOR_ALERT_STATE_PATH`` env var
  3. ``data/monitor_alert_state.json`` relative to cwd
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mhde.monitoring.alert_state")

_DEFAULT_PATH = Path("data/monitor_alert_state.json")


def _resolve_path(path: Optional[Path]) -> Path:
    if path is not None:
        return path
    override = os.environ.get("MHDE_MONITOR_ALERT_STATE_PATH")
    if override:
        return Path(override)
    return _DEFAULT_PATH


def load_state(monitor: str, path: Optional[Path] = None) -> Optional[dict]:
    """Return ``{last_payload_sha, last_severity, last_sent_at}`` or ``None``.

    ``last_sent_at`` comes back as a ``datetime`` (parsed from ISO-8601).
    A missing file, a missing monitor key, or a corrupt JSON file all
    return ``None`` so the caller treats the cycle as first-send.
    """
    p = _resolve_path(path)
    try:
        with open(p, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            contents = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("alert_state: read failed for %s", p)
        return None

    if not contents.strip():
        return None
    try:
        data = json.loads(contents)
    except json.JSONDecodeError:
        logger.exception("alert_state: %s is corrupt — treating as empty", p)
        return None

    entry = data.get(monitor)
    if entry is None:
        return None
    ts = entry.get("last_sent_at")
    if isinstance(ts, str):
        try:
            entry = dict(entry)
            entry["last_sent_at"] = datetime.fromisoformat(ts)
        except ValueError:
            entry["last_sent_at"] = None
    return entry


def save_state(monitor: str, payload_sha: str, severity: str,
               sent_at: datetime, path: Optional[Path] = None) -> None:
    """Insert/overwrite the ``monitor`` entry, serialized via flock."""
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 'a+' creates if missing; seek-to-start lets us read prior state.
        with open(p, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            contents = f.read()
            if contents.strip():
                try:
                    data = json.loads(contents)
                except json.JSONDecodeError:
                    logger.warning(
                        "alert_state: %s corrupt — overwriting", p
                    )
                    data = {}
            else:
                data = {}
            data[monitor] = {
                "last_payload_sha": payload_sha,
                "last_severity": severity,
                "last_sent_at": sent_at.isoformat(),
            }
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        logger.exception("alert_state: write failed for %s", p)
