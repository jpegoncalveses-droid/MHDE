"""All-traffic REQUEST_WEIGHT header-gate for the snapshot-owner (ADR-039 stage 2b).

The :class:`~crypto.research.capture_core.rest_throttle.WeightThrottle` paces the
owner's OWN snapshot weight against a static budget. This gate adds an *all-traffic
backstop* on top of it: it reads the live per-IP ``X-MBX-USED-WEIGHT-1M`` header — which
reflects EVERY caller on the IP (owner + engine + light collectors) — and, when observed
used-weight exceeds ``cap - margin``, blocks NEW depth fetches until the current
per-minute weight window resets. On HTTP 429 it backs off for ``Retry-After`` (the hard
backstop against a ban that would hit the live engine on the shared IP). It subsumes the
static reserved-headroom assumption: the owner adapts to the real combined usage.

The two compose — at any moment the TIGHTER constraint binds. This gate uses a
WALL-CLOCK clock for minute-window alignment, deliberately SEPARATE from the throttle's
monotonic core (no wall-clock dependency leaks into the throttle's accounting).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from crypto.research.capture_core import config as cfg


class HeaderGate:
    """Live-used-weight backoff gate. ``observe`` after each response; ``acquire`` before
    each fetch (blocks while over-threshold or in a 429 backoff); ``handle_429`` on 429."""

    def __init__(
        self,
        *,
        cap: int,
        margin: int = cfg.CAPTURE_HEADER_GATE_MARGIN,
        wall_clock: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._cap = cap
        self._margin = margin
        self._wall = wall_clock                    # epoch seconds; minute = int(t // 60)
        self._sleep = sleep_fn
        self._blocked_through_minute: Optional[int] = None
        self._backoff_until = 0.0
        self.backoffs = 0

    @property
    def threshold(self) -> int:
        return self._cap - self._margin

    def observe(self, used_weight: Optional[int]) -> None:
        """Record the live used-weight from a response. ``None`` (header missing or
        unparseable) is a graceful no-op — the gate falls back to throttle-only."""
        if used_weight is None:
            return
        if used_weight > self.threshold:
            # The 1-minute REQUEST_WEIGHT counter resets at the next minute boundary, so
            # block new fetches through the REMAINDER of the current minute window.
            self._blocked_through_minute = int(self._wall() // 60)

    def handle_429(self, retry_after: float) -> None:
        """Enter a hard backoff until ``Retry-After`` elapses (ban-escalation backstop)."""
        self._backoff_until = max(self._backoff_until, self._wall() + max(retry_after, 0.0))
        self.backoffs += 1

    async def acquire(self) -> None:
        """Block until neither a 429 backoff nor the used-weight window is active.

        One sleep per active constraint (no busy-spin): the 429 backoff sleeps the exact
        remaining time; the used-weight block sleeps to the next minute boundary, then
        re-checks (the counter has reset).
        """
        while True:
            now = self._wall()
            if now < self._backoff_until:                  # 429 hard backoff
                await self._sleep(self._backoff_until - now)
                continue
            if self._blocked_through_minute is not None:    # used-weight window block
                if int(now // 60) <= self._blocked_through_minute:
                    next_boundary = (self._blocked_through_minute + 1) * 60
                    await self._sleep(max(next_boundary - now, 0.0))
                    continue
                self._blocked_through_minute = None         # window reset -> clear
            return
