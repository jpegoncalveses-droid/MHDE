"""Regression tests for systemd unit files.

Each test here exists because a real production incident was caused by
the unit files diverging from intent. See `KNOWN_ISSUES.md` for the
historical record.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO / "systemd"
SYSTEM_DEPLOY = Path("/etc/systemd/system")
USER_DEPLOY = Path.home() / ".config/systemd/user"


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _exec_starts(unit_path: Path) -> list[str]:
    return [line.split("=", 1)[1].strip()
            for line in _read(unit_path).splitlines()
            if line.startswith("ExecStart=")]


def _on_calendar(timer_path: Path) -> str | None:
    for line in _read(timer_path).splitlines():
        if line.startswith("OnCalendar="):
            return line.split("=", 1)[1].strip()
    return None


# ──────────────────────────────────────────────────────────────────────
# KI-101: retrain timers must not all fire together
# ──────────────────────────────────────────────────────────────────────


def test_retrain_timers_staggered():
    """Equity / crypto / FX retrain timers must fire at different times
    to avoid DuckDB write contention. KI-101."""
    schedules = {
        "equity": _on_calendar(SYSTEMD_DIR / "mhde-retrain.timer"),
        "crypto": _on_calendar(SYSTEMD_DIR / "mhde-crypto-retrain.timer"),
        "fx":     _on_calendar(SYSTEMD_DIR / "mhde-fx-retrain.timer"),
    }
    # All set
    for engine, sched in schedules.items():
        assert sched, f"{engine} retrain timer missing OnCalendar"
    # Pairwise distinct
    assert len({s for s in schedules.values()}) == 3, (
        f"retrain timers not staggered: {schedules}"
    )


# ──────────────────────────────────────────────────────────────────────
# KI-102: equity predict service must include feature backfill before predict
# ──────────────────────────────────────────────────────────────────────


def test_equity_predict_includes_features():
    cmds = _exec_starts(SYSTEMD_DIR / "mhde-predict.service")
    joined = " || ".join(cmds)
    assert "backfill-features" in joined, (
        f"mhde-predict.service must include backfill-features ExecStart. KI-102. "
        f"Got: {cmds}"
    )
    assert "ml predict" in joined, "mhde-predict.service must include `ml predict`"


# ──────────────────────────────────────────────────────────────────────
# KI-106: user-level units must not declare User= or Group=
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not USER_DEPLOY.exists(),
                    reason="not running on the deployment host")
def test_user_level_units_no_user_group():
    """Silently fails with exit code 216 if present. KI-106."""
    bad: list[tuple[Path, str]] = []
    for unit in USER_DEPLOY.glob("*.service"):
        text = _read(unit)
        for line in text.splitlines():
            if line.startswith("User=") or line.startswith("Group="):
                bad.append((unit, line.strip()))
    assert not bad, (
        "User-level systemd units must NOT declare User=/Group= "
        f"(silent failure with exit 216). KI-106. Offenders: {bad}"
    )


# ──────────────────────────────────────────────────────────────────────
# KI-108: crypto predict service must chain backfill steps
# ──────────────────────────────────────────────────────────────────────


def test_crypto_predict_chain():
    cmds = _exec_starts(SYSTEMD_DIR / "mhde-crypto-predict.service")
    required_in_order = [
        "crypto backfill-prices",
        "crypto backfill-funding",
        "crypto backfill-oi",
        "crypto backfill-labels",
        "crypto backfill-features",
        "crypto predict",
    ]
    joined = " | ".join(cmds)
    for step in required_in_order:
        assert step in joined, (
            f"mhde-crypto-predict.service missing `{step}` ExecStart. KI-108. "
            f"Got: {cmds}"
        )
    # Confirm relative ordering by index in cmds list.
    indices = [next(i for i, c in enumerate(cmds) if step in c)
               for step in required_in_order]
    assert indices == sorted(indices), (
        f"crypto predict ExecStart out of order. Expected: {required_in_order}, "
        f"got order: {[cmds[i] for i in indices]}"
    )


# ──────────────────────────────────────────────────────────────────────
# KI-109: health-check unit + timer must be deployed and enabled
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not USER_DEPLOY.exists(),
                    reason="not running on the deployment host")
def test_health_check_unit_deployed():
    svc = USER_DEPLOY / "mhde-health-check.service"
    timer = USER_DEPLOY / "mhde-health-check.timer"
    assert svc.exists(), f"{svc} missing — KI-109"
    assert timer.exists(), f"{timer} missing — KI-109"
    # Service must invoke `system health-check`.
    assert "system health-check" in _read(svc)
    # Timer must declare an OnCalendar.
    assert _on_calendar(timer), f"{timer} missing OnCalendar"


# ──────────────────────────────────────────────────────────────────────
# KI-112: every unit in repo systemd/ must validate cleanly
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not shutil.which("systemd-analyze"),
                    reason="systemd-analyze not available")
def test_all_repo_units_validate():
    """`systemd-analyze verify` must pass on every .service / .timer in
    the repo. KI-112 (drift between repo and deployed)."""
    failures: list[tuple[Path, str]] = []
    for unit in sorted(SYSTEMD_DIR.glob("*.service")) + sorted(SYSTEMD_DIR.glob("*.timer")):
        result = subprocess.run(
            ["systemd-analyze", "verify", str(unit)],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or result.stdout or result.stderr.strip():
            failures.append((unit, result.stderr.strip() or result.stdout.strip()))
    assert not failures, f"systemd unit validation failed: {failures}"


# ──────────────────────────────────────────────────────────────────────
# KI-112 part 2: repo systemd files match deployed copies
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not SYSTEM_DEPLOY.exists(),
                    reason="not on a deployment host")
def test_repo_vs_deployed_unit_parity():
    """For every system-level unit in repo systemd/, the deployed copy
    in /etc/systemd/system/ must exist and match byte-for-byte
    (modulo CRLF). KI-112."""
    drift: list[tuple[str, str]] = []
    for repo_unit in sorted(SYSTEMD_DIR.glob("*")):
        if not repo_unit.is_file():
            continue
        deployed = SYSTEM_DEPLOY / repo_unit.name
        if not deployed.exists():
            continue  # Repo also includes user-level templates we don't deploy here
        if _read(repo_unit).strip() != _read(deployed).strip():
            drift.append((repo_unit.name, "content differs"))
    assert not drift, (
        f"Deployed unit files diverge from repo. KI-112. {drift}"
    )
