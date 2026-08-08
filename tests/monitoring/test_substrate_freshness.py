"""Substrate-freshness monitor.

Fires when the brain stops producing labels/primitives OR any capture dataset stops
receiving writes — measured by write RECENCY, never process-liveness. During the
2026-08-08 outage every systemd unit stayed "active" while nothing was written for
~14h; a liveness check would have stayed green. This monitor is that missing safety
net: it reads newest-write timestamps (parquet mtime / brain cursor recency) and
alerts when any monitored stream goes stale beyond its threshold.
"""
from __future__ import annotations

import os
import sqlite3

from monitoring import substrate_freshness as sf

NS = 1_000_000_000


def _sample(name, age_s, threshold_s, now_ns):
    newest = None if age_s is None else now_ns - int(age_s * NS)
    return sf.FreshnessSample(name=name, newest_ns=newest, threshold_s=threshold_s)


# -- pure evaluator ------------------------------------------------------------

def test_fresh_stream_is_not_stale():
    now = 1_000 * NS
    assert sf.evaluate_freshness([_sample("aggTrade", 60, 600, now)], now_ns=now) == []


def test_stream_older_than_threshold_is_stale():
    now = 100_000 * NS
    stale = sf.evaluate_freshness([_sample("brain", 1200, 900, now)], now_ns=now)
    assert [s.name for s in stale] == ["brain"]
    assert stale[0].age_s == 1200


def test_missing_stream_none_newest_is_stale():
    now = 100_000 * NS
    stale = sf.evaluate_freshness([_sample("markPrice", None, 600, now)], now_ns=now)
    assert [s.name for s in stale] == ["markPrice"]
    assert stale[0].age_s is None                      # never written at all


def test_boundary_exactly_at_threshold_is_not_stale():
    now = 100_000 * NS
    # age == threshold is within tolerance; only strictly-older is stale.
    assert sf.evaluate_freshness([_sample("depth", 600, 600, now)], now_ns=now) == []


# -- run() -> MonitorResult ----------------------------------------------------

def test_run_ok_when_all_fresh():
    now = 100_000 * NS
    samples = [_sample("aggTrade", 60, 600, now), _sample("brain", 30, 900, now)]
    res = sf.run(samples=samples, now_ns=now)
    assert res.monitor == "substrate_freshness"
    assert res.status == "ok"


def test_run_fails_and_names_every_stale_stream():
    now = 100_000 * NS
    samples = [_sample("aggTrade", 60, 600, now),      # fresh
               _sample("markPrice", None, 600, now),   # never written
               _sample("brain", 5000, 900, now)]       # stale
    res = sf.run(samples=samples, now_ns=now)
    assert res.status == "fail"
    assert res.severity in ("warn", "critical")
    assert "markPrice" in res.body and "brain" in res.body
    assert "aggTrade" not in res.body                  # fresh streams not named
    assert res.metrics["stale_count"] == 2


# -- capture gatherer (newest parquet mtime, bounded to given dates) -----------

def test_newest_parquet_mtime_returns_max_mtime(tmp_path):
    ds = tmp_path / "aggTrade" / "symbol=BTCUSDT" / "date=2026-08-08"
    ds.mkdir(parents=True)
    old = ds / "part-a.parquet"; old.write_bytes(b"x"); os.utime(old, ns=(3 * NS, 3 * NS))
    new = ds / "part-b.parquet"; new.write_bytes(b"x"); os.utime(new, ns=(9 * NS, 9 * NS))
    got = sf.newest_parquet_mtime_ns(str(tmp_path / "aggTrade"), dates=["2026-08-08"])
    assert got == 9 * NS


def test_newest_parquet_mtime_none_when_no_files(tmp_path):
    (tmp_path / "aggTrade" / "symbol=X" / "date=2026-08-08").mkdir(parents=True)
    assert sf.newest_parquet_mtime_ns(str(tmp_path / "aggTrade"),
                                      dates=["2026-08-08"]) is None


def test_newest_parquet_mtime_none_when_dataset_absent(tmp_path):
    assert sf.newest_parquet_mtime_ns(str(tmp_path / "nope"), dates=["2026-08-08"]) is None


# -- brain gatherer (max reader_cursor.updated_at_ns) --------------------------

def test_brain_cursor_recency_reads_max_updated_at(tmp_path):
    db = tmp_path / "registry.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reader_cursor (reader TEXT PRIMARY KEY, "
                 "last_recv_ts_ns INTEGER, updated_at_ns INTEGER)")
    conn.executemany("INSERT INTO reader_cursor VALUES (?,?,?)",
                     [("trades", 1, 100), ("markprice", 1, 250)])
    conn.commit(); conn.close()
    assert sf.brain_cursor_recency_ns(str(db)) == 250


def test_brain_cursor_recency_none_when_db_missing(tmp_path):
    assert sf.brain_cursor_recency_ns(str(tmp_path / "missing.sqlite")) is None
