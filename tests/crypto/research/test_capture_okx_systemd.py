"""OKX Stage A: static checks for the BUILT-NOT-DEPLOYED systemd unit set.

Six tracked units, none installed/enabled: the two long-running collectors
(as-of REST + klines) and the two maintenance timer pairs that REUSE the
existing capture_core CLI with ``--root data/research/capture_core_okx``
(asof seal-yesterday compaction; klines retention). Timers are offset from
their Binance twins (01:30 / 00:10) so the two roots never compact at once.
"""
from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UNITS = {
    "rest": REPO / "systemd" / "mhde-capture-okx-rest.service",
    "klines": REPO / "systemd" / "mhde-capture-okx-klines.service",
    "asof_compact": REPO / "systemd" / "mhde-capture-okx-asof-compact.service",
    "asof_compact_timer": REPO / "systemd" / "mhde-capture-okx-asof-compact.timer",
    "klines_expire": REPO / "systemd" / "mhde-capture-okx-klines-expire.service",
    "klines_expire_timer": REPO / "systemd" / "mhde-capture-okx-klines-expire.timer",
}
OKX_ROOT = "data/research/capture_core_okx"


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


def test_all_units_exist():
    for name, path in UNITS.items():
        assert path.exists(), f"missing unit {name}: {path}"


@pytest.mark.parametrize("name", sorted(UNITS))
def test_unit_valid_syntax(name):
    rc, out = _verify(UNITS[name])
    assert rc == 0, out


@pytest.mark.parametrize("name", ["rest", "klines", "asof_compact", "klines_expire"])
def test_units_are_built_not_deployed_and_workdir(name):
    text = UNITS[name].read_text()
    assert "BUILT-NOT-DEPLOYED" in text
    assert _parse(UNITS[name])["Service"]["WorkingDirectory"] == "/home/jpcg/MHDE"


def test_collector_execstarts_run_okx_commands():
    rest = _parse(UNITS["rest"])["Service"]
    assert rest["ExecStart"].endswith("main.py crypto capture-okx-rest-run")
    assert rest["Type"] == "simple" and rest["Restart"] == "on-failure"
    klines = _parse(UNITS["klines"])["Service"]
    assert klines["ExecStart"].endswith("main.py crypto capture-okx-klines-run")


def test_maintenance_units_reuse_existing_cli_with_okx_root():
    asof = _parse(UNITS["asof_compact"])["Service"]
    assert asof["ExecStart"].endswith(
        f"main.py crypto capture-asof-compact --root {OKX_ROOT}")
    assert asof["Type"] == "oneshot"
    expire = _parse(UNITS["klines_expire"])["Service"]
    assert expire["ExecStart"].endswith(
        f"main.py crypto capture-klines-expire --root {OKX_ROOT}")
    assert expire["Type"] == "oneshot"


def test_timers_offset_from_binance_twins():
    asof_t = _parse(UNITS["asof_compact_timer"])["Timer"]
    expire_t = _parse(UNITS["klines_expire_timer"])["Timer"]
    assert asof_t["Persistent"] == "true" and expire_t["Persistent"] == "true"
    # Binance twins fire 01:30 (asof) / 00:10 (klines-expire); OKX must not collide.
    assert asof_t["OnCalendar"] != "*-*-* 01:30:00"
    assert expire_t["OnCalendar"] != "*-*-* 00:10:00"


def test_no_unit_enables_itself_into_the_live_capture_target():
    # Stage A ships parked: nothing may be wanted by mhde-capture.target or hook
    # into the deployed Binance units.
    for name, path in UNITS.items():
        assert "mhde-capture.target" not in path.read_text(), name
