"""Brain-store retention: parquet date-partition expiry + registry bookkeeping prune
+ free-space-guarded VACUUM.

The brain store had NO retention while every capture dataset does, so it grew
unbounded (parquet back to store inception, a ~9.5G registry). This bounds it to what
discovery needs: BRAIN_STORE_RETENTION_DAYS of parquet (>= DISCOVERY_HISTORY_DAYS +
max label horizon + margin) and a shorter cursor-lag-sized window for the registry
bookkeeping (its only consumer is the tick's write-dedup, checked near the monotonic
cursor — never discovery, which reads parquet).
"""
from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime

from crypto.research.brain import config as cfg
from crypto.research.brain import registry
from crypto.research.brain import retention as ret
from crypto.research.brain.discovery import config as dcfg


def _now_ms(date="2026-08-08"):
    return int(datetime.fromisoformat(date + "T00:00:00+00:00").timestamp() * 1000)


def _now_ns(date="2026-08-08"):
    return int(datetime.fromisoformat(date + "T00:00:00+00:00").timestamp() * 1_000_000_000)


def _mk_partition(root, dataset, symbol, date):
    p = pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date}")
    p.mkdir(parents=True)
    (p / "part-x.parquet").write_bytes(b"x")
    return p


def _seed_registry(path, rows):
    conn = registry.connect(str(path))
    conn.executemany(
        "INSERT INTO snapshot_bookkeeping "
        "(dataset,symbol,window_start_ns,window_end_ns,recv_ts_ns,n_events,written_at_ns) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO reader_cursor VALUES ('trades', 999, 999)")
    conn.commit()
    conn.close()


# -- config sizing -------------------------------------------------------------

def test_retention_windows_are_sized_for_discovery():
    # parquet window must exceed discovery's read window (with room for the 720-min
    # label horizon + a discovery-read-race margin).
    assert cfg.BRAIN_STORE_RETENTION_DAYS == 21
    assert cfg.BRAIN_STORE_RETENTION_DAYS > dcfg.DISCOVERY_HISTORY_DAYS
    # registry bookkeeping window: shorter (cursor-lag-sized) but still >= the capture
    # raw retention (the max a cursor can lag before raw expires and the tick seeds past).
    assert cfg.BRAIN_REGISTRY_RETENTION_DAYS == 10
    assert cfg.BRAIN_REGISTRY_RETENTION_DAYS < cfg.BRAIN_STORE_RETENTION_DAYS


# -- expire_brain_partitions ---------------------------------------------------

def test_expire_removes_old_partitions_keeps_recent_and_today(tmp_path):
    root = str(tmp_path)
    old = _mk_partition(root, "labels", "BTCUSDT", "2026-07-01")        # 38d -> expire
    recent = _mk_partition(root, "labels", "BTCUSDT", "2026-07-20")     # 19d -> keep
    today = _mk_partition(root, "labels", "BTCUSDT", "2026-08-08")      # today -> keep
    removed = ret.expire_brain_partitions(root, days=21, datasets=["labels"], now_ms=_now_ms())
    assert not old.exists()
    assert recent.exists() and today.exists()
    assert removed == [str(old)]


def test_expire_only_touches_named_datasets(tmp_path):
    root = str(tmp_path)
    lab = _mk_partition(root, "labels", "BTCUSDT", "2026-07-01")
    prim = _mk_partition(root, "markprice", "BTCUSDT", "2026-07-01")
    ret.expire_brain_partitions(root, days=21, datasets=["labels"], now_ms=_now_ms())
    assert not lab.exists()          # in the list -> pruned
    assert prim.exists()             # not in the list -> untouched


def test_expire_keeps_partition_exactly_at_cutoff(tmp_path):
    # cutoff date = today - 21d = 2026-07-18; a partition AT the cutoff is kept
    # (only strictly-older partitions expire), so discovery never loses its floor day.
    root = str(tmp_path)
    at_cutoff = _mk_partition(root, "labels", "BTCUSDT", "2026-07-18")
    ret.expire_brain_partitions(root, days=21, datasets=["labels"], now_ms=_now_ms())
    assert at_cutoff.exists()


