"""Tests for the capture-core WS connection manager.

Pure helpers (sharding / backoff / URL) are tested directly; the async shard
loop is driven with injected fakes via ``asyncio.run`` (no pytest-asyncio dep).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from crypto.research.capture_core import conn_manager as cm


# -- pure helpers --

def test_shard_streams_chunks_evenly_with_remainder():
    assert cm.shard_streams(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]


def test_combined_url_joins_streams():
    assert cm.combined_url("wss://x/stream?streams=", ["a@aggTrade", "b@aggTrade"]) \
        == "wss://x/stream?streams=a@aggTrade/b@aggTrade"


def test_compute_backoff_doubles_and_caps_with_centered_jitter():
    rng = lambda: 0.5  # noqa: E731 -> jitter factor exactly 1.0
    assert cm.compute_backoff(1, base=1.0, cap=60.0, jitter=0.1, rand=rng) == pytest.approx(1.0)
    assert cm.compute_backoff(2, base=1.0, cap=60.0, jitter=0.1, rand=rng) == pytest.approx(2.0)
    # attempt 10 would be 512s, capped to 60
    assert cm.compute_backoff(10, base=1.0, cap=60.0, jitter=0.1, rand=rng) == pytest.approx(60.0)


def test_compute_backoff_jitter_bounds():
    lo = cm.compute_backoff(3, base=1.0, cap=60.0, jitter=0.1, rand=lambda: 0.0)
    hi = cm.compute_backoff(3, base=1.0, cap=60.0, jitter=0.1, rand=lambda: 1.0)
    assert lo == pytest.approx(4.0 * 0.9)
    assert hi == pytest.approx(4.0 * 1.1)


# -- async shard loop fakes --

class _FakeConn:
    def __init__(self, messages, *, exc=None):
        self._messages = list(messages)
        self._exc = exc or ConnectionError("closed")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        if self._messages:
            return self._messages.pop(0)
        raise self._exc


def _frame(symbol="BTCUSDT", price="100.0"):
    return json.dumps({"stream": f"{symbol.lower()}@aggTrade",
                       "data": {"e": "aggTrade", "s": symbol, "p": price}})


def _make_connect_fn(conns):
    it = iter(conns)

    def _factory(url):
        return next(it)

    return _factory


def test_dispatches_frames_then_reconnects_and_records_gap():
    received = []
    gaps = []
    slept = []

    def on_message(stream, data, recv_ns):
        received.append((stream, data["p"], recv_ns))
        if len(received) == 3:
            mgr.stop()

    def on_gap(streams, reason, start_ms, end_ms):
        gaps.append((tuple(streams), reason, start_ms, end_ms))

    async def fake_sleep(s):
        slept.append(s)

    conn1 = _FakeConn([_frame(price="1"), _frame(price="2")])  # then raises -> reconnect
    conn2 = _FakeConn([_frame(price="3")])

    mgr = cm.ConnectionManager(
        streams=["btcusdt@aggTrade"],
        on_message=on_message,
        on_gap=on_gap,
        streams_per_conn=10,
        connect_fn=_make_connect_fn([conn1, conn2]),
        backoff_base=1.0, backoff_max=60.0, jitter=0.1,
        proactive_reconnect_s=10**9,
        sleep_fn=fake_sleep,
        time_fn=lambda: 0.0,
        rand_fn=lambda: 0.5,
        recv_clock=lambda: 777,
        wall_ms_fn=lambda: 1234,
    )
    asyncio.run(mgr.run())

    assert [p for _, p, _ in received] == ["1", "2", "3"]
    assert mgr.dispatched == 3
    assert received[0][2] == 777                      # recv_ts_ns stamped
    assert len(gaps) == 1
    streams, reason, start_ms, end_ms = gaps[0]
    assert reason == "reconnect"
    assert streams == ("btcusdt@aggTrade",)
    assert slept == [1.0]                             # one backoff at base


def test_proactive_reconnect_breaks_without_backoff():
    received = []
    gaps = []
    slept = []
    times = iter([0.0, 100.0, 100.0, 100.0, 100.0, 100.0])

    def on_message(stream, data, recv_ns):
        received.append(data["p"])
        mgr.stop()

    async def fake_sleep(s):
        slept.append(s)

    conn1 = _FakeConn([])              # proactive fires before any recv
    conn2 = _FakeConn([_frame(price="9")])

    mgr = cm.ConnectionManager(
        streams=["btcusdt@aggTrade"],
        on_message=on_message,
        on_gap=lambda *a: gaps.append(a),
        connect_fn=_make_connect_fn([conn1, conn2]),
        proactive_reconnect_s=10.0,
        sleep_fn=fake_sleep,
        time_fn=lambda: next(times),
        rand_fn=lambda: 0.5,
    )
    asyncio.run(mgr.run())

    assert received == ["9"]
    assert slept == []                                # proactive cycle has no backoff
    assert gaps and gaps[0][1] == "proactive_reconnect"


def test_tracks_raw_bytes_in_for_load_sizing():
    def on_message(stream, data, recv_ns):
        mgr.stop()

    f = _frame(price="100.0")
    conn = _FakeConn([f])
    mgr = cm.ConnectionManager(
        streams=["btcusdt@aggTrade"],
        on_message=on_message,
        connect_fn=_make_connect_fn([conn]),
        proactive_reconnect_s=10**9,
        sleep_fn=lambda s: asyncio.sleep(0),
        time_fn=lambda: 0.0,
    )
    asyncio.run(mgr.run())
    assert mgr.bytes_in == len(f)


def test_malformed_frames_are_dropped_not_dispatched():
    received = []

    def on_message(stream, data, recv_ns):
        received.append(data)
        mgr.stop()

    conn = _FakeConn(["not json", json.dumps({"no_stream": 1}), _frame(price="5")])
    mgr = cm.ConnectionManager(
        streams=["btcusdt@aggTrade"],
        on_message=on_message,
        connect_fn=_make_connect_fn([conn]),
        proactive_reconnect_s=10**9,
        sleep_fn=lambda s: asyncio.sleep(0),
        time_fn=lambda: 0.0,
    )
    asyncio.run(mgr.run())

    assert len(received) == 1                          # only the valid frame
    assert mgr.dropped == 2
