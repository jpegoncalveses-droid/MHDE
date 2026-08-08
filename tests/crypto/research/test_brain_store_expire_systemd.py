"""Wiring for the brain-store retention sweep: the CLI verb + the systemd unit/timer.

Retention (retention.py) is built and unit-tested, but a maintenance job is only real once
something INVOKES it. This locks the wiring: ``crypto brain-store-expire`` runs
``run_retention`` and reports its summary; the unit is a oneshot invoking that verb with the
brain-family shared-host caps (idle IO, OOM-first, 2G backstop); the timer fires daily,
offset from the hourly compactor. BUILT-NOT-DEPLOYED — the operator installs it.
"""
from __future__ import annotations

import configparser
import pathlib
import shutil
import subprocess

from click.testing import CliRunner

import main as main_mod
from crypto.research.brain import registry

REPO = pathlib.Path(__file__).resolve().parents[3]
SVC = REPO / "systemd" / "mhde-brain-store-expire.service"
TIMER = REPO / "systemd" / "mhde-brain-store-expire.timer"


def _parse(unit_path: pathlib.Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.read_string(unit_path.read_text())
    return p


# -- systemd unit + timer ------------------------------------------------------

def test_units_exist():
    assert SVC.exists() and TIMER.exists()


def test_units_valid_syntax():
    analyze = shutil.which("systemd-analyze")
    if not analyze:
        import pytest
        pytest.skip("systemd-analyze unavailable")
    for unit in (SVC, TIMER):
        proc = subprocess.run([analyze, "verify", str(unit)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{unit.name}:\n{proc.stdout}{proc.stderr}"


def test_service_is_oneshot_invoking_brain_store_expire_with_family_caps():
    svc = _parse(SVC)
    assert svc.get("Service", "Type") == "oneshot"
    exec_start = svc.get("Service", "ExecStart")
    assert "brain-store-expire" in exec_start
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start
    assert svc.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert svc.get("Service", "Nice") == "19"
    assert svc.get("Service", "IOSchedulingClass") == "idle"     # never disturbs the tick
    assert svc.get("Service", "OOMScoreAdjust") == "800"         # OOM-first
    assert "BUILT-NOT-DEPLOYED" in SVC.read_text()


def test_timer_is_daily_offset_from_compactor():
    timer = _parse(TIMER)
    oncal = timer.get("Timer", "OnCalendar")
    assert "00:45" in oncal              # daily, after capture retention + the :36 compactor
    assert timer.get("Timer", "Persistent") == "true"
    assert timer.get("Install", "WantedBy") == "timers.target"


# -- CLI verb wires to run_retention -------------------------------------------

def test_cli_brain_store_expire_expires_prunes_and_reports(tmp_path):
    root = tmp_path / "brain"
    old = root / "labels" / "symbol=BTCUSDT" / "date=2020-01-01"   # ancient -> expire
    old.mkdir(parents=True)
    (old / "part-x.parquet").write_bytes(b"x")
    db = root / "registry.sqlite"
    conn = registry.connect(str(db))
    conn.execute("INSERT INTO snapshot_bookkeeping VALUES ('markprice','BTCUSDT',1,2,1,1,1)")
    conn.commit()
    conn.close()

    result = CliRunner().invoke(
        main_mod.cli, ["crypto", "brain-store-expire", "--root", str(root),
                       "--registry", str(db)])
    assert result.exit_code == 0, result.output
    assert "brain retention:" in result.output
    assert "expired 1 partitions" in result.output
    assert "pruned 1 bookkeeping" in result.output
    assert not old.exists()
