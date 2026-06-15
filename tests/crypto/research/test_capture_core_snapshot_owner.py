"""ADR-039 stage 2a — snapshot-owner + unix request/response socket + client helper.

The owner is the single broker over /fapi/v1/depth: clients (shards, later) ask for a
symbol over a unix socket and get a snapshot or a typed error. Owner-down must be a
clean typed error (never a hang); the raw diff tape is independent of the owner; and a
mid-request connection drop replays transparently on reconnect.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import rest_throttle as rt
from crypto.research.capture_core import snapshot_owner as so


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += max(s, 0.0)


def _fast_throttle() -> "rt.WeightThrottle":
    fc = FakeClock()
    return rt.WeightThrottle(10 ** 9, clock=fc, sleep_fn=fc.sleep)   # never blocks


SNAP = {"lastUpdateId": 123, "E": 1000, "bids": [["1", "2"]], "asks": [["3", "4"]]}


# -- (a) socket round-trip + typed errors -------------------------------------

def test_owner_round_trip_and_typed_errors(tmp_path):
    sock = str(tmp_path / "owner.sock")
    fetched = []

    def fake_fetch(symbol, limit):
        fetched.append((symbol, limit))
        return dict(SNAP)

    owner = so.SnapshotOwner(fetch_fn=fake_fetch, throttle=_fast_throttle(),
                             socket_path=sock, limit=1000)

    async def scenario():
        await owner.start()
        client = so.SnapshotClient(sock)
        ok = await client.request("BTCUSDT")
        bad = await client.request_raw("{ not json")
        nosym = await client.request_raw(json.dumps({"nope": 1}))
        await owner.stop()
        return ok, bad, nosym

    ok, bad, nosym = asyncio.run(scenario())
    assert ok["symbol"] == "BTCUSDT" and ok["snapshot"]["lastUpdateId"] == 123
    assert fetched == [("BTCUSDT", 1000)]
    assert bad["error"] == "bad_request"
    assert nosym["error"] == "bad_request"


def test_owner_dedups_concurrent_requests_for_one_symbol(tmp_path):
    # Two in-flight requests for the SAME symbol must collapse to ONE REST fetch
    # (the post-re-resolve 2-writer window must not double-spend the budget).
    sock = str(tmp_path / "owner.sock")
    fetch_started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def gated_to_thread(fn, *args):
        calls.append(args[0])
        fetch_started.set()
        await release.wait()
        return fn(*args)

    owner = so.SnapshotOwner(fetch_fn=lambda s, l: dict(SNAP), throttle=_fast_throttle(),
                             socket_path=sock, limit=1000, to_thread=gated_to_thread)

    async def scenario():
        f1 = asyncio.ensure_future(owner._snapshot("BTCUSDT"))
        await fetch_started.wait()                 # first fetch in-flight, future created
        f2 = asyncio.ensure_future(owner._snapshot("BTCUSDT"))
        await asyncio.sleep(0)                      # let f2 piggyback the pending future
        release.set()
        return await asyncio.gather(f1, f2)

    a, b = asyncio.run(scenario())
    assert a["snapshot"]["lastUpdateId"] == 123 and b["snapshot"]["lastUpdateId"] == 123
    assert calls == ["BTCUSDT"]                     # exactly ONE fetch for both requests


# -- (d) failure / replay -----------------------------------------------------

def test_client_against_down_owner_is_clean_error_not_hang(tmp_path):
    client = so.SnapshotClient(str(tmp_path / "absent.sock"))   # no server listening

    async def scenario():
        try:
            await asyncio.wait_for(client.request("BTCUSDT"), timeout=5)
            return "no-error"
        except so.SnapshotOwnerUnavailable:
            return "unavailable"

    assert asyncio.run(scenario()) == "unavailable"            # typed + fast (no hang)


def test_raw_diff_tape_is_independent_of_snapshot_owner(tmp_path):
    # With snapshots OFF (no owner / no scheduler), a depth diff still lands on the
    # raw tape — owner-down can never cause a permanent gap.
    from crypto.research.capture_core import service as svc
    s = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                           install_signals=False, disk_guard_enabled=False,
                           inode_guard_enabled=False)
    assert s._snap_sched is None
    diff = {"e": "depthUpdate", "E": 1000, "T": 1000, "s": "BTCUSDT",
            "U": 1, "u": 2, "pu": 0, "b": [["100", "1"]], "a": [["101", "2"]]}
    s._handle_depth(diff, recv_ns=42)
    s._depth.flush_all()
    files = sorted(pathlib.Path(tmp_path, "depth").rglob("*.parquet"))
    rows = [r for fp in files for r in pq.read_table(str(fp)).to_pylist()]
    assert len(rows) == 1 and rows[0]["s"] == "BTCUSDT" and rows[0]["recv_ts_ns"] == 42


def test_in_flight_request_replays_on_reconnect(tmp_path):
    # An owner that drops the FIRST connection without responding, then serves the
    # SECOND -> the client transparently reconnects + re-sends and gets the payload.
    sock = str(tmp_path / "owner.sock")
    conns = {"n": 0}

    async def handler(reader, writer):
        conns["n"] += 1
        if conns["n"] == 1:
            writer.close()                            # drop the first connection
            return
        line = await reader.readline()
        req = json.loads(line)
        writer.write((json.dumps({"symbol": req["symbol"], "snapshot": SNAP}) + "\n").encode())
        await writer.drain()
        writer.close()

    async def scenario():
        server = await asyncio.start_unix_server(handler, path=sock)
        client = so.SnapshotClient(sock)
        res = await asyncio.wait_for(client.request("BTCUSDT"), timeout=5)
        server.close()
        await server.wait_closed()
        return res

    res = asyncio.run(scenario())
    assert res["snapshot"]["lastUpdateId"] == 123
    assert conns["n"] == 2                            # it reconnected (replayed) once


# -- factory wiring -----------------------------------------------------------

def test_build_owner_reserves_headroom_from_exchangeinfo_cap():
    class FakeClient:
        def fetch_request_weight_limit(self, *, fallback):
            return 2400
        def fetch_depth_snapshot(self, symbol, limit):
            return dict(SNAP)

    owner = so.build_owner(FakeClient(), socket_path="/tmp/unused.sock")
    from crypto.research.capture_core import config as cfg
    assert owner._throttle._budget == 2400 - cfg.CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN
