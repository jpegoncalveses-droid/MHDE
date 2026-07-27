"""OKX Stage C — depth retention: the raw `depth` byte monster prunes on its own TIGHT window,
independently of depth_state (2d) and the other firehose datasets (7d). Keeps the box alive.
"""
from __future__ import annotations

import os
import pathlib

from crypto.research.capture_core import maintenance as mt

_NOW_MS = 1_700_000_000_000       # 2023-11-14


def _mk(root, dataset, date, symbol="BTCUSDT"):
    d = pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date}")
    d.mkdir(parents=True)
    (d / "part-x.parquet").write_bytes(b"placeholder")     # expiry rmtree's by date, never reads
    return str(d)


def test_expire_depth_partitions_selects_only_depth(tmp_path):
    root = str(tmp_path)
    _mk(root, "depth", "2023-11-10")            # old raw depth -> pruned
    depth_today = _mk(root, "depth", "2023-11-14")   # today -> kept
    ds_old = _mk(root, "depth_state", "2023-11-10")  # depth_state -> untouched by this path
    agg_old = _mk(root, "aggTrade", "2023-11-10")    # firehose -> untouched

    removed = mt.expire_depth_partitions(root, days=1, now_ms=_NOW_MS)

    assert len(removed) == 1
    assert "/depth/" in removed[0] and "date=2023-11-10" in removed[0]
    assert os.path.isdir(depth_today)           # today's depth kept
    assert os.path.isdir(ds_old)                # depth_state NOT pruned by the depth path
    assert os.path.isdir(agg_old)               # aggTrade NOT pruned


def test_depth_state_expires_on_its_own_two_day_window(tmp_path):
    root = str(tmp_path)
    ds_old = _mk(root, "depth_state", "2023-11-10")   # >2d old -> pruned
    ds_recent = _mk(root, "depth_state", "2023-11-13")  # within 2d -> kept
    removed = mt.expire_depth_state_partitions(root, days=2, now_ms=_NOW_MS)
    assert not os.path.isdir(ds_old) and os.path.isdir(ds_recent)
    assert removed and all("/depth_state/" in p for p in removed)
