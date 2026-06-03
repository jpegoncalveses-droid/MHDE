"""Paced, deduped REST depth-snapshot scheduler for capture-core.

Seeding/resyncing the 529-symbol order-book diff streams requires a REST
``/fapi/v1/depth`` snapshot per symbol. At limit=1000 each costs ~20 request
weight; 529 full re-seeds = ~10,580 weight, which would blow the ~2400/min
futures IP budget that is SHARED with the engine and the per-minute collector —
and a capture-triggered 429/ban would starve them too. So snapshots are:

  * **paced** — at least ``min_interval_s`` between requests (derived from a
    weight ceiling well under the IP budget), which also staggers the initial
    529-symbol seed over several minutes; and
  * **deduped** — a symbol already queued/in-flight is not re-requested.

The actual HTTP call runs in a thread (``asyncio.to_thread``) so it never blocks
the socket event loop. Read-only public endpoint; the 429/418 backoff lives in
the REST client.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger("mhde.crypto.capture_core.snapshot")

OnSnapshot = Callable[[str, dict, int], None]


class SnapshotScheduler:
    def __init__(
        self,
        *,
        client: Any,
        on_snapshot: OnSnapshot,
        min_interval_s: float,
        limit: int,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._client = client
        self._on_snapshot = on_snapshot
        self._min_interval_s = min_interval_s
        self._limit = limit
        self._sleep = sleep_fn
        self._clock_ns = clock_ns
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending: set[str] = set()
        self._stop = asyncio.Event()
        self.fetched = 0
        self.errors = 0

    def request(self, symbol: str) -> bool:
        """Queue a snapshot for ``symbol``. Returns False if already pending."""
        if symbol in self._pending:
            return False
        self._pending.add(symbol)
        self._queue.put_nowait(symbol)
        return True

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            symbol = await self._next()
            if symbol is None:
                return
            try:
                snap = await asyncio.to_thread(
                    self._client.fetch_depth_snapshot, symbol, self._limit)
                self._on_snapshot(symbol, snap, self._clock_ns())
                self.fetched += 1
            except Exception as exc:  # noqa: BLE001 - isolate one symbol's seed
                self.errors += 1
                logger.warning("capture-core snapshot fetch failed for %s (%s: %s)",
                               symbol, type(exc).__name__, exc)
            finally:
                self._pending.discard(symbol)
            await self._sleep(self._min_interval_s)

    async def _next(self):
        """Next queued symbol, or None when stop is requested."""
        get_task = asyncio.ensure_future(self._queue.get())
        stop_task = asyncio.ensure_future(self._stop.wait())
        done, pending = await asyncio.wait(
            {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        if get_task in done:
            return get_task.result()
        return None
