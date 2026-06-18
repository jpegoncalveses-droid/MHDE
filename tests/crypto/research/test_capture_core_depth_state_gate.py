"""DEPTH_STATE_ENABLED gates the online level book + depth_state writer.

PR #49 wired the maintainer's level book + the depth_state writer unconditionally,
so any shard restart (incl. the auto-restart on a crash) would activate them. This
flag keeps the maintainer CURSOR-ONLY (the proven pre-#49 behavior — no per-symbol
book, no fat level-carrying _Diff buffers, which are the reconnect-storm OOM source)
until depth-state capture is explicitly enabled. Default OFF is mandatory.

The flag must gate BOTH paths: the level FEED into the maintainer (the buffer/OOM
source) AND the depth_state WRITER. Gating only the writer would leave the fat
buffers growing.
"""
from __future__ import annotations

import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import service as svc

_A_BIDS = [["100.0", "5"], ["99.0", "3"], ["98.0", "2"]]
_A_ASKS = [["101.0", "4"], ["102.0", "6"], ["103.0", "1"]]


def _diff(symbol, U, u, pu, *, b=None, a=None):
    return {"e": "depthUpdate", "E": u, "T": u, "s": symbol, "U": U, "u": u, "pu": pu,
            "b": b or [], "a": a or []}


def _snap(luid, bids, asks):
    return {"lastUpdateId": luid, "E": 0, "bids": bids, "asks": asks}


def _read_state(root):
    rows = []
    for fp in sorted(pathlib.Path(root, "depth_state").rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


def _read_depth(root):
    rows = []
    for fp in sorted(pathlib.Path(root, "depth").rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


def _sync(s):
    """Seed + a bracket diff so the maintainer is synced (cursor established)."""
    s._on_snapshot_arrived("BTCUSDT", _snap(100, _A_BIDS, _A_ASKS), recv_ns=1)
    s._handle_depth(_diff("BTCUSDT", 99, 105, 98, b=[["100.0", "7"]]), recv_ns=2)
    return s._maintainers["BTCUSDT"]


# -- OFF (default): cursor-only, byte-identical to pre-#49 --

def test_gate_off_is_default(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None)
    assert s._depth_state_enabled is False               # default OFF (mandatory)


def test_gate_off_keeps_maintainer_cursor_only(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None)
    m = _sync(s)
    assert m.synced is True                              # cursor still works...
    assert m.bids == {} and m.asks == {}                # ...but NO level book is built


def test_gate_off_creates_no_depth_state_writer_and_writes_nothing(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None)
    assert s._depth_state is None                        # writer not created
    assert None not in s._writers                        # and not in the flush set
    _sync(s)
    s._maybe_write_book_states()                          # must be a no-op
    s.flush_all()
    assert _read_state(str(tmp_path)) == []              # no depth_state rows on disk


def test_gate_off_does_not_feed_levels_into_the_buffer(tmp_path):
    # The fat, level-carrying _Diff buffer is the reconnect-storm OOM source — when
    # OFF, an unsynced diff must buffer WITHOUT its level arrays.
    s = svc.CaptureService(root=str(tmp_path), client=None)
    s._handle_depth(_diff("ETHUSDT", 5, 10, 4, b=[["10.0", "1"]], a=[["11.0", "1"]]), recv_ns=1)
    m = s._maintainers["ETHUSDT"]
    assert m._buffer[-1].bids is None and m._buffer[-1].asks is None


def test_gate_off_still_persists_raw_depth(tmp_path):
    # The gate only changes what the MAINTAINER sees; the raw firehose is untouched.
    s = svc.CaptureService(root=str(tmp_path), client=None)
    s._handle_depth(_diff("BTCUSDT", 5, 10, 4, b=[["10.0", "1"]], a=[]), recv_ns=7)
    s.flush_all()
    raw = _read_depth(str(tmp_path))
    assert len(raw) == 1 and raw[0]["b"] == [["10.0", "1"]]   # raw b/a persisted verbatim


# -- ON: the full PR #49 behavior --

def test_gate_on_builds_book_and_feeds_buffer(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None, depth_state_enabled=True)
    m = _sync(s)
    assert m.synced is True
    assert m.bids and m.asks                              # level book is maintained
    # and the feed reaches the buffer on the unsynced path
    s._handle_depth(_diff("ETHUSDT", 5, 10, 4, b=[["10.0", "1"]], a=[]), recv_ns=3)
    assert s._maintainers["ETHUSDT"]._buffer[-1].bids == [["10.0", "1"]]


def test_gate_on_writes_depth_state(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None, depth_state_enabled=True)
    assert s._depth_state is not None and s._depth_state in s._writers
    _sync(s)
    s._maybe_write_book_states()
    s.flush_all()
    rows = _read_state(str(tmp_path))
    assert rows and rows[0]["s"] == "BTCUSDT" and rows[0]["valid"] is True
