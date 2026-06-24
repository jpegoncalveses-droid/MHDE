"""Component 8 (§9) — the brain-discovery systemd unit + timer (BUILT-NOT-DEPLOYED)."""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

_SYSTEMD = Path(__file__).resolve().parents[3] / "systemd"
SVC = _SYSTEMD / "mhde-brain-discover.service"
TIMER = _SYSTEMD / "mhde-brain-discover.timer"


def _parse(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(path)
    return cp


def _verify(path: Path):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available")
    p = subprocess.run(["systemd-analyze", "verify", "--user", str(path)],
                       capture_output=True, text=True, timeout=20)
    return p.returncode, p.stdout + p.stderr


def test_units_exist():
    assert SVC.exists() and TIMER.exists()


def test_units_valid_syntax():
    for unit in (SVC, TIMER):
        rc, out = _verify(unit)
        assert rc == 0, f"verify {unit} failed:\n{out}"


def test_service_is_oneshot_batch_invoking_the_discovery_command():
    svc = _parse(SVC)
    assert svc.get("Service", "Type") == "oneshot"
    exec_start = svc.get("Service", "ExecStart")
    assert "brain-discover-run" in exec_start
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start
    assert svc.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert not svc.has_option("Service", "User")             # user-scope


def test_service_carries_shared_host_resource_caps():
    svc = _parse(SVC)
    assert svc.get("Service", "OOMScoreAdjust") == "800"     # OOM-first vs the engine
    assert int(svc.get("Service", "CPUWeight")) <= 20
    assert int(svc.get("Service", "IOWeight")) <= 20
    assert svc.has_option("Service", "MemoryMax")
    assert svc.has_option("Service", "Nice")


def test_timer_schedules_periodically_and_installs():
    timer = _parse(TIMER)
    assert timer.has_option("Timer", "OnCalendar")
    assert timer.get("Timer", "Persistent") == "true"
    assert timer.get("Install", "WantedBy") == "timers.target"


def test_units_never_touch_the_production_db():
    for unit in (SVC, TIMER):
        assert "mhde.duckdb" not in unit.read_text()


def test_built_not_deployed_marker_present():
    assert "BUILT-NOT-DEPLOYED" in SVC.read_text()
