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
    # drift Fix 4: BEST-EFFORT at the LOWEST priority (7), no longer class-idle. Idle-class
    # IO is starved indefinitely while ANY best-effort IO runs anywhere on the host — with 8
    # capture shards writing continuously the brain's reads queued behind everything (the
    # measured live-vs-gate fast-tick gap), and every refault re-read paid that queue. BE-7
    # still yields to every higher-priority BE task (engine, capture at the BE default) but
    # is never starved outright. IOWeight=20 keeps the cgroup-level ceiling.
    assert svc.get("Service", "IOSchedulingClass") == "best-effort"
    assert svc.get("Service", "IOSchedulingPriority") == "7"
    # drift Fix 4: 3G, was 2G. Measured at 2G over one night: the cap was hit 316,958 times,
    # 21.9M direct-reclaim scans, 9.87M file refaults ~= 37.6 GiB re-read from disk — the
    # tape+store working set (~1.0G anon + cache) cannot live in 2G beside itself. 3G leaves
    # the host ~4.9G available worst-case (15.6G total; capture ~2.7G live-critical aggregate,
    # streamlit ~2G) — the host aggregate stays the binding limit, and OOMScoreAdjust=800
    # still sacrifices the brain first.
    assert svc.get("Service", "MemoryMax") == "3G"


def test_built_not_deployed_and_never_touches_the_production_db():
    text = SVC.read_text()
    assert "BUILT-NOT-DEPLOYED" in text
    assert "mhde.duckdb" not in text


def test_installable_but_not_a_timer():
    svc = _parse(SVC)
    # a continuous service is enableable for manual deploy, but it is NOT timer-driven
    assert svc.get("Install", "WantedBy") == "default.target"
    assert not (_SYSTEMD / "mhde-brain-tick.timer").exists()
