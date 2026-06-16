"""ADR-039 gap 3 — sd_notify supervision wiring in the capture run loop.

READY=1 fires once the shard is up (manager launched); WATCHDOG=1 is fed from the flush
loop ONLY while messages are flowing, so a wedged loop (stops ticking) OR a silently-
stalled socket (no frames) both let systemd's WatchdogSec escalate to a restart.
"""
from __future__ import annotations

import asyncio
import time

from crypto.research.capture_core.service import CaptureService


class _RecordingNotifier:
    def __init__(self):
        self.calls = []

    def ready(self):
        self.calls.append("READY")

    def watchdog(self):
        self.calls.append("WATCHDOG")


class _StubClient:
    def fetch_usdtm_perp_universe(self):
        return ["BTCUSDT", "ETHUSDT"]


class _FakeMgr:
    def __init__(self):
        self._stopped = asyncio.Event()

    async def run(self):
        await self._stopped.wait()          # runs until the service calls stop()

    def stop(self):
        self._stopped.set()


class _HaltedGuard:
    halted = True

    def enforce(self):
        pass


def _svc(tmp_path, notifier, **kw):
    return CaptureService(
        root=str(tmp_path), client=_StubClient(), shard=0, n_shards=1,
        install_signals=False, enable_snapshots=False,
        disk_guard_enabled=False, inode_guard_enabled=False,
        mgr_factory=lambda streams: _FakeMgr(),
        notifier=notifier, **kw)


def test_on_message_stamps_liveness_before_halt_guard(tmp_path):
    # A disk/inode CRITICAL halt intentionally DROPS data (forward-only). Liveness must
    # still be stamped so systemd doesn't kill a process behaving correctly.
    svc = _svc(tmp_path, _RecordingNotifier(), disk_guard=_HaltedGuard())
    assert svc._writes_halted() is True
    before = svc._last_msg_monotonic
    svc._on_message("BTCUSDT@aggTrade", {}, recv_ns=123)
    assert svc._last_msg_monotonic > before


def test_watchdog_fed_while_messages_flow(tmp_path):
    notifier = _RecordingNotifier()
    svc = _svc(tmp_path, notifier, watchdog_liveness_window_s=60.0)
    svc._last_msg_monotonic = time.monotonic()          # a fresh frame just arrived
    svc._feed_watchdog_if_live()
    assert "WATCHDOG" in notifier.calls


def test_watchdog_skipped_when_silent_past_window(tmp_path):
    notifier = _RecordingNotifier()
    svc = _svc(tmp_path, notifier, watchdog_liveness_window_s=1.0)
    svc._last_msg_monotonic = time.monotonic() - 5.0    # stale: no frame for 5s
    svc._feed_watchdog_if_live()
    assert "WATCHDOG" not in notifier.calls             # systemd WatchdogSec escalates


def test_run_fires_ready_after_manager_launch(tmp_path):
    notifier = _RecordingNotifier()
    svc = _svc(tmp_path, notifier)

    async def scenario():
        task = asyncio.ensure_future(svc.run())
        for _ in range(2000):
            await asyncio.sleep(0)
            if "READY" in notifier.calls:
                break
        svc.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert "READY" in notifier.calls
