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

from crypto.research.capture_core import rest_throttle as rt
from crypto.research.capture_core import snapshot_owner as so


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
