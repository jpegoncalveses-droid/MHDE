"""Sliding-window REQUEST_WEIGHT throttle for the snapshot-owner (ADR-039 stage 2a).

The owner is the sole caller of ``/fapi/v1/depth`` across all shards, so this one
throttle is the GLOBAL, N-independent cap on capture's REST weight. It is a
sliding-window LOG: the total weight granted in any trailing ``window_s`` never
exceeds ``budget`` — a HARD per-window cap, strictly tighter than Binance's
fixed-minute REQUEST_WEIGHT window, so the owner can never draw a 429 on its own and
can never weight-starve the engine + light collectors sharing the IP.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable

from crypto.research.capture_core import config as cfg


def snapshot_weight_budget(
    cap: int,
    reserved: int = cfg.CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN,
) -> int:
    """The owner's per-minute weight budget = ``cap`` minus reserved headroom.

    Reserves headroom under the REQUEST_WEIGHT cap so capture never weight-starves
    the engine + collectors on the shared IP. Floored at 1 so it is always grantable.
    """
    return max(1, cap - reserved)


class WeightThrottle:
    """Async sliding-window weight limiter; ``acquire`` blocks until the grant fits."""

    def __init__(
        self,
        budget: int,
        *,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._budget = budget
        self._window = window_s
        self._clock = clock
        self._sleep = sleep_fn
        self._events: "deque[tuple[float, int]]" = deque()
        self._in_window = 0
        self.granted_weight = 0
        self.grants = 0
        self.peak_in_window = 0

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0][0] <= cutoff:
            _, w = self._events.popleft()
            self._in_window -= w

    async def acquire(self, weight: int) -> None:
        """Block until ``weight`` fits in the trailing window without exceeding budget.

        A single request larger than the whole budget can never be granted and raises
        ``ValueError`` rather than blocking forever.
        """
        if weight > self._budget:
            raise ValueError(
                f"request weight {weight} exceeds the whole budget {self._budget}; "
                "ungrantable")
        while True:
            now = self._clock()
            self._evict(now)
            if self._in_window + weight <= self._budget:
                # No await between this check and the record below, so the grant is
                # atomic on the event loop — the cap holds no matter how many
                # coroutines (shards) call acquire concurrently (N-independence).
                self._events.append((now, weight))
                self._in_window += weight
                self.granted_weight += weight
                self.grants += 1
                if self._in_window > self.peak_in_window:
                    self.peak_in_window = self._in_window
                return
            # Over budget: sleep until just enough of the oldest events roll off the
            # window to fit `weight`, then retry. One sleep per acquire (no spin).
            need = (self._in_window + weight) - self._budget
            freed = 0
            target_expiry = now
            for ts, w in self._events:
                freed += w
                target_expiry = ts + self._window
                if freed >= need:
                    break
            await self._sleep(max(target_expiry - now, 0.0))
