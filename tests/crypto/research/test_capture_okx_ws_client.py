"""OKX Stage B WS — the client-boundary venue seam (Constraint 1).

The fresh OKX WS client decodes the OKX envelope `{arg:{channel,instId?,instType?}, data:[...]}`,
routes on `arg.channel`, and resolves the instId from `arg.instId` — crucially for `bbo-tbt`,
whose DATA elements carry no instId. Arg-routing stays here so rows leaving the client are
venue-agnostic. Also builds the post-connect subscribe frame (Binance sends none) and drives
reconnect via an injected connect_fn.
"""
from __future__ import annotations

import asyncio
import json
import logging

from crypto.research.capture_core_okx import ws_client as wc


# ---- pure seam helpers ----------------------------------------------------

def test_subscribe_frame_built_per_channel():
    args = [
        {"channel": "trades", "instId": "BTC-USDT-SWAP"},
        {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
        {"channel": "liquidation-orders", "instType": "SWAP"},
    ]
    frame = wc.build_subscribe_frame(args)
    parsed = json.loads(frame)
    assert parsed == {"op": "subscribe", "args": args}


def test_decode_envelope_distinguishes_data_from_ack():
    data_frame = wc.decode_envelope(json.dumps(
        {"arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"}, "data": [{"tradeId": "1"}]}))
    assert data_frame.kind == "data"
    assert data_frame.channel == "trades" and data_frame.inst_id == "BTC-USDT-SWAP"

    ack = wc.decode_envelope(json.dumps(
        {"event": "subscribe", "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"}}))
    assert ack.kind == "ack"

    err = wc.decode_envelope(json.dumps({"event": "error", "code": "60012", "msg": "bad"}))
    assert err.kind == "error"


def test_bbo_arg_routing_injects_instid():
    # bbo-tbt DATA element has NO instId; the seam must supply it from arg.instId.
    raw = json.dumps({
        "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
        "data": [{"asks": [["42224.7", "5", "0", "2"]], "bids": [["42224.6", "1", "0", "1"]],
                  "ts": "1700000000123", "seqId": 42}],
    })
    seen = []
    client = wc.OkxWsClient(sub_args=[{"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"}],
                            on_frame=lambda ch, inst, data, recv_ns: seen.append((ch, inst, data)))
    client.handle_raw(raw, recv_ns=999)

    assert len(seen) == 1
    channel, inst_id, data = seen[0]
    assert channel == "bbo-tbt"
    assert inst_id == "BTC-USDT-SWAP"          # injected from arg, though data lacks it
    assert "instId" not in data[0]             # confirms the data element itself omits it


def test_malformed_frame_is_isolated_not_treated_as_socket_break():
    # A raising on_frame (e.g. one-sided bbo bids:[] -> d['bids'][0] IndexError) must be
    # isolated inside handle_raw, NOT propagate to the client's socket-break handler and
    # tear down the whole (single, universe-wide) connection.
    calls = {"n": 0}

    def boom(ch, inst, data, recv_ns):
        calls["n"] += 1
        raise IndexError("one-sided book")

    client = wc.OkxWsClient(sub_args=[], on_frame=boom)
    raw = json.dumps({"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
                      "data": [{"asks": [["1", "1", "0", "1"]], "bids": [], "ts": "1", "seqId": 1}]})
    client.handle_raw(raw, recv_ns=1)                      # must NOT raise
    assert calls["n"] == 1
    assert client.frame_errors == 1                        # counted, connection intact

    # a subsequent good frame still routes (the client was not torn down)
    good = []
    client2 = wc.OkxWsClient(sub_args=[], on_frame=lambda *a: good.append(a))
    client2.handle_raw(raw.replace('"bids": []', '"bids": [["1","1","0","1"]]'), recv_ns=2)
    assert len(good) == 1


def test_ack_and_error_frames_do_not_reach_on_frame():
    seen = []
    client = wc.OkxWsClient(sub_args=[], on_frame=lambda *a: seen.append(a))
    client.handle_raw(json.dumps({"event": "subscribe", "arg": {"channel": "trades"}}), recv_ns=1)
    client.handle_raw(json.dumps({"event": "error", "code": "x", "msg": "y"}), recv_ns=1)
    assert seen == []


# ---- async loop: subscribe-on-connect + route + reconnect -----------------

class _FakeConn:
    """Async-context-manager WS stub yielding a canned frame sequence, capturing sends."""
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        raise ConnectionError("socket closed")     # forces the reconnect path


