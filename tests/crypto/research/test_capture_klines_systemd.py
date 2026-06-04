"""Static checks for the capture-core 1h klines systemd units (built-not-deployed).

A persistent Type=simple maintenance service + a daily oneshot retention timer.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
MAINT = REPO / "systemd" / "mhde-capture-klines.service"
EXPIRE_SVC = REPO / "systemd" / "mhde-capture-klines-expire.service"
EXPIRE_TIMER = REPO / "systemd" / "mhde-capture-klines-expire.timer"


def _parse(unit_path: Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(unit_path)
    return p


def _verify(unit_path: Path) -> tuple[int, str]:
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available in this environment")
    proc = subprocess.run(
        ["systemd-analyze", "verify", "--user", str(unit_path)],
        capture_output=True, text=True, timeout=20,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def test_units_exist():
    assert MAINT.exists() and EXPIRE_SVC.exists() and EXPIRE_TIMER.exists()


def test_units_valid_syntax():
    for unit in (MAINT, EXPIRE_SVC, EXPIRE_TIMER):
        rc, out = _verify(unit)
        assert rc == 0, f"verify of {unit} failed:\n{out}"


def test_maintenance_is_persistent_user_unit_invoking_run():
    p = _parse(MAINT)
    assert p.get("Service", "Type") == "simple"
    assert not p.has_option("Service", "User")
    assert not p.has_option("Service", "Group")
    assert p.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert p.get("Service", "Restart") in ("on-failure", "always")
    exec_start = p.get("Service", "ExecStart")
    assert "capture-klines-run" in exec_start
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start
    assert int(p.get("Service", "TimeoutStopSec")) >= 15
    assert p.get("Service", "StandardOutput") == "journal"


def test_expire_is_oneshot_with_daily_persistent_timer():
    svc = _parse(EXPIRE_SVC)
    assert svc.get("Service", "Type") == "oneshot"
    assert "capture-klines-expire" in svc.get("Service", "ExecStart")
    timer = _parse(EXPIRE_TIMER)
    assert timer.has_option("Timer", "OnCalendar")
    assert timer.get("Timer", "Persistent") == "true"
    assert timer.get("Install", "WantedBy") == "timers.target"


def test_units_do_not_touch_production_db():
    for unit in (MAINT, EXPIRE_SVC, EXPIRE_TIMER):
        assert "mhde.duckdb" not in unit.read_text()