# -- prune_registry_bookkeeping ------------------------------------------------

def test_prune_deletes_bookkeeping_older_than_window_only(tmp_path):
    db = tmp_path / "registry.sqlite"
    old_ns = _now_ns("2026-07-20")       # 19d -> pruned (> 10d)
    keep_ns = _now_ns("2026-08-05")      # 3d  -> kept
    _seed_registry(db, [
        ("markprice", "BTCUSDT", old_ns, old_ns + 1, old_ns, 1, old_ns),
        ("markprice", "BTCUSDT", keep_ns, keep_ns + 1, keep_ns, 1, keep_ns),
    ])
    deleted = ret.prune_registry_bookkeeping(str(db), days=10, now_ns=_now_ns())
    assert deleted == 1
    conn = sqlite3.connect(db)
    try:
        rows = [r[0] for r in conn.execute("SELECT window_start_ns FROM snapshot_bookkeeping")]
        assert rows == [keep_ns]
        assert conn.execute("SELECT count(*) FROM reader_cursor").fetchone()[0] == 1  # untouched
    finally:
        conn.close()


# -- vacuum_registry_if_space --------------------------------------------------

def test_vacuum_runs_when_space_and_bloat_ok(tmp_path):
    db = tmp_path / "registry.sqlite"
    _seed_registry(db, [("markprice", "BTCUSDT", 1, 2, 1, 1, 1)])
    did, reason = ret.vacuum_registry_if_space(str(db), headroom_factor=1.2,
                                               min_bloat_ratio=0.0,       # bypass bloat gate
                                               free_fn=lambda _p: 10 ** 12)
    assert did is True


def test_vacuum_skipped_when_free_space_insufficient(tmp_path):
    db = tmp_path / "registry.sqlite"
    _seed_registry(db, [("markprice", "BTCUSDT", 1, 2, 1, 1, 1)])
    did, reason = ret.vacuum_registry_if_space(str(db), headroom_factor=1.2,
                                               min_bloat_ratio=0.0,
                                               free_fn=lambda _p: 0)
    assert did is False
    assert "free" in reason.lower()      # explains it skipped for space


def test_vacuum_skipped_when_file_not_bloated(tmp_path):
    # A compact (unfragmented) registry should NOT be rewritten daily for ~0 gain, even
    # with ample free space — VACUUM only when there are enough freed pages to reclaim.
    db = tmp_path / "registry.sqlite"
    _seed_registry(db, [("markprice", "BTCUSDT", 1, 2, 1, 1, 1)])
    did, reason = ret.vacuum_registry_if_space(str(db), headroom_factor=1.2,
                                               min_bloat_ratio=0.99,      # impossibly high
                                               free_fn=lambda _p: 10 ** 12)
    assert did is False
    assert "bloat" in reason.lower() or "fragment" in reason.lower()


# -- run_retention orchestration ----------------------------------------------

def test_run_retention_expires_prunes_and_vacuums(tmp_path):
    root = tmp_path / "brain"
    old = _mk_partition(str(root), "labels", "BTCUSDT", "2026-07-01")
    db = root / "registry.sqlite"
    _seed_registry(db, [("markprice", "BTCUSDT", _now_ns("2026-07-20"), 1, 1, 1, 1)])
    summary = ret.run_retention(
        store_root=str(root), registry_path=str(db),
        store_days=21, registry_days=10, now_ms=_now_ms(),
        datasets=["labels"], free_fn=lambda _p: 10 ** 12,
        vacuum_min_bloat_ratio=0.0)
    assert not old.exists()
    assert summary["partitions_expired"] == 1
    assert summary["bookkeeping_pruned"] == 1
    assert summary["vacuumed"] is True
