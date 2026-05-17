"""Static checks for the universe-correction systemd unit files.

systemd runtime is not exercised here (no install / no journal); we only
parse the files and run ``systemd-analyze verify`` to catch syntax mistakes
before they hit production.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
UNITS_DIR = REPO / "systemd"

RANK_SERVICE = UNITS_DIR / "mhde-crypto-rank-universe-daily.service"
RANK_TIMER = UNITS_DIR / "mhde-crypto-rank-universe-daily.timer"
BUILD_SERVICE = UNITS_DIR / "mhde-crypto-build-universe-daily.service"
BUILD_TIMER = UNITS_DIR / "mhde-crypto-build-universe-daily.timer"

ALL_UNITS = [RANK_SERVICE, RANK_TIMER, BUILD_SERVICE, BUILD_TIMER]


def _parse(unit_path: Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    # ConfigParser by default is case-insensitive on options — systemd is
    # case-sensitive, but the canonical keys we read all use the standard
    # capitalization so this is fine.
    p.optionxform = str  # preserve case
    p.read(unit_path)
    return p


def _systemd_analyze_verify(unit_path: Path) -> tuple[int, str]:
    """Run systemd-analyze verify against one unit. Returns (rc, combined output)."""
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available in this environment")
    proc = subprocess.run(
        ["systemd-analyze", "verify", "--user", str(unit_path)],
        capture_output=True, text=True, timeout=20,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


# ---------- A. systemd-analyze verify ----------

def test_rank_universe_timer_valid_syntax():
    rc, out = _systemd_analyze_verify(RANK_TIMER)
    # systemd-analyze verify exits non-zero on errors and prints them. Warnings
    # alone usually return 0; treat non-zero as failure with the captured output.
    assert rc == 0, f"verify of {RANK_TIMER} failed:\n{out}"


def test_rank_universe_service_valid_syntax():
    rc, out = _systemd_analyze_verify(RANK_SERVICE)
    assert rc == 0, f"verify of {RANK_SERVICE} failed:\n{out}"


def test_build_universe_timer_valid_syntax():
    rc, out = _systemd_analyze_verify(BUILD_TIMER)
    assert rc == 0, f"verify of {BUILD_TIMER} failed:\n{out}"


def test_build_universe_service_valid_syntax():
    rc, out = _systemd_analyze_verify(BUILD_SERVICE)
    assert rc == 0, f"verify of {BUILD_SERVICE} failed:\n{out}"


# ---------- B. Required key/value invariants ----------

@pytest.mark.parametrize("timer_path", [RANK_TIMER, BUILD_TIMER])
def test_both_units_specify_persistent_true(timer_path):
    p = _parse(timer_path)
    assert p.get("Timer", "Persistent") == "true", (
        f"{timer_path.name}: Persistent must be 'true' to recover missed fires"
    )


@pytest.mark.parametrize("svc_path", [RANK_SERVICE, BUILD_SERVICE])
def test_both_units_specify_correct_user(svc_path):
    p = _parse(svc_path)
    assert p.get("Service", "User") == "jpcg"


@pytest.mark.parametrize("svc_path", [RANK_SERVICE, BUILD_SERVICE])
def test_both_units_specify_correct_workingdirectory(svc_path):
    p = _parse(svc_path)
    assert p.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"


# ---------- C. ExecStart commands ----------

def test_rank_service_invokes_correct_cli_command():
    p = _parse(RANK_SERVICE)
    exec_start = p.get("Service", "ExecStart")
    assert "rank-universe-daily" in exec_start, (
        f"rank service ExecStart must call `crypto rank-universe-daily`; got: {exec_start}"
    )
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start


def test_build_service_invokes_correct_cli_command():
    """The daily rebuild service MUST NOT carry --dry-run."""
    p = _parse(BUILD_SERVICE)
    exec_start = p.get("Service", "ExecStart")
    assert "build-universe" in exec_start, (
        f"build service ExecStart must call `crypto build-universe`; got: {exec_start}"
    )
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start
    assert "--dry-run" not in exec_start, (
        "build-universe daily service must NOT carry --dry-run "
        "(dry-run is the operator-review tool, not the live timer)"
    )


# ---------- D. Pre-flight checks present ----------

@pytest.mark.parametrize("svc_path", [RANK_SERVICE, BUILD_SERVICE])
def test_services_have_venv_and_db_preflight_checks(svc_path):
    """Each service must fail fast if the venv or DB is missing."""
    raw = svc_path.read_text()
    assert "ExecStartPre" in raw, f"{svc_path.name}: missing ExecStartPre"
    assert "venv/bin/python" in raw, (
        f"{svc_path.name}: must check for the venv as a pre-flight"
    )
    assert "mhde.duckdb" in raw, (
        f"{svc_path.name}: must check for the DB as a pre-flight"
    )


# ---------- E. Schedule sanity ----------

def test_rank_timer_runs_daily_at_23_00_utc():
    p = _parse(RANK_TIMER)
    cal = p.get("Timer", "OnCalendar")
    assert "23:00:00" in cal, f"expected daily 23:00:00; got {cal!r}"
    assert "Sun" not in cal, f"daily timer should not be weekly-only; got {cal!r}"


def test_build_timer_runs_daily_at_23_30_utc():
    p = _parse(BUILD_TIMER)
    cal = p.get("Timer", "OnCalendar")
    assert "23:30:00" in cal, f"expected daily 23:30:00; got {cal!r}"
    assert "Sun" not in cal, f"daily timer should not be weekly-only; got {cal!r}"


# ---------- F. Logging convention ----------

@pytest.mark.parametrize("svc_path", [RANK_SERVICE, BUILD_SERVICE])
def test_services_log_to_journal(svc_path):
    p = _parse(svc_path)
    assert p.get("Service", "StandardOutput") == "journal"
    assert p.get("Service", "StandardError") == "journal"


# ---------- G. Restart policy ----------

@pytest.mark.parametrize("svc_path", [RANK_SERVICE, BUILD_SERVICE])
def test_services_have_restart_on_failure_with_5min_backoff(svc_path):
    p = _parse(svc_path)
    assert p.get("Service", "Restart") == "on-failure"
    assert p.get("Service", "RestartSec") == "300"
