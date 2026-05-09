"""Monitor: the running Streamlit process is recent enough vs master.

Streamlit doesn't auto-reload in this deployment (`mhde-streamlit.service`
runs without `--server.runOnSave`). Any change under `dashboard/`
sits on disk unloaded until the operator restarts the service.

This monitor compares two timestamps:

  - `process_start`  — the wall-clock time the Streamlit process
                       was started. Read from `/proc/<PID>/stat`
                       field 22 (start time in clock ticks since
                       boot) combined with `/proc/uptime` and
                       `time.time()`. The PID is found via
                       `pgrep -f 'streamlit run dashboard'`.
  - `latest_commit`  — Unix epoch of the most recent commit on
                       master. Read from `git log -1 --format=%ct`.

If `latest_commit - process_start > STALE_THRESHOLD` we warn. The
default threshold is 4 hours.

**Why /proc instead of `systemctl --user`?** The monitor systemd
unit installs at system level under `User=jpcg` (matches the other
mhde-monitor-* units), and `systemctl --user` requires a D-Bus
session that the system manager doesn't provide for non-login users
("Failed to connect to bus: No medium found"). Reading /proc works
identically from both system-level systemd invocation and a direct
CLI invocation in a user shell. See OPERATIONS.md → "Cross-scope
systemd traps".
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.streamlit_freshness")


SERVICE_NAME = "mhde-streamlit.service"
REPO_PATH = "/home/jpcg/MHDE"
STALE_THRESHOLD = timedelta(hours=4)
# Tight pattern that matches only the actual Streamlit process and
# excludes the relay (`mhde_streamlit_relay.py`) which also contains
# the substring "streamlit".
PGREP_PATTERN = "streamlit run dashboard"


def _find_streamlit_pid() -> tuple[int | None, str | None]:
    """Return (pid, error). pgrep against the tight pattern above."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", PGREP_PATTERN],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, f"pgrep unavailable: {exc}"
    if result.returncode == 1:
        # No process matched — Streamlit is down (or pattern stale).
        return None, f"no process matched 'pgrep -f {PGREP_PATTERN!r}'"
    if result.returncode != 0:
        return None, (
            f"pgrep returned {result.returncode}: "
            f"{result.stderr.strip()[:200]}"
        )
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None, "pgrep returned empty output"
    # Multiple matches shouldn't happen with the tight pattern, but
    # if they do we take the oldest PID (the lowest number) to match
    # the long-lived parent rather than a transient child.
    try:
        pid = min(int(line.strip()) for line in lines if line.strip().isdigit())
    except ValueError:
        return None, f"pgrep returned non-numeric output: {result.stdout!r}"
    return pid, None


def _read_proc_start_time(pid: int) -> tuple[datetime | None, str | None]:
    """Parse /proc/<pid>/stat field 22 (starttime in clock ticks since
    boot) and combine with /proc/uptime + wall-clock to produce the
    absolute start time."""
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, f"/proc/{pid}/stat not found (process exited?)"
    except PermissionError as exc:
        return None, f"/proc/{pid}/stat unreadable: {exc}"

    # The comm field (#2) is in parens and may itself contain spaces
    # or a closing paren; rindex picks the LAST `)` which terminates
    # comm reliably. Everything after that is space-separated.
    try:
        close_idx = raw.rindex(")")
    except ValueError:
        return None, f"/proc/{pid}/stat malformed (no comm close): {raw[:120]!r}"
    tail = raw[close_idx + 2:].split()
    # Field numbering in proc(5): 1=pid, 2=comm, 3=state, ..., 22=starttime.
    # `tail` starts at field 3 (state). starttime is the 20th field of
    # `tail` → index 19.
    if len(tail) < 20:
        return None, f"/proc/{pid}/stat too few fields: {len(tail)}"
    try:
        starttime_ticks = int(tail[19])
    except ValueError:
        return None, f"/proc/{pid}/stat field 22 not int: {tail[19]!r}"

    try:
        with open("/proc/uptime", "r") as fh:
            uptime_s = float(fh.read().split()[0])
    except (FileNotFoundError, ValueError) as exc:
        return None, f"/proc/uptime unreadable: {exc}"

    try:
        clock_hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clock_hz = 100  # the universal Linux default

    boot_epoch = time.time() - uptime_s
    start_epoch = boot_epoch + (starttime_ticks / clock_hz)
    return datetime.fromtimestamp(start_epoch, tz=timezone.utc), None


def _read_process_start() -> tuple[datetime | None, str | None]:
    """Return (datetime, error). Two-step: pgrep for the PID, then
    parse /proc/<pid>/stat field 22. Works under both system-level
    systemd invocation and a direct CLI invocation."""
    pid, err = _find_streamlit_pid()
    if pid is None:
        return None, err
    return _read_proc_start_time(pid)


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
