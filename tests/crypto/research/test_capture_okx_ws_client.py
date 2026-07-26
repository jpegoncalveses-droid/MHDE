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
