"""Static checks for the ADR-039 sharded capture systemd units (gap 3, BUILD-ONLY).

Three new units ship tracked-but-not-deployed: the snapshot-owner, the per-shard template
(@.service), and the grouping target. Both services are Type=notify with WatchdogSec (the
run paths emit raw sd_notify). REST stays mainnet; cpuset (AllowedCPUs) is deferred to gap 4;
the shard->owner edge is soft (Wants=, never Requires=) so an owner crash never cycles shards.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/jpcg/MHDE")
OWNER = REPO / "systemd" / "mhde-capture-owner.service"
SHARD = REPO / "systemd" / "mhde-capture-core@.service"
TARGET = REPO / "systemd" / "mhde-capture.target"


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


# -- existence + syntax (systemd-analyze) -------------------------------------

def test_units_exist():
    assert OWNER.exists()
    assert SHARD.exists()
    assert TARGET.exists()


def test_owner_valid_syntax():
    rc, out = _verify(OWNER)
    assert rc == 0, out
    assert "Unknown key" not in out          # StartLimit* must be in [Unit], not [Service]


def test_shard_template_valid_syntax():
    rc, out = _verify(SHARD)
    assert rc == 0, out
    assert "Unknown key" not in out


def test_target_valid_syntax():
    rc, out = _verify(TARGET)
    assert rc == 0, out


# -- shared shape of the two notify services ----------------------------------

def _assert_notify_service(p: configparser.ConfigParser, unit: Path) -> None:
    assert p.get("Service", "Type") == "notify"
    assert p.has_option("Service", "WatchdogSec")
    assert int(p.get("Service", "WatchdogSec")) >= 15
    # capture-family --user convention (cloned from mhde-capture-core.service)
    assert not p.has_option("Service", "User")
    assert not p.has_option("Service", "Group")
    assert p.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert p.has_option("Service", "ExecStartPre")
    assert p.get("Service", "Restart") in ("on-failure", "always")
    assert int(p.get("Service", "TimeoutStopSec")) >= 60          # ADR-039 §E clean stop
    assert p.get("Service", "StandardOutput") == "journal"
    assert p.get("Service", "StandardError") == "journal"
    # priority caps: engine wins CPU/IO; capture is first OOM victim
    assert p.get("Service", "OOMScoreAdjust") == "800"
    assert int(p.get("Service", "CPUWeight")) <= 20
    assert int(p.get("Service", "IOWeight")) <= 20
    assert p.has_option("Service", "MemoryMax")
    # crash-LOOP -> failed (StartLimit* parsed under [Unit], the correct section)
    assert int(p.get("Unit", "StartLimitBurst")) >= 1
    # REST mainnet: NO env override that could flip the base to testnet
    assert not p.has_option("Service", "Environment")
    assert not p.has_option("Service", "EnvironmentFile")
    # cpuset deferred to gap 4: AllowedCPUs is a silent no-op on --user units today
    assert not p.has_option("Service", "AllowedCPUs")
    assert "mhde.duckdb" not in unit.read_text()


def test_owner_service_shape():
    p = _parse(OWNER)
    _assert_notify_service(p, OWNER)
    exec_start = p.get("Service", "ExecStart")
    assert "capture-owner-run" in exec_start
    assert "--socket" in exec_start
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start


def test_shard_template_shape():
    p = _parse(SHARD)
    _assert_notify_service(p, SHARD)
    exec_start = p.get("Service", "ExecStart")
    assert "capture-core-run" in exec_start
    assert "--shard %i" in exec_start
    assert "--of 8" in exec_start
    assert "--snapshot-socket" in exec_start
    assert "/home/jpcg/MHDE/venv/bin/python" in exec_start


def test_shard_wants_owner_soft_not_requires():
    # Resilience (ADR-039 §A): a shard must SURVIVE an owner crash-loop -> soft Wants=/After=,
    # NEVER Requires= (which would cascade owner failure to all 8 shards).
    p = _parse(SHARD)
    assert "mhde-capture-owner.service" in p.get("Unit", "After")
    assert "mhde-capture-owner.service" in p.get("Unit", "Wants")
    assert not p.has_option("Unit", "Requires")
    assert not p.has_option("Unit", "BindsTo")


def test_target_groups_owner_and_eight_shards():
    text = TARGET.read_text()
    assert "mhde-capture-owner.service" in text
    for i in range(8):
        assert f"mhde-capture-core@{i}.service" in text
    assert _parse(TARGET).get("Install", "WantedBy") == "default.target"


def test_legacy_single_process_unit_left_untouched():
    # The sharded units are self-contained; the legacy Type=simple unit stays as-is so its
    # own test (asserts Type==simple) keeps passing.
    legacy = REPO / "systemd" / "mhde-capture-core.service"
    assert _parse(legacy).get("Service", "Type") == "simple"
