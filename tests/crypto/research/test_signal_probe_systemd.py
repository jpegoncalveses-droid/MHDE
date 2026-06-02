"""Static checks for the signal-probe collector systemd unit files.

Mirrors tests/crypto/test_systemd_units.py: parse the files and run
``systemd-analyze verify`` to catch syntax mistakes before they hit the host.
The probe writes ONLY the separate research DB, so (unlike the universe units)
there is no mhde.duckdb pre-flight to assert.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
UNITS_DIR = REPO / "systemd"
SERVICE = UNITS_DIR / "mhde-signal-probe-collector.service"
TIMER = UNITS_DIR / "mhde-signal-probe-collector.timer"


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
    assert SERVICE.exists()
    assert TIMER.exists()


def test_service_valid_syntax():
    rc, out = _verify(SERVICE)
    assert rc == 0, f"verify of {SERVICE} failed:\n{out}"


def test_timer_valid_syntax():
    rc, out = _verify(TIMER)
    assert rc == 0, f"verify of {TIMER} failed:\n{out}"


def test_service_is_user_unit_without_user_line():
    # systemd --user unit: runs as the invoking user, so NO User=/Group= line.
    p = _parse(SERVICE)
    assert not p.has_option("Service", "User")
    assert not p.has_option("Service", "Group")
    assert p.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert p.get("Service", "Type") == "oneshot"


def test_service_caps_runtime_under_cadence():
    # A hung cycle must not overlap the next 60s fire.
    p = _parse(SERVICE)
    assert int(p.get("Service", "TimeoutStartSec")) < 60


def test_service_invokes_collector_cli():
    p = _parse(SERVICE)
    exec_start = p.get("Service", "ExecStart")
    assert "signal-probe-collect" in exec_start
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start


def test_service_has_venv_preflight():
    raw = SERVICE.read_text()
    assert "ExecStartPre" in raw
    assert "venv/bin/python" in raw


def test_service_does_not_touch_production_db():
    # The probe must never reference the production DB in its unit.
    raw = SERVICE.read_text()
    assert "mhde.duckdb" not in raw


def test_service_logs_to_journal():
    p = _parse(SERVICE)
    assert p.get("Service", "StandardOutput") == "journal"
    assert p.get("Service", "StandardError") == "journal"


def test_timer_60s_monotonic_cadence():
    p = _parse(TIMER)
    assert p.get("Timer", "OnUnitActiveSec") == "60"
    # Monotonic cadence (skip missed cycles): no Persistent / OnCalendar.
    assert not p.has_option("Timer", "Persistent")
    assert not p.has_option("Timer", "OnCalendar")


def test_timer_installs_to_timers_target():
    p = _parse(TIMER)
    assert p.get("Install", "WantedBy") == "timers.target"
