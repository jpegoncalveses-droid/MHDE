"""ADR-039 §D layer-2 — the per-shard heartbeat producer in CaptureService.

Each shard writes {shard, ts_ns, dispatched, bytes_in, rows} to the heartbeat dir on a cadence,
atomically; the stall-detector timer reads them across shards to spot a hung/asymmetric shard.
"""
from __future__ import annotations

import json

from crypto.research.capture_core.service import CaptureService


class _StubClient:
    def fetch_usdtm_perp_universe(self):
        return ["BTCUSDT"]


def _svc(tmp_path, **kw):
    return CaptureService(
        root=str(tmp_path), client=_StubClient(), n_shards=8,
        install_signals=False, enable_snapshots=False,
        disk_guard_enabled=False, inode_guard_enabled=False, **kw)


def test_service_writes_heartbeat_with_shard_and_stats(tmp_path):
    hbdir = tmp_path / "hb"
    svc = _svc(tmp_path, shard=3, heartbeat_dir=str(hbdir))

    class _Mgr:
        dispatched = 1234
        bytes_in = 9999

    svc._current_mgr = _Mgr()
    svc._write_heartbeat()
    data = json.loads((hbdir / "shard-3.json").read_text())
    assert data["shard"] == "3"
    assert data["dispatched"] == 1234
    assert data["bytes_in"] == 9999
    assert isinstance(data["rows"], int)
    assert data["ts_ns"] > 0


def test_heartbeat_with_no_mgr_is_safe(tmp_path):
    hbdir = tmp_path / "hb"
    svc = _svc(tmp_path, shard=0, heartbeat_dir=str(hbdir))
    svc._current_mgr = None                        # transient during a rebuild
    svc._write_heartbeat()                         # must not raise
    data = json.loads((hbdir / "shard-0.json").read_text())
    assert data["dispatched"] == 0 and data["bytes_in"] == 0


def test_heartbeat_cadence_throttles_to_interval(tmp_path):
    hbdir = tmp_path / "hb"
    svc = _svc(tmp_path, shard=0, heartbeat_dir=str(hbdir), heartbeat_interval_s=1000.0)
    svc._maybe_write_heartbeat()                   # first tick (last=0) writes
    assert (hbdir / "shard-0.json").exists()
    (hbdir / "shard-0.json").unlink()
    svc._maybe_write_heartbeat()                   # within the interval -> skip
    assert not (hbdir / "shard-0.json").exists()
