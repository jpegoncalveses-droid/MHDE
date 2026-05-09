"""Monitor: the running Streamlit process is recent enough vs master.

Streamlit doesn't auto-reload in this deployment (`mhde-streamlit.service`
runs without `--server.runOnSave`). Any change under `dashboard/`
sits on disk unloaded until the operator restarts the service.

This monitor compares two timestamps:

  - `process_start`  — the time `mhde-streamlit.service` last entered
                       active state. Read from `systemctl --user show
                       mhde-streamlit.service -p ActiveEnterTimestamp`.
  - `latest_commit`  — Unix epoch of the most recent commit on
                       master. Read from `git log -1 --format=%ct`.

If `latest_commit - process_start > STALE_THRESHOLD` we warn. The
default threshold is 4 hours: enough headroom for an unrelated commit
to land mid-day without flapping, tight enough that an overnight
forgotten-restart fires by morning.

Schedule: hourly. The unit installs at system level under `User=jpcg`
so the user-manager session for jpcg is reachable.
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.streamlit_freshness")


SERVICE_NAME = "mhde-streamlit.service"
REPO_PATH = "/home/jpcg/MHDE"
STALE_THRESHOLD = timedelta(hours=4)


# `Sat 2026-05-09 09:26:18 UTC`  (the format `systemctl show … --value`
# emits for ActiveEnterTimestamp on a UTC-locale host)
_TIMESTAMP_RE = re.compile(
    r"^[A-Za-z]{3}\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(UTC|GMT)$"
)


def _read_process_start() -> tuple[datetime | None, str | None]:
    """Return (datetime, error_message). Either the timestamp or a
    diagnostic. The systemctl --user call talks to the per-user
    systemd manager."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", SERVICE_NAME,
             "-p", "ActiveEnterTimestamp", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, f"systemctl unavailable: {exc}"
    if result.returncode != 0:
        return None, (
            f"systemctl --user returned {result.returncode}: "
            f"{result.stderr.strip()[:200]}"
        )
    value = result.stdout.strip()
    if not value or value == "n/a":
        return None, f"service {SERVICE_NAME} has never been active"
    m = _TIMESTAMP_RE.match(value)
    if not m:
        return None, f"unparseable ActiveEnterTimestamp: {value!r}"
    date_part, time_part, _tz = m.groups()
    dt = datetime.strptime(
        f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    return dt, None


def _read_latest_commit() -> tuple[datetime | None, str | None]:
    """Return (datetime, error_message)."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO_PATH, "log", "-1", "--format=%ct", "master"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, f"git unavailable: {exc}"
    if result.returncode != 0:
        return None, f"git log returned {result.returncode}: {result.stderr.strip()[:200]}"
    value = result.stdout.strip()
    if not value:
        return None, "git log returned empty"
    try:
        epoch = int(value)
    except ValueError:
        return None, f"unparseable epoch: {value!r}"
    return datetime.fromtimestamp(epoch, tz=timezone.utc), None


def run(
    process_start: datetime | None = None,
    latest_commit: datetime | None = None,
    threshold: timedelta = STALE_THRESHOLD,
    now: datetime | None = None,
) -> MonitorResult:
    """Compare process start time vs latest commit.

    `process_start` and `latest_commit` are exposed as parameters for
    unit testing. When None, they are read from systemctl and git.
    """
    started = datetime.now(timezone.utc)
    now = now or started

    proc_err = commit_err = None
    if process_start is None:
        process_start, proc_err = _read_process_start()
    if latest_commit is None:
        latest_commit, commit_err = _read_latest_commit()

    metrics: dict[str, object] = {}
    if process_start is not None:
        metrics["process_start"] = process_start.isoformat()
    if latest_commit is not None:
        metrics["latest_commit"] = latest_commit.isoformat()
    metrics["threshold_hours"] = threshold.total_seconds() / 3600.0

    finished = datetime.now(timezone.utc)

    if process_start is None or latest_commit is None:
        body_parts = []
        if proc_err:
            body_parts.append(f"process_start: {proc_err}")
        if commit_err:
            body_parts.append(f"latest_commit: {commit_err}")
        return MonitorResult(
            monitor="streamlit_freshness",
            status="warn",
            severity="warn",
            title="streamlit_freshness could not read its inputs",
            body="\n".join(f"- {b}" for b in body_parts),
            metrics=metrics,
            started_at=started, finished_at=finished,
        )

    lag = latest_commit - process_start
    metrics["lag_hours"] = round(lag.total_seconds() / 3600.0, 2)

    if lag <= threshold:
        return MonitorResult(
            monitor="streamlit_freshness",
            status="ok",
            severity="info",
            title=(
                f"Streamlit fresh (commit lag {lag.total_seconds() / 3600.0:.1f}h "
                f"≤ {threshold.total_seconds() / 3600.0:.0f}h)"
            ),
            metrics=metrics,
            started_at=started, finished_at=finished,
        )

    return MonitorResult(
        monitor="streamlit_freshness",
        status="fail",
        severity="warn",
        title="Streamlit running stale code",
        body=(
            f"- Streamlit process started: {process_start.isoformat()}\n"
            f"- Latest commit on master:   {latest_commit.isoformat()}\n"
            f"- Lag: {lag.total_seconds() / 3600.0:.1f}h "
            f"(threshold {threshold.total_seconds() / 3600.0:.0f}h)\n"
            f"- Restart with: `systemctl --user restart {SERVICE_NAME}`"
        ),
        metrics=metrics,
        started_at=started, finished_at=finished,
    )


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
