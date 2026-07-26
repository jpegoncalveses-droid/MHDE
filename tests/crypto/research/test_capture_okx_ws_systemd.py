"""OKX Stage B WS: static checks for the BUILT-NOT-DEPLOYED systemd unit set.

Five tracked units, none installed/enabled: the Type=simple WS firehose collector (no sibling
timer) and the two Type=oneshot maintenance pairs that REUSE the shared capture_core firehose
CLI with ``--root data/research/capture_core_okx``. Timers are offset from both the Binance
firehose twins (expire 00:15 / compact :06) and the OKX Stage-A twins (klines-expire 00:20 /
asof-compact 02:00) so the roots never sweep concurrently on the shared disk.
"""
from __future__ import annotations

import configparser
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SYS = REPO / "systemd"
OKX_ROOT = "data/research/capture_core_okx"

WS_SERVICE = SYS / "mhde-capture-okx-ws.service"
EXPIRE_SERVICE = SYS / "mhde-capture-okx-firehose-expire.service"
EXPIRE_TIMER = SYS / "mhde-capture-okx-firehose-expire.timer"
COMPACT_SERVICE = SYS / "mhde-capture-okx-firehose-compact.service"
COMPACT_TIMER = SYS / "mhde-capture-okx-firehose-compact.timer"

ALL_UNITS = [WS_SERVICE, EXPIRE_SERVICE, EXPIRE_TIMER, COMPACT_SERVICE, COMPACT_TIMER]

# Binance firehose twins to stay clear of.
_BINANCE_EXPIRE_ONCAL = "*-*-* 00:15:00"
_BINANCE_COMPACT_ONCAL = "*-*-* *:06:00"


def _parse(unit_path: Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(unit_path)
    return p


def test_all_units_present():
    for u in ALL_UNITS:
        assert u.exists(), f"missing unit {u.name}"


def test_ws_daemon_is_type_simple_with_no_timer():
    p = _parse(WS_SERVICE)
    assert p["Service"]["Type"] == "simple"
    assert p["Service"]["Restart"] == "on-failure"
    assert p["Service"]["TimeoutStopSec"] == "30"          # flush before SIGKILL
    assert "MemoryMax" in p["Service"]
    assert p["Service"]["ExecStart"].endswith("crypto capture-okx-ws-run")
    assert p["Install"]["WantedBy"] == "default.target"
    # a never-exiting daemon has NO sibling timer
    assert not (SYS / "mhde-capture-okx-ws.timer").exists()


def test_maintenance_are_oneshot_reusing_shared_cli_with_okx_root():
    exp = _parse(EXPIRE_SERVICE)
    assert exp["Service"]["Type"] == "oneshot"
    assert f"capture-firehose-expire --root {OKX_ROOT}" in exp["Service"]["ExecStart"]

    comp = _parse(COMPACT_SERVICE)
    assert comp["Service"]["Type"] == "oneshot"
    assert f"--root {OKX_ROOT}" in comp["Service"]["ExecStart"]
    assert "capture-firehose-compact" in comp["Service"]["ExecStart"]


def test_maintenance_timers_persistent_and_offset_from_binance_twins():
    for timer, binance_oncal in ((EXPIRE_TIMER, _BINANCE_EXPIRE_ONCAL),
                                 (COMPACT_TIMER, _BINANCE_COMPACT_ONCAL)):
        p = _parse(timer)
        assert p["Timer"]["Persistent"] == "true"
        oncal = p["Timer"]["OnCalendar"]
        assert oncal != binance_oncal, f"{timer.name} collides with the Binance twin"
        assert oncal not in ("*-*-* 00:20:00", "*-*-* 02:00:00")   # clear of OKX Stage-A twins
        assert p["Install"]["WantedBy"] == "timers.target"


def test_all_units_built_not_deployed_and_not_wanted_by_capture_target():
    for u in ALL_UNITS:
        text = u.read_text()
        assert "BUILT-NOT-DEPLOYED" in text, f"{u.name} missing the parked marker"
        assert "mhde-capture.target" not in text, f"{u.name} must not auto-enable"
