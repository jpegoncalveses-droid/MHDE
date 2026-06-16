"""ADR-039 gap 4 — cpuset core-pinning drop-ins (BUILD-ONLY).

Pins capture (8 shards + owner) to the band 4-7, disjoint from the engine band 0-3 on the
8-core host (N unchanged at 8; the band is sized to ~4 cores, kernel-balanced). All three
drop-ins are INERT until the sudo cpuset-delegation drop-in is installed — AllowedCPUs is a
silent no-op on --user units until app.slice gets the cpuset controller (ADR-039 §B).
"""
from __future__ import annotations

import configparser
from pathlib import Path

REPO = Path("/home/jpcg/MHDE")
SYSTEMD = REPO / "systemd"

SHARD_CPUSET = SYSTEMD / "mhde-capture-core@.service.d" / "cpuset.conf"
OWNER_CPUSET = SYSTEMD / "mhde-capture-owner.service.d" / "cpuset.conf"
DELEGATE = SYSTEMD / "system" / "user@.service.d" / "10-cpuset-delegate.conf"

CAPTURE_BAND = "4-7"
ENGINE_BAND = set(range(0, 4))          # 0-3
CAPTURE_CORES = set(range(4, 8))        # 4-7


def _parse(p: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(p)
    return cp


def _cores(spec: str) -> set:
    out = set()
    for part in spec.split():
        for token in part.split(","):
            if "-" in token:
                lo, hi = token.split("-")
                out.update(range(int(lo), int(hi) + 1))
            elif token:
                out.add(int(token))
    return out


def test_cpuset_dropins_exist():
    assert SHARD_CPUSET.exists()
    assert OWNER_CPUSET.exists()
    assert DELEGATE.exists()


def test_shards_and_owner_pinned_to_capture_band():
    for f in (SHARD_CPUSET, OWNER_CPUSET):
        assert _parse(f).get("Service", "AllowedCPUs") == CAPTURE_BAND


def test_capture_band_is_four_cores_disjoint_from_engine():
    cores = _cores(CAPTURE_BAND)
    assert cores == CAPTURE_CORES                 # sized to ~4 cores (operator directive)
    assert len(cores) == 4
    assert cores.isdisjoint(ENGINE_BAND)          # provably disjoint from the engine band 0-3
    assert cores <= set(range(0, 8))              # within the 8-core host


def test_delegation_dropin_adds_cpuset_and_is_sudo_documented():
    p = _parse(DELEGATE)
    assert "cpuset" in p.get("Service", "Delegate").split()   # the blocker fix
    text = DELEGATE.read_text()
    assert "sudo" in text                                     # documents the operator install
    assert "DO NOT install with `systemctl --user`" in text


def test_base_units_carry_no_inline_allowedcpus():
    # cpuset lives ONLY in the drop-ins: the gap-3 base units stay AllowedCPUs-free (their
    # tests keep passing, and the band can be lifted without editing the units).
    for name in ("mhde-capture-owner.service", "mhde-capture-core@.service"):
        assert not _parse(SYSTEMD / name).has_option("Service", "AllowedCPUs")
