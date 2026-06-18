"""The disk/inode guard enforcement runs OFF the asyncio flush loop.

Root cause of the watchdog-SIGABRT crash storm: DiskGuard.enforce() did its
scandir + rmtree of up-to-thousands of partitions SYNCHRONOUSLY on the flush loop,
and the systemd WATCHDOG=1 feed runs on that same loop right after it — so under a
prune storm a big sweep blocked the loop past WatchdogSec=30 and systemd delivered
SIGABRT. The fix offloads enforce to a worker thread as a background task: the loop
keeps iterating and feeding the watchdog (which still reflects GENUINE loop health),
and a concurrent prune can never touch the actively-written (today's) partition.
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

from crypto.research.capture_core import disk_guard as dg
from crypto.research.capture_core import service as svc


class _RecordingNotifier:
    def __init__(self) -> None:
        self.watchdogs = 0
        self.readies = 0

    def ready(self) -> None:
        self.readies += 1

    def watchdog(self) -> None:
        self.watchdogs += 1


class _SlowGuard:
    """Stands in for a big prune: enforce() blocks the WORKER THREAD (not the loop)."""
    halted = False

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s
        self.calls = 0

    def enforce(self):
        self.calls += 1
        time.sleep(self._sleep_s)
        return None


def _make_partition(root, dataset, symbol, date, nbytes=1000):
    d = pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date}")
    d.mkdir(parents=True)
    (d / "part-0.parquet").write_bytes(b"x" * nbytes)
    return d


# -- 1. a slow/large enforce does NOT block the loop or the watchdog feed --

def test_slow_enforce_runs_offloop_while_watchdog_keeps_feeding(tmp_path):
    async def go():
        feeds = _RecordingNotifier()
        slow = _SlowGuard(sleep_s=0.6)
        s = svc.CaptureService(root=str(tmp_path), client=None, disk_guard=slow,
                               inode_guard_enabled=False, notifier=feeds,
                               disk_check_interval_s=0.0)
        s._last_msg_monotonic = time.monotonic()        # live: feeds should fire
        s._maybe_enforce_guards()                         # launches enforce OFF the loop
        assert s._enforce_task is not None
        before = feeds.watchdogs
        for _ in range(5):                                # the loop keeps turning...
            s._maybe_enforce_guards()                     # in-flight -> no second task
            s._feed_watchdog_if_live()                    # ...and feeding the watchdog
            await asyncio.sleep(0.05)
        assert not s._enforce_task.done()                 # prune STILL running (concurrent)
        assert feeds.watchdogs - before >= 5              # watchdog fed DURING the prune
        await s._enforce_task
        assert slow.calls == 1                            # exactly one enforce in flight
    asyncio.run(go())


# -- 2. the floor is still enforced (the off-loop path actually prunes) --

def test_run_guards_offloop_still_prunes_oldest(tmp_path):
    root = str(tmp_path)
    old = _make_partition(root, "depth", "BTCUSDT", "2026-06-01", 1000)
    soft = 50_000
    guard = dg.DiskGuard(root, datasets=("depth",), soft_floor=soft, critical_floor=1,
                         free_fn=lambda _: soft - 500,           # below the soft floor
                         active_date_fn=lambda: "2099-01-01")    # old day is not protected
    s = svc.CaptureService(root=root, client=None, disk_guard=guard,
                           inode_guard_enabled=False, disk_check_interval_s=0.0)
    asyncio.run(s._run_guards_offloop())
    assert not old.exists()                              # off-loop enforce pruned the oldest


# -- 3. no prune-vs-write race: the active (today's) partition is never pruned --

def test_select_oldest_to_reclaim_skips_protected_dates():
    parts = [dg.Partition("/d/symbol=X/date=2026-06-01", "2026-06-01", 100),
             dg.Partition("/d/symbol=X/date=2026-06-17", "2026-06-17", 100)]
    # deficit needs BOTH, but the active date is protected -> it is never selected.
    chosen = dg.select_oldest_to_reclaim(parts, 150, protect_dates=("2026-06-17",))
    assert [p.date for p in chosen] == ["2026-06-01"]


def test_enforce_never_prunes_the_active_partition(tmp_path):
    root = str(tmp_path)
    old = _make_partition(root, "depth", "BTCUSDT", "2026-06-01", 1000)
    today = _make_partition(root, "depth", "BTCUSDT", "2026-06-17", 1000)
    soft = 50_000
    guard = dg.DiskGuard(root, datasets=("depth",), soft_floor=soft, critical_floor=1,
                         free_fn=lambda _: soft - 1500,          # deficit 1500 (> old's 1000)
                         active_date_fn=lambda: "2026-06-17")    # the writer's live partition
    res = guard.enforce()
    assert today.exists()                                # active partition NEVER pruned
    assert not old.exists()                              # oldest still reclaimed
    assert all("2026-06-17" not in p for p in res.pruned)


# -- 4. we did NOT mask stalls: the watchdog feed still reflects genuine liveness --

def test_watchdog_feed_still_gated_on_liveness(tmp_path):
    feeds = _RecordingNotifier()
    s = svc.CaptureService(root=str(tmp_path), client=None, notifier=feeds,
                           disk_guard_enabled=False, inode_guard_enabled=False,
                           watchdog_liveness_window_s=1.0)
    s._last_msg_monotonic = time.monotonic() - 100.0     # socket silent -> stale liveness
    s._feed_watchdog_if_live()
    assert feeds.watchdogs == 0                           # a genuine stall still misses the feed
    s._last_msg_monotonic = time.monotonic()
    s._feed_watchdog_if_live()
    assert feeds.watchdogs == 1                           # fed only when genuinely live
