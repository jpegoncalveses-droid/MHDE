"""Tests for the paced, deduped REST depth-snapshot scheduler."""
from __future__ import annotations

import asyncio

from crypto.research.capture_core import snapshot as snap


class _FakeClient:
    def __init__(self):
        self.calls = []

    def fetch_depth_snapshot(self, symbol, limit):
        self.calls.append((symbol, limit))
        return {"lastUpdateId": 100 + len(self.calls), "bids": [], "asks": []}


def _run_until(coro_factory, predicate, stopper, *, max_ticks=5000):
    async def scenario():
        task = asyncio.create_task(coro_factory())
        for _ in range(max_ticks):
            if predicate():
                break
            await asyncio.sleep(0)
        stopper()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())


def test_scheduler_fetches_each_requested_symbol_once_paced_and_deduped():
    client = _FakeClient()
    got = []
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    s = snap.SnapshotScheduler(
        client=client, on_snapshot=lambda sym, snp, recv: got.append((sym, recv)),
        min_interval_s=5.0, limit=1000, sleep_fn=fake_sleep, clock_ns=lambda: 42,
    )
    assert s.request("BTCUSDT") is True
    assert s.request("BTCUSDT") is False     # dedup while pending
    assert s.request("ETHUSDT") is True

    _run_until(s.run, lambda: s.fetched >= 2, s.stop)

    assert client.calls == [("BTCUSDT", 1000), ("ETHUSDT", 1000)]
    assert got == [("BTCUSDT", 42), ("ETHUSDT", 42)]
    assert slept[:2] == [5.0, 5.0]           # paced ~min_interval between fetches


def test_scheduler_counts_errors_and_keeps_going():
    class _BadClient:
        def __init__(self):
            self.n = 0

        def fetch_depth_snapshot(self, symbol, limit):
            self.n += 1
            if symbol == "BADUSDT":
                raise RuntimeError("boom")
            return {"lastUpdateId": 1, "bids": [], "asks": []}

    got = []
    s = snap.SnapshotScheduler(
        client=_BadClient(), on_snapshot=lambda sym, snp, recv: got.append(sym),
        min_interval_s=0.0, limit=5, sleep_fn=lambda x: asyncio.sleep(0),
        clock_ns=lambda: 0,
    )
    s.request("BADUSDT")
    s.request("OKUSDT")
    _run_until(s.run, lambda: (s.fetched + s.errors) >= 2, s.stop)

    assert s.errors == 1
    assert got == ["OKUSDT"]                  # the good one still processed
