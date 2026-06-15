"""ADR-039 stage 2a — REQUEST_WEIGHT throttle (the global snapshot weight budget).

The snapshot-owner is the sole caller of /fapi/v1/depth across all shards; its
throttle must keep issued weight under a budget that reserves headroom below the
REQUEST_WEIGHT cap (2400/min) so capture can never weight-starve the live engine on
the shared IP. The cap is HARD (no trailing 60s window may exceed it) and structural
for ANY number of clients — all requests serialize through this one throttle.
"""
from __future__ import annotations

import asyncio

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import rest_throttle as rt


class FakeClock:
    """Deterministic clock: sleeping advances virtual time, nothing wall-clock."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += max(s, 0.0)


def _throttle(budget: int, fc: FakeClock) -> "rt.WeightThrottle":
    return rt.WeightThrottle(budget, window_s=60.0, clock=fc, sleep_fn=fc.sleep)


# -- budget = cap - reserved headroom -----------------------------------------

def test_budget_is_cap_minus_reserved_headroom():
    cap = cfg.FAPI_WEIGHT_LIMIT                       # 2400 REQUEST_WEIGHT/min
    budget = rt.snapshot_weight_budget(cap)
    assert budget == cap - cfg.CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN
    assert cap - budget >= cfg.CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN   # headroom kept
    assert budget > 0


# -- (b) hard cap under burst + N-independence --------------------------------

def test_throttle_hard_caps_weight_per_window_under_burst():
    fc = FakeClock()
    budget = 1400
    th = _throttle(budget, fc)

    async def scenario():
        for _ in range(200):                          # 200 depth snapshots @20 weight
            await th.acquire(20)

    asyncio.run(scenario())
    assert th.granted_weight == 200 * 20              # all eventually served
    assert th.peak_in_window <= budget                # HARD per-window cap never exceeded
    # it was PACED past the first window, not all granted at once
    assert fc.t >= (200 * 20 - budget) / budget * 60.0


def test_throttle_cap_is_independent_of_client_count():
    # The cap lives on the ONE throttle, so M concurrent callers (shards) can never
    # COLLECTIVELY exceed it — the budget is N-independent.
    for n_clients in (1, 3, 8):
        fc = FakeClock()
        budget = 1400
        th = _throttle(budget, fc)

        async def worker():
            for _ in range(25):
                await th.acquire(20)

        async def scenario():
            await asyncio.gather(*(worker() for _ in range(n_clients)))

        asyncio.run(scenario())
        assert th.granted_weight == n_clients * 25 * 20
        assert th.peak_in_window <= budget            # holds regardless of caller count


def test_oversized_single_request_is_rejected():
    fc = FakeClock()
    th = _throttle(20, fc)

    async def scenario():
        await th.acquire(21)                          # bigger than the whole budget

    try:
        asyncio.run(scenario())
        assert False, "expected ValueError for an un-grantable request"
    except ValueError:
        pass


# -- (c) budget proof: 527-symbol cold start ----------------------------------

def test_cold_start_527_symbols_drains_under_cap_with_headroom():
    cap = cfg.FAPI_WEIGHT_LIMIT
    budget = rt.snapshot_weight_budget(cap)           # 2400 - reserved
    fc = FakeClock()
    th = _throttle(budget, fc)

    async def scenario():
        for _ in range(527):
            await th.acquire(cfg.DEPTH_SNAPSHOT_WEIGHT)   # 20 each

    asyncio.run(scenario())
    total = 527 * cfg.DEPTH_SNAPSHOT_WEIGHT            # 10,540 weight
    assert th.granted_weight == total
    assert th.peak_in_window <= budget                # per-window cap held throughout
    # expected ramp: paced past the first window, finishing near total/budget minutes
    assert fc.t >= (total - budget) / budget * 60.0           # >= ~6.5 min
    assert fc.t <= total / budget * 60.0 + 60.0               # <= ~8.5 min
    # headroom for engine + collectors is preserved, and steady-state is far under budget
    assert cap - budget >= cfg.CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN
    assert 5 * cfg.DEPTH_SNAPSHOT_WEIGHT < budget     # a few resyncs << budget
