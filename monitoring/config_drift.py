"""Monitor: detect drift between repo systemd files and the deployed
copies under /etc/systemd/system and ~/.config/systemd/user.

Daily.

Subset of what `tests/regression/test_systemd_units.py::test_repo_vs_deployed_unit_parity`
asserts at test time, but as a runtime monitor that fires Telegram on
detection.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.config_drift")

REPO = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO / "systemd"
SYSTEM_DEPLOY = Path("/etc/systemd/system")
USER_DEPLOY = Path.home() / ".config/systemd/user"


def _diff_unit(repo_path: Path, deployed_path: Path) -> str | None:
    """Return None if files match (modulo trailing whitespace), else a
    short summary of what differs."""
    if not deployed_path.exists():
        return f"deployed copy missing at {deployed_path}"
    repo_text = repo_path.read_text().strip()
    deployed_text = deployed_path.read_text().strip()
    if repo_text == deployed_text:
        return None
    # Cheap diff summary: line counts + first differing line.
    repo_lines = repo_text.splitlines()
    deployed_lines = deployed_text.splitlines()
    for i, (a, b) in enumerate(zip(repo_lines, deployed_lines), 1):
        if a != b:
            return f"line {i} differs (repo: {a[:60]!r} vs deployed: {b[:60]!r})"
    return f"length differs ({len(repo_lines)} vs {len(deployed_lines)} lines)"


def run() -> MonitorResult:
    started = datetime.now(timezone.utc)

    drift: list[str] = []
    checked = 0

    for repo_unit in sorted(SYSTEMD_DIR.glob("*")):
        if not repo_unit.is_file():
            continue
        # Try system-level path first; fall back to user-level if not
        # there. The repo doesn't distinguish, so we check both.
        system_target = SYSTEM_DEPLOY / repo_unit.name
        user_target = USER_DEPLOY / repo_unit.name
        target = system_target if system_target.exists() else user_target
        if not target.exists():
            # Repo has the unit but it's not deployed anywhere — could be
            # a template never installed; we don't flag this case.
            continue
        checked += 1
        diff = _diff_unit(repo_unit, target)
        if diff is not None:
            drift.append(f"{repo_unit.name} → {target}: {diff}")

    finished = datetime.now(timezone.utc)

    if drift:
        return MonitorResult(
            monitor="config_drift",
            status="warn",
            severity="warn",
            title="Repo ↔ deployed systemd config drift",
            body="\n".join(f"- {d}" for d in drift),
            metrics={"units_checked": checked, "drift_count": len(drift)},
            started_at=started, finished_at=finished,
        )
    return MonitorResult(
        monitor="config_drift",
        status="ok",
        severity="info",
        title=f"{checked} systemd units match repo",
        metrics={"units_checked": checked},
        started_at=started, finished_at=finished,
    )


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
