"""Static checks for the capture-core hourly closed-hour compaction systemd units
(ADR-038). A daily/hourly oneshot timer that merges CLOSED-hour small files into
~1 file/partition/hour. Built-not-deployed; filesystem-only; never opens the DB.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
COMPACT_SVC = REPO / "systemd" / "mhde-capture-firehose-compact.service"
COMPACT_TIMER = REPO / "systemd" / "mhde-capture-firehose-compact.timer"


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
    assert COMPACT_SVC.exists() and COMPACT_TIMER.exists()


def test_units_valid_syntax():
    for unit in (COMPACT_SVC, COMPACT_TIMER):
        rc, out = _verify(unit)
        assert rc == 0, f"verify of {unit} failed:\n{out}"


def test_compact_is_oneshot_invoking_the_recent_compactor():
    svc = _parse(COMPACT_SVC)
    assert svc.get("Service", "Type") == "oneshot"
    assert "capture-firehose-compact-recent" in svc.get("Service", "ExecStart")
    assert "/home/jpcg/MHDE/venv/bin/python" in svc.get("Service", "ExecStart")
    assert svc.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert not svc.has_option("Service", "User")
    timer = _parse(COMPACT_TIMER)
    assert timer.has_option("Timer", "OnCalendar")
    assert timer.get("Timer", "Persistent") == "true"
    assert timer.get("Install", "WantedBy") == "timers.target"


def test_compact_carries_resource_caps_shared_host_invariant():
    # Compaction reads+rewrites under live capture -> OOM-first + below the
    # system.slice CPU/IO default so it never disturbs the engine (ADR-036/038).
    svc = _parse(COMPACT_SVC)
    assert svc.get("Service", "OOMScoreAdjust") == "800"
    assert int(svc.get("Service", "CPUWeight")) <= 20
    assert int(svc.get("Service", "IOWeight")) <= 20
    assert svc.has_option("Service", "MemoryMax")


def test_units_do_not_touch_production_db():
    for unit in (COMPACT_SVC, COMPACT_TIMER):
        assert "mhde.duckdb" not in unit.read_text()
