"""Static checks for the capture-core firehose retention systemd units (Phase 0).

A daily oneshot timer that expires firehose date partitions past the rolling
window. Built-not-deployed; filesystem-only; never opens the production DB.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
EXPIRE_SVC = REPO / "systemd" / "mhde-capture-firehose-expire.service"
EXPIRE_TIMER = REPO / "systemd" / "mhde-capture-firehose-expire.timer"


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
    assert EXPIRE_SVC.exists() and EXPIRE_TIMER.exists()


def test_units_valid_syntax():
    for unit in (EXPIRE_SVC, EXPIRE_TIMER):
        rc, out = _verify(unit)
        assert rc == 0, f"verify of {unit} failed:\n{out}"


def test_expire_is_oneshot_with_daily_persistent_timer():
    svc = _parse(EXPIRE_SVC)
    assert svc.get("Service", "Type") == "oneshot"
    assert "capture-firehose-expire" in svc.get("Service", "ExecStart")
    assert "/home/jpcg/MHDE/venv/bin/python" in svc.get("Service", "ExecStart")
    assert svc.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert not svc.has_option("Service", "User")
    timer = _parse(EXPIRE_TIMER)
    assert timer.has_option("Timer", "OnCalendar")
    assert timer.get("Timer", "Persistent") == "true"
    assert timer.get("Install", "WantedBy") == "timers.target"


def test_expire_carries_resource_caps():
    # PR-3 discipline: OOM-first + below the system.slice CPU/IO default so a sweep
    # never disturbs the engine.
    svc = _parse(EXPIRE_SVC)
    assert svc.get("Service", "OOMScoreAdjust") == "800"
    assert int(svc.get("Service", "CPUWeight")) <= 20
    assert int(svc.get("Service", "IOWeight")) <= 20
    assert svc.has_option("Service", "MemoryMax")


def test_units_do_not_touch_production_db():
    for unit in (EXPIRE_SVC, EXPIRE_TIMER):
        assert "mhde.duckdb" not in unit.read_text()
