"""ADR-039 stage 2b piece 2 — owner header-gate on live X-MBX-USED-WEIGHT-1M.

The owner backs off on the live per-IP used-weight header (which reflects ALL traffic
on the IP — owner + engine + light collectors) as an all-traffic backstop ON TOP OF the
sliding-window WeightThrottle. This subsumes the static reserved-headroom assumption:
the owner adapts to actual combined usage instead of assuming the split holds. The gate
uses a WALL-CLOCK clock for minute-window alignment, deliberately SEPARATE from the
throttle's monotonic core (no wall-clock dependency leaks there).
"""
from __future__ import annotations

import asyncio
import inspect
import time as _time

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import rest_header_gate as grt
from crypto.research.capture_core import rest_throttle as rt
from crypto.research.capture_core import snapshot_owner as so
from crypto.research.capture_core.client import RateLimited


class FakeWall:
    """Deterministic wall clock (epoch seconds): sleeping advances virtual time."""

    def __init__(self, t0=0.0):
        self.t = t0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += max(s, 0.0)


async def _direct(fn, *args):                 # run the fetch inline (deterministic)
    return fn(*args)


async def _anoop(_s):                          # a sleep that never advances anything
    return None


def _free_throttle():                          # huge budget -> never blocks/sleeps
    return rt.WeightThrottle(10 ** 9, clock=lambda: 0.0, sleep_fn=_anoop)


def _gate(fw, *, margin=400):
    return grt.HeaderGate(cap=2400, margin=margin, wall_clock=fw, sleep_fn=fw.sleep)


# -- (a) below threshold: no block --------------------------------------------

def test_gate_below_threshold_does_not_block():
    fw = FakeWall(t0=100.0)
    g = _gate(fw)
    g.observe(1500)                            # < cap-margin (2000)
    asyncio.run(g.acquire())
    assert fw.t == 100.0                        # immediate, no backoff


# -- (b) crosses threshold: back off until the per-minute window resets --------

def test_gate_over_threshold_blocks_until_next_minute_then_resumes():
    fw = FakeWall(t0=90.0)                      # minute index 1, 30s in
    g = _gate(fw)
    g.observe(2100)                            # > 2000 -> block through minute 1
    asyncio.run(g.acquire())
    assert fw.t == 120.0                        # slept to the minute-2 boundary (window reset)
    # window cleared: a fresh low observation no longer blocks
    g.observe(100)
    asyncio.run(g.acquire())
    assert fw.t == 120.0


# -- (c) 429 + Retry-After: wait it once, no tight loop -----------------------

def test_gate_429_waits_retry_after_no_tight_loop():
    fw = FakeWall(t0=100.0)
    g = _gate(fw)
    g.handle_429(retry_after=5.0)
    asyncio.run(g.acquire())
    assert fw.t == 105.0                        # waited exactly Retry-After, then resumed
    assert g.backoffs == 1
    asyncio.run(g.acquire())                    # already past the backoff -> immediate
    assert fw.t == 105.0


# -- (d) header missing/malformed: throttle-only fallback, no crash ------------

def test_gate_missing_header_is_throttle_only_fallback():
    fw = FakeWall(t0=100.0)
    g = _gate(fw)
    g.observe(None)                            # missing/unparseable -> no gate action
    asyncio.run(g.acquire())
    assert fw.t == 100.0


# -- (e) composition: gate binds over a permissive throttle -------------------

def test_owner_header_gate_binds_over_permissive_throttle(tmp_path):
    fw = FakeWall(t0=30.0)                      # minute 0
    used = [2100, 100]                          # first response high, second low
    calls = []

    def fetch(symbol, limit):
        i = len(calls)
        calls.append((symbol, fw.t))
        return {"lastUpdateId": i + 1}, used[i]

    owner = so.SnapshotOwner(fetch_fn=fetch, throttle=_free_throttle(), gate=_gate(fw),
                             socket_path=str(tmp_path / "o.sock"), to_thread=_direct)

    async def scenario():
        a = await owner._snapshot("A")         # used 2100 -> gate blocks the next fetch
        b = await owner._snapshot("B")         # waits to the minute reset, throttle is free
        return a, b

    a, b = asyncio.run(scenario())
    assert a["snapshot"]["lastUpdateId"] == 1 and b["snapshot"]["lastUpdateId"] == 2
    assert calls[0][1] == 30.0                  # first fetch immediate
    assert calls[1][1] == 60.0                  # second waited for the window reset (GATE bound)


def test_owner_429_backs_off_retry_after_then_resumes(tmp_path):
    fw = FakeWall(t0=10.0)
    calls = []

    def fetch(symbol, limit):
        calls.append((symbol, fw.t))
        if symbol == "A":
            raise RateLimited(429, 5.0)        # Retry-After 5s
        return {"lastUpdateId": 1}, 100

    owner = so.SnapshotOwner(fetch_fn=fetch, throttle=_free_throttle(), gate=_gate(fw),
                             socket_path=str(tmp_path / "o.sock"), to_thread=_direct)

    async def scenario():
        a = await owner._snapshot("A")         # 429 -> fetch_failed, gate backs off Retry-After
        b = await owner._snapshot("B")         # waits Retry-After, then fetches
        return a, b

    a, b = asyncio.run(scenario())
    assert a.get("error") == "fetch_failed"     # A failed on the 429
    assert b["snapshot"]["lastUpdateId"] == 1   # B succeeded after the backoff
    assert owner._gate.backoffs == 1
    assert calls[0][1] == 10.0                  # A attempted at t=10
    assert calls[1][1] == 15.0                  # B waited Retry-After (gate backoff)


# -- (f) the WeightThrottle monotonic core is untouched -----------------------

def test_weight_throttle_core_stays_monotonic_no_wallclock():
    assert inspect.signature(rt.WeightThrottle.__init__).parameters["clock"].default \
        is _time.monotonic                      # default clock is monotonic, NOT wall-clock
    src = inspect.getsource(rt)                  # the throttle module has no wall-clock / gate coupling
    assert "time.time(" not in src
    assert "header_gate" not in src


def test_gate_margin_config_is_conservative_and_under_cap():
    assert 0 < cfg.CAPTURE_HEADER_GATE_MARGIN < cfg.FAPI_WEIGHT_LIMIT
