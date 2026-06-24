"""Component 2 — the brain TICK-loop systemd unit (continuous, BUILT-NOT-DEPLOYED)."""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

_SYSTEMD = Path(__file__).resolve().parents[3] / "systemd"
SVC = _SYSTEMD / "mhde-brain-tick.service"


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


def test_unit_exists_and_is_valid():
    assert SVC.exists()
    rc, out = _verify(SVC)
    assert rc == 0, f"verify {SVC} failed:\n{out}"


def test_is_a_continuous_service_running_the_tick_loop():
    svc = _parse(SVC)
    assert svc.get("Service", "Type") == "simple"            # continuous, NOT oneshot
    assert svc.get("Service", "Restart") == "on-failure"
    exec_start = svc.get("Service", "ExecStart")
    assert "-m crypto.research.brain.runner" in exec_start    # the tick loop entrypoint
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start
    assert "brain-discover-run" not in exec_start             # NOT the discovery batch
    assert svc.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert not svc.has_option("Service", "User")             # user-scope


def test_carries_shared_host_resource_caps():
    svc = _parse(SVC)
    assert svc.get("Service", "OOMScoreAdjust") == "800"     # OOM-first vs the engine
    assert int(svc.get("Service", "CPUWeight")) <= 20
    assert int(svc.get("Service", "IOWeight")) <= 20
    assert svc.get("Service", "Nice") == "19"
    assert svc.get("Service", "IOSchedulingClass") == "idle"
    assert svc.has_option("Service", "MemoryMax")


def test_built_not_deployed_and_never_touches_the_production_db():
    text = SVC.read_text()
    assert "BUILT-NOT-DEPLOYED" in text
    assert "mhde.duckdb" not in text


def test_installable_but_not_a_timer():
    svc = _parse(SVC)
    # a continuous service is enableable for manual deploy, but it is NOT timer-driven
    assert svc.get("Install", "WantedBy") == "default.target"
    assert not (_SYSTEMD / "mhde-brain-tick.timer").exists()
