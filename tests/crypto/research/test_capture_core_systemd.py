"""Static checks for the capture-core systemd unit file.

Unlike the signal-probe (oneshot + 60s timer), capture-core is a persistent
Type=simple service with NO timer — it relies on user linger to stay up. It is
shipped built-not-deployed: the file exists in the repo but PR-1 installs/enables
nothing.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
SERVICE = REPO / "systemd" / "mhde-capture-core.service"


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


def test_unit_exists():
    assert SERVICE.exists()


def test_service_valid_syntax():
    rc, out = _verify(SERVICE)
    assert rc == 0, f"verify of {SERVICE} failed:\n{out}"


def test_service_is_persistent_user_unit():
    p = _parse(SERVICE)
    assert p.get("Service", "Type") == "simple"          # long-running, not oneshot
    assert not p.has_option("Service", "User")           # systemd --user unit
    assert not p.has_option("Service", "Group")
    assert p.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert p.get("Service", "Restart") in ("on-failure", "always")


def test_service_invokes_capture_core_run():
    p = _parse(SERVICE)
    exec_start = p.get("Service", "ExecStart")
    assert "capture-core-run" in exec_start
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start


def test_service_has_venv_preflight_and_journal_logging():
    p = _parse(SERVICE)
    assert "venv/bin/python" in SERVICE.read_text()
    assert "ExecStartPre" in SERVICE.read_text()
    assert p.get("Service", "StandardOutput") == "journal"
    assert p.get("Service", "StandardError") == "journal"


def test_service_does_not_touch_production_db():
    assert "mhde.duckdb" not in SERVICE.read_text()


def test_service_allows_clean_flush_on_stop():
    # SIGTERM must give the writer time to flush open parquet buffers.
    p = _parse(SERVICE)
    assert int(p.get("Service", "TimeoutStopSec")) >= 15


def test_no_timer_file_for_capture_core():
    # Persistent service => relies on linger, not a timer.
    assert not (REPO / "systemd" / "mhde-capture-core.timer").exists()
