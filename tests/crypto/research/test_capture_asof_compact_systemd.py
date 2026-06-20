"""Static checks for the daily as-of seal-yesterday compaction systemd units.

A daily oneshot timer (post-01:00, once yesterday's as-of date= has sealed) that
whole-partition compacts the 7 REST as-of series. Built-not-deployed; filesystem-only;
never opens the DB; same shared-host resource caps as the hourly closed-hour compactor.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
ASOF_SVC = REPO / "systemd" / "mhde-capture-asof-compact.service"
ASOF_TIMER = REPO / "systemd" / "mhde-capture-asof-compact.timer"


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
    assert ASOF_SVC.exists() and ASOF_TIMER.exists()


def test_units_valid_syntax():
    for unit in (ASOF_SVC, ASOF_TIMER):
        rc, out = _verify(unit)
        assert rc == 0, f"verify of {unit} failed:\n{out}"


def test_asof_is_oneshot_invoking_the_asof_compactor():
    svc = _parse(ASOF_SVC)
    assert svc.get("Service", "Type") == "oneshot"
    assert "capture-asof-compact" in svc.get("Service", "ExecStart")
    assert "/home/jpcg/MHDE/venv/bin/python" in svc.get("Service", "ExecStart")
    assert svc.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert not svc.has_option("Service", "User")


def test_timer_is_daily_post_0100():
    timer = _parse(ASOF_TIMER)
    oncal = timer.get("Timer", "OnCalendar")
    # daily, and the clock time is in the 01:00 hour (after the as-of date= seals ~00:35).
    assert oncal.split()[-1].startswith("01:"), oncal
    assert timer.get("Timer", "Persistent") == "true"
    assert timer.get("Install", "WantedBy") == "timers.target"


def test_asof_carries_resource_caps_shared_host_invariant():
    svc = _parse(ASOF_SVC)
    assert svc.get("Service", "OOMScoreAdjust") == "800"
    assert int(svc.get("Service", "CPUWeight")) <= 20
    assert int(svc.get("Service", "IOWeight")) <= 20
    assert svc.has_option("Service", "MemoryMax")


def test_units_do_not_touch_production_db():
    for unit in (ASOF_SVC, ASOF_TIMER):
        assert "mhde.duckdb" not in unit.read_text()
