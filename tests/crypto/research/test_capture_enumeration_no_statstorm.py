"""Enumeration must not stat every file (#3): the O(total-files) `_dir_size` stat-storm.

`list_firehose_partitions` computes each partition's on-disk size via `_dir_size`, which
recursively stats EVERY file. Over a fragmented tape (millions of tiny part-*), that
enumeration alone pins memory (page cache) / OOMs the 1G-capped compaction + expire units
before any real work. But ONLY the disk guard needs the size (for size-based oldest-first
reclaim); the compaction and expire callers prune/compact by date+path and never read
`Partition.size`. So enumeration drops O(total-files) -> O(partition dirs) by gating the
size behind `with_size`, default True (guard unchanged), False for the four capped callers.

Pinned with a `_dir_size`-raises spy: a caller that no longer stats every file does not trip
it. One change covers compaction AND expire (they share the enumerator).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.capture_core import disk_guard as dg
from crypto.research.capture_core import maintenance

_OLD = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)        # well past any retention
_OLD_MS = int(_OLD.timestamp() * 1000)


def _agg_row(symbol="BTCUSDT", *, recv_ns, e_ms=_OLD_MS):
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": e_ms, "a": 1, "s": symbol,
            "p": "100.5", "q": "2.0", "f": 1, "l": 2, "T": e_ms, "m": False}


def _write_parts(root, n, *, symbol="BTCUSDT", e_ms=_OLD_MS):
    """n single-row part files in one symbol=/date= partition (date = day of e_ms)."""
    w = capture_store.aggtrade_writer(str(root), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for i in range(n):
        w.append(_agg_row(symbol, recv_ns=1000 + i, e_ms=e_ms))
        w.flush_due()                 # flush_max_bytes=1 -> one file per row
    w.flush_all()


def _ban_dir_size(monkeypatch):
    """Make `_dir_size` raise — any caller that still stats every file trips it."""
    def _boom(path):
        raise AssertionError(f"_dir_size called (stat-storm) on {path}")
    monkeypatch.setattr(dg, "_dir_size", _boom)


# -- the gate itself -----------------------------------------------------------

def test_with_size_false_skips_dir_size_and_returns_zero(tmp_path, monkeypatch):
    _write_parts(tmp_path, 3)
    _ban_dir_size(monkeypatch)
    parts = dg.list_firehose_partitions(str(tmp_path), ["aggTrade"], with_size=False)
    assert len(parts) == 1 and parts[0].size == 0          # enumerated, size not computed
    assert parts[0].date == "2026-05-01" and parts[0].path.endswith("date=2026-05-01")


def test_with_size_true_still_computes_size_for_the_guard(tmp_path):
    _write_parts(tmp_path, 3)
    parts = dg.list_firehose_partitions(str(tmp_path), ["aggTrade"])   # default True
    assert len(parts) == 1 and parts[0].size > 0           # the guard still gets real sizes


# -- the four capped callers must not stat every file --------------------------

def test_migrate_compact_does_not_stat_every_file(tmp_path, monkeypatch):
    _write_parts(tmp_path, 4)
    _ban_dir_size(monkeypatch)
    rep = maintenance.migrate_compact(str(tmp_path), datasets=["aggTrade"],
                                      dates=["2026-05-01"])
    assert rep.partitions_compacted == 1 and rep.rows_after == 4   # compacted, no stat-storm


def test_compact_firehose_closed_hours_does_not_stat_every_file(tmp_path, monkeypatch):
    _write_parts(tmp_path, 4)
    _ban_dir_size(monkeypatch)
    rep = maintenance.compact_firehose_closed_hours(
        str(tmp_path), datasets=["aggTrade"], now_ts=10 ** 12)   # far future -> hour closed
    assert rep.hours_compacted >= 1                          # merged, no stat-storm


def test_expire_firehose_partitions_does_not_stat_every_file(tmp_path, monkeypatch):
    _write_parts(tmp_path, 2)                                # date 2026-05-01, long expired
    _ban_dir_size(monkeypatch)
    removed = maintenance.expire_firehose_partitions(
        str(tmp_path), datasets=["aggTrade"], days=7, now_ms=int(datetime(
            2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000))
    assert len(removed) == 1                                 # pruned by date, no stat-storm


def test_expire_depth_state_does_not_stat_every_file(tmp_path, monkeypatch):
    # depth_state expire shares the enumerator; build an old depth_state partition.
    from crypto.research.capture_core import config as cfg
    part = tmp_path / cfg.DEPTH_STATE_DATASET / "symbol=BTCUSDT" / "date=2026-05-01"
    part.mkdir(parents=True)
    (part / "part-x.parquet").write_bytes(b"x")
    _ban_dir_size(monkeypatch)
    removed = maintenance.expire_depth_state_partitions(
        str(tmp_path), days=2, now_ms=int(datetime(
            2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000))
    assert len(removed) == 1                                 # pruned by date, no stat-storm
