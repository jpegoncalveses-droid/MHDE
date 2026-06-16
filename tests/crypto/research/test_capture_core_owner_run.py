"""ADR-039 §G-trial gap 1 — owner run entrypoint (``crypto capture-owner-run``).

A standalone runnable snapshot owner: the manual Trial-1 process that a sharded
``capture-core-run --snapshot-socket`` points at. Single-box, REST mocked. Covers:
the runner binds + serves the owner socket and a client completes a round-trip (a, b);
SIGTERM/SIGINT stop the serve loop and release the socket with no orphan / stale socket
(c); and the CLI command parses ``--socket`` and wires it into the owner (a). No
systemd / sd_notify / cpuset / shard-launch / live-run here.
"""
from __future__ import annotations

import asyncio
import os
import signal

import pytest

from crypto.research.capture_core import rest_throttle as rt
from crypto.research.capture_core import snapshot_owner as so


class _RecordingNotifier:
    def __init__(self):
        self.calls = []

    def ready(self):
        self.calls.append("READY")

    def watchdog(self):
        self.calls.append("WATCHDOG")


SNAP = {"lastUpdateId": 7, "E": 1000, "bids": [["1", "2"]], "asks": [["3", "4"]]}


async def _anoop(_s):                       # a sleep that never advances anything
    return None


def _free_throttle():                       # huge budget -> never blocks/sleeps
    return rt.WeightThrottle(10 ** 9, clock=lambda: 0.0, sleep_fn=_anoop)


def _owner(sock):
    def fake_fetch(symbol, limit):          # REST mocked: deterministic snapshot
        return dict(SNAP)
    return so.SnapshotOwner(fetch_fn=fake_fetch, throttle=_free_throttle(),
                            socket_path=sock, limit=1000)


# -- (a)+(b) runner binds the socket, a client round-trips, stop releases it ----

def test_owner_run_serves_round_trip_then_releases_socket(tmp_path):
    sock = str(tmp_path / "owner.sock")
    owner = _owner(sock)

    async def scenario():
        stop = asyncio.Event()
        ready = asyncio.Event()
        task = asyncio.ensure_future(
            so.run_owner(owner, stop_event=stop, install_signal_handlers=False,
                         ready_event=ready))
        await asyncio.wait_for(ready.wait(), timeout=2)
        listening = os.path.exists(sock)                            # (a) listening on S
        resp = await so.SnapshotClient(sock).request("BTCUSDT")     # (b) round-trip
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        return listening, resp, os.path.exists(sock)

    listening, resp, still_there = asyncio.run(scenario())
    assert listening is True
    assert resp["symbol"] == "BTCUSDT"
    assert resp["snapshot"]["lastUpdateId"] == 7
    assert still_there is False                                     # socket released on stop


# -- (c) SIGTERM / SIGINT stop the loop and release the socket (no orphan) ------

def _signal_scenario(sig, sock):
    async def scenario():
        owner = _owner(sock)
        ready = asyncio.Event()
        task = asyncio.ensure_future(
            so.run_owner(owner, install_signal_handlers=True, ready_event=ready))
        await asyncio.wait_for(ready.wait(), timeout=2)
        up = os.path.exists(sock)
        os.kill(os.getpid(), sig)                                  # real signal to self
        await asyncio.wait_for(task, timeout=2)                    # loop stops, no hang
        return up, task.done(), os.path.exists(sock)
    return scenario


def test_owner_run_sigterm_stops_and_releases_socket(tmp_path):
    sock = str(tmp_path / "owner.sock")
    up, done, still_there = asyncio.run(_signal_scenario(signal.SIGTERM, sock)())
    assert up is True
    assert done is True                                            # serve loop stopped
    assert still_there is False                                    # socket released, not stale


def test_owner_run_sigint_stops_and_releases_socket(tmp_path):
    sock = str(tmp_path / "owner.sock")
    up, done, still_there = asyncio.run(_signal_scenario(signal.SIGINT, sock)())
    assert up is True
    assert done is True
    assert still_there is False


# -- (a) the CLI command parses --socket and wires it into the owner -----------

# -- gap 3: sd_notify READY + the start()-in-try handler-leak tidy ------------

def test_run_owner_fires_ready_via_notifier(tmp_path):
    sock = str(tmp_path / "owner.sock")
    owner = _owner(sock)
    notifier = _RecordingNotifier()

    async def scenario():
        stop = asyncio.Event()
        ready = asyncio.Event()
        task = asyncio.ensure_future(
            so.run_owner(owner, stop_event=stop, install_signal_handlers=False,
                         ready_event=ready, notifier=notifier))
        await asyncio.wait_for(ready.wait(), timeout=2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert "READY" in notifier.calls                       # READY=1 fired after bind


def test_run_owner_removes_signal_handlers_when_start_raises():
    # A start() bind failure must STILL remove the installed signal handlers (no leak) and
    # re-raise the bind error — not mask it with a NameError from the finally touching
    # serve_task/stop_task before they were assigned.
    class _OwnerFailsStart:
        async def start(self):
            raise OSError("EADDRINUSE")

        async def serve(self):
            return None

        async def stop(self):
            return None

    state = {}

    async def scenario():
        loop = asyncio.get_running_loop()
        try:
            await so.run_owner(_OwnerFailsStart(), stop_event=asyncio.Event(),
                               install_signal_handlers=True)
        except OSError:
            state["raised"] = True
        state["leaked"] = [s for s in (signal.SIGTERM, signal.SIGINT)
                           if s in getattr(loop, "_signal_handlers", {})]

    asyncio.run(scenario())
    assert state.get("raised") is True                     # bind error propagated
    assert state["leaked"] == []                           # handlers cleaned up despite failure


def test_run_owner_watchdog_keepalive_fires_then_stops_clean(tmp_path, monkeypatch):
    # With WATCHDOG_USEC set, the owner emits a STEADY WATCHDOG keepalive (not activity-
    # gated — an idle owner is healthy), and the watchdog task is drained on stop (no hang).
    monkeypatch.setenv("WATCHDOG_USEC", "100000")          # 0.1s deadline -> 0.05s ping
    sock = str(tmp_path / "owner.sock")
    owner = _owner(sock)
    notifier = _RecordingNotifier()

    async def scenario():
        stop = asyncio.Event()
        ready = asyncio.Event()
        task = asyncio.ensure_future(
            so.run_owner(owner, stop_event=stop, install_signal_handlers=False,
                         ready_event=ready, notifier=notifier))
        await asyncio.wait_for(ready.wait(), timeout=2)
        for _ in range(200):                               # poll up to ~2s for a ping
            await asyncio.sleep(0.01)
            if "WATCHDOG" in notifier.calls:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=2)            # drains cleanly, no hang

    asyncio.run(scenario())
    assert "WATCHDOG" in notifier.calls


def test_capture_owner_run_cli_parses_socket_and_wires_owner(tmp_path, monkeypatch):
    import main
    from click.testing import CliRunner

    captured = {}

    def fake_build_owner(client, *, socket_path, **kw):            # no REST: stub it out
        captured["socket_path"] = socket_path
        captured["has_client"] = client is not None
        return object()

    async def fake_run_owner(owner, **kw):
        captured["ran"] = True

    monkeypatch.setattr(so, "build_owner", fake_build_owner)
    monkeypatch.setattr(so, "run_owner", fake_run_owner)

    sock = str(tmp_path / "x.sock")
    res = CliRunner().invoke(main.cli, ["crypto", "capture-owner-run", "--socket", sock])
    assert res.exit_code == 0, res.output
    assert captured["socket_path"] == sock
    assert captured["has_client"] is True
    assert captured.get("ran") is True