def test_client_sends_subscribe_on_connect_and_routes_frames():
    sub_args = [{"channel": "trades", "instId": "BTC-USDT-SWAP"}]
    frame = json.dumps({"arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                        "data": [{"tradeId": "1", "px": "1", "sz": "1", "side": "buy",
                                  "ts": "1700000000123", "count": "1"}]})
    conns = [_FakeConn([frame]), _FakeConn([])]          # 2nd connect used after reconnect
    made = []

    def connect_fn(url):
        c = conns[len(made)]
        made.append(c)
        return c

    seen = []
    client = wc.OkxWsClient(
        sub_args=sub_args, on_frame=lambda ch, inst, d, r: seen.append((ch, inst)),
        connect_fn=connect_fn, sleep_fn=_noop_sleep, max_reconnects=1)

    asyncio.run(client.run())

    assert json.loads(conns[0].sent[0]) == {"op": "subscribe", "args": sub_args}  # subscribed
    assert ("trades", "BTC-USDT-SWAP") in seen                                    # frame routed
    assert len(made) == 2                                                         # reconnected once


async def _noop_sleep(_):
    return None


# ---- fix #1: chunk the subscribe below OKX's 64 KiB WS frame limit --------

def test_chunk_subscribe_args_keeps_each_frame_under_limit():
    # The full production universe (273 x 5 + 1 = 1366 args) is a 71 KiB frame; OKX closes the
    # socket with 1009 "Request message exceeds the maximum frame length". Chunk it.
    args = [{"channel": "trades", "instId": f"SYM{i}-USDT-SWAP"} for i in range(1000)]
    chunks = wc.chunk_subscribe_args(args, max_bytes=2000)
    assert len(chunks) > 1                                  # actually split
    for ch in chunks:
        assert ch                                          # no empty batch
        assert len(wc.build_subscribe_frame(ch)) <= 2000   # every frame under the limit
    flat = [a for ch in chunks for a in ch]
    assert flat == args                                    # all args, order preserved


def test_full_universe_subscribe_is_chunked_under_okx_limit():
    # the real default limit must keep a 1366-arg universe's frames each under 64 KiB
    from crypto.research.capture_core_okx.ws_collector import build_sub_args
    args = build_sub_args([f"SYM{i}-USDT-SWAP" for i in range(273)])
    for ch in wc.chunk_subscribe_args(args):
        assert len(wc.build_subscribe_frame(ch)) < 65536


def test_client_sends_multiple_subscribe_frames_when_over_limit():
    args = [{"channel": "trades", "instId": f"S{i}-USDT-SWAP"} for i in range(50)]
    conn = _FakeConn([])                                    # closes right after subscribe
    client = wc.OkxWsClient(sub_args=args, on_frame=lambda *a: None,
                            connect_fn=lambda url: conn, sleep_fn=_noop_sleep,
                            max_reconnects=0, subscribe_max_bytes=500)
    asyncio.run(client.run())

    subs = [json.loads(s) for s in conn.sent]
    assert len(subs) > 1                                    # chunked into several frames
    for s in subs:
        assert s["op"] == "subscribe"
        assert len(json.dumps(s)) <= 500
    sent_args = [a for s in subs for a in s["args"]]
    assert sent_args == args                                # all args subscribed exactly once


# ---- fix #2: surface OKX errors + reconnect reasons loudly ----------------

def test_error_frame_is_logged_loudly(caplog):
    client = wc.OkxWsClient(sub_args=[], on_frame=lambda *a: None)
    with caplog.at_level(logging.WARNING):
        client.handle_raw(json.dumps(
            {"event": "error", "code": "60012",
             "msg": "Invalid request: the total args exceed the maximum"}), recv_ns=1)
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "60012" in blob and "Invalid request" in blob    # not silently dropped


def test_malformed_frame_log_is_rate_limited(caplog):
    # KI-165 guard: a persistently-malformed channel at firehose rate must NOT log per-frame
    # (journal -> rsyslog -> uncapped /var/log/syslog is the exact disk-fill mechanism).
    clock = [1000.0]

    def raise_always(ch, inst, data, recv_ns):
        raise KeyError("markPx")

    bad = json.dumps({"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
                      "data": [{"ts": "1"}]})
    client = wc.OkxWsClient(sub_args=[], on_frame=raise_always,
                            monotonic_fn=lambda: clock[0], log_throttle_s=60.0)
    with caplog.at_level(logging.WARNING):
        for _ in range(500):
            client.handle_raw(bad, recv_ns=1)
    malformed = [r for r in caplog.records if "malformed" in r.getMessage()]
    assert len(malformed) == 1                              # only the first, not 500 tracebacks
    assert client.frame_errors == 500                      # metric still counts every drop

    clock[0] += 61.0                                        # past the throttle window
    client.handle_raw(bad, recv_ns=1)
    malformed = [r for r in caplog.records if "malformed" in r.getMessage()]
    assert len(malformed) == 2                              # logs again, with an aggregate
    assert "suppressed" in malformed[1].getMessage()


def test_reconnect_reason_is_logged(caplog):
    def connect_fn(url):
        raise ConnectionError("1009 frame too large")

    client = wc.OkxWsClient(sub_args=[], on_frame=lambda *a: None,
                            connect_fn=connect_fn, sleep_fn=_noop_sleep, max_reconnects=0)
    with caplog.at_level(logging.WARNING):
        asyncio.run(client.run())
    blob = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "reconnect" in blob and "1009" in blob           # the break reason is visible
