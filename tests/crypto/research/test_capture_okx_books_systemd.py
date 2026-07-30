"""OKX Stage C — BUILT-NOT-DEPLOYED depth daemon + the retention flags on the OKX maintenance units.

The books depth collector is its own Type=simple unit (not folded into the live Stage B ws unit),
and the OKX firehose expire/compact units carry the depth-specific flags (--depth-days /
--include-depth-state) that the Binance twins must NOT.
"""
from __future__ import annotations

import configparser
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SYS = REPO / "systemd"

BOOKS = SYS / "mhde-capture-okx-books.service"
OKX_EXPIRE = SYS / "mhde-capture-okx-firehose-expire.service"
OKX_COMPACT = SYS / "mhde-capture-okx-firehose-compact.service"
BINANCE_EXPIRE = SYS / "mhde-capture-firehose-expire.service"
BINANCE_COMPACT = SYS / "mhde-capture-firehose-compact.service"


def _parse(p):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(p)
    return cp


def test_books_daemon_is_type_simple_built_not_deployed():
    assert BOOKS.exists()
    p = _parse(BOOKS)
    assert p["Service"]["Type"] == "simple"
    assert p["Service"]["Restart"] == "on-failure"
    assert int(p["Service"]["TimeoutStopSec"]) >= 30
    assert p["Service"]["OOMScoreAdjust"] == "800"
    assert p["Service"]["ExecStart"].endswith("crypto capture-okx-books-run")
    assert p["Install"]["WantedBy"] == "default.target"
    text = BOOKS.read_text()
    assert "BUILT-NOT-DEPLOYED" in text
    assert "mhde-capture.target" not in text               # ships parked
    assert not (SYS / "mhde-capture-okx-books.timer").exists()   # a daemon has no timer


def test_okx_maintenance_units_carry_depth_flags():
    exp = _parse(OKX_EXPIRE)["Service"]["ExecStart"]
    comp = _parse(OKX_COMPACT)["Service"]["ExecStart"]
    assert "--depth-days" in exp                            # tight raw-depth window on the OKX root
    assert "--include-depth-state" in comp                 # inode-monster fix on the OKX root


def test_binance_twins_do_not_carry_depth_flags():
    # Binance keeps depth at the shared 7d and its KI-159 depth_state status quo — untouched.
    assert "--depth-days" not in _parse(BINANCE_EXPIRE)["Service"]["ExecStart"]
    assert "--include-depth-state" not in _parse(BINANCE_COMPACT)["Service"]["ExecStart"]
