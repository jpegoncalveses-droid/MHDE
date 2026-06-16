"""Static checks for the ADR-039 §D layer-2 stall-detector systemd units (BUILD-ONLY).

A --user oneshot + 30s timer that runs `capture-stall-check`: reads shard heartbeats + systemd
unit states and Telegram-alerts on a hung/asymmetric shard. Tracked, not installed/enabled.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
SVC = REPO / "systemd" / "mhde-capture-stall-detector.service"
TIMER = REPO / "systemd" / "mhde-capture-stall-detector.timer"


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
    assert SVC.exists() and TIMER.exists()


def test_units_valid_syntax():
    for u in (SVC, TIMER):
        rc, out = _verify(u)
        assert rc == 0, out


def test_service_is_oneshot_user_unit_invoking_stall_check():
    p = _parse(SVC)
    assert p.get("Service", "Type") == "oneshot"
    assert not p.has_option("Service", "User")          # capture-family --user convention
    assert not p.has_option("Service", "Group")
    assert p.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    exec_start = p.get("Service", "ExecStart")
    assert "capture-stall-check" in exec_start
    assert "--of 8" in exec_start                        # MUST match the shard units' --of
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start
    assert p.has_option("Service", "ExecStartPre")
    assert p.get("Service", "StandardOutput") == "journal"
    assert "mhde.duckdb" not in SVC.read_text()


def test_service_is_idle_priority_and_oom_first():
    p = _parse(SVC)
    assert p.get("Service", "OOMScoreAdjust") == "800"
    assert int(p.get("Service", "CPUWeight")) <= 20
    assert int(p.get("Service", "IOWeight")) <= 20
    assert p.get("Service", "IOSchedulingClass") == "idle"


def test_timer_runs_frequently_and_installs():
    p = _parse(TIMER)
    assert p.has_option("Timer", "OnUnitActiveSec")
    assert int(p.get("Timer", "OnUnitActiveSec")) <= 60     # frequent liveness probe
    assert p.get("Install", "WantedBy") == "timers.target"
