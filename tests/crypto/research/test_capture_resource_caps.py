"""Static checks: every capture unit carries the four PR-3 resource-cap directives,
and the cross-slice deprioritize drop-in is present (built-not-deployed)."""
from __future__ import annotations

import configparser
from pathlib import Path

REPO = Path("/home/jpcg/MHDE")
SYSTEMD = REPO / "systemd"

CAPTURE_SERVICES = [
    "mhde-capture-core.service",
    "mhde-capture-rest-collector.service",
    "mhde-capture-klines.service",
    "mhde-capture-klines-expire.service",
]
CAP_DIRECTIVES = ("MemoryMax", "CPUWeight", "IOWeight", "OOMScoreAdjust")
SLICE_DROPIN = SYSTEMD / "system" / "user.slice.d" / "10-capture-deprioritize.conf"


def _parse(unit_path: Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(unit_path)
    return p


def test_every_capture_service_carries_all_four_caps():
    for name in CAPTURE_SERVICES:
        p = _parse(SYSTEMD / name)
        for directive in CAP_DIRECTIVES:
            assert p.has_option("Service", directive), f"{name} missing {directive}"


def test_cpu_io_weights_below_system_slice_default():
    # system.slice default is 100; capture units must be strictly below so the
    # engine wins WITHIN-slice and (with the drop-in) cross-slice contention.
    for name in CAPTURE_SERVICES:
        p = _parse(SYSTEMD / name)
        assert int(p.get("Service", "CPUWeight")) < 100
        assert int(p.get("Service", "IOWeight")) < 100
        assert int(p.get("Service", "OOMScoreAdjust")) > 0   # capture is OOM-first


def test_firehose_has_the_largest_memory_ceiling():
    fire = _parse(SYSTEMD / "mhde-capture-core.service").get("Service", "MemoryMax")
    rest = _parse(SYSTEMD / "mhde-capture-rest-collector.service").get("Service", "MemoryMax")
    assert fire == "4G" and rest == "1G"


def test_slice_dropin_present_and_deprioritizes_user_slice():
    assert SLICE_DROPIN.exists()
    p = _parse(SLICE_DROPIN)
    assert int(p.get("Slice", "CPUWeight")) < 100
    assert int(p.get("Slice", "IOWeight")) < 100


def test_slice_dropin_is_not_a_user_unit_and_documents_sudo_install():
    raw = SLICE_DROPIN.read_text()
    assert "systemctl --user" in raw and "DO NOT install" in raw   # warns against user install
    assert "/etc/systemd/system/user.slice.d/" in raw             # documents the target
