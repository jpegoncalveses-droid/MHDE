"""Fresh OKX public-WS client — the venue seam (Constraint 1).

Unlike the Binance combined-stream manager (``capture_core/conn_manager.py``, which routes on a
``{stream,data}`` envelope), OKX pushes ``{arg:{channel,instId?,instType?}, data:[...]}``. This
client decodes that envelope, routes on ``arg.channel``, and resolves the instId from
``arg.instId`` — load-bearing for ``bbo-tbt``, whose DATA elements carry no instId. Arg-routing
lives here so rows leaving the client (via ``on_frame``) are venue-agnostic and the downstream
normalizers/store stay unchanged. The client also emits the post-connect ``{op:subscribe,...}``
frame (Binance sends none), sends an app-level ``ping`` on silence (OKX has no server ping), and
reconnects with capped backoff on any socket break.

BUILT-NOT-DEPLOYED (Stage B): no unit enables this.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from crypto.research.capture_core.conn_manager import compute_backoff

WS_PUBLIC_BASE = "wss://ws.okx.com:8443/ws/v5/public"   # keyless single plane
PING_INTERVAL_S = 20.0                                   # app-level ping well under OKX's 30s idle
RECONNECT_BACKOFF_BASE_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0
RECONNECT_JITTER = 0.2

# The callback the client hands each decoded data frame to.
OnFrame = Callable[[str, Optional[str], list, int], None]
OnGap = Callable[[str], None]


@dataclass(frozen=True)
class DecodedFrame:
    kind: str                       # 'data' | 'ack' | 'error' | 'other'
    channel: Optional[str] = None
    inst_id: Optional[str] = None
    inst_type: Optional[str] = None
    data: Optional[list] = None
    arg: Optional[dict] = None


def build_subscribe_frame(args: Sequence[dict]) -> str:
    """The OKX ``{op:subscribe, args:[{channel,instId|instType}, ...]}`` frame."""
    return json.dumps({"op": "subscribe", "args": list(args)})


def decode_envelope(raw: Any) -> DecodedFrame:
    """Classify one raw OKX WS message. Non-JSON (e.g. ``pong``) -> ``other``."""
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return DecodedFrame(kind="other")
    if not isinstance(msg, dict):
        return DecodedFrame(kind="other")
    event = msg.get("event")
    if event == "error":
        return DecodedFrame(kind="error", arg=msg.get("arg"))
    if event is not None:                               # subscribe/unsubscribe/login ack
        return DecodedFrame(kind="ack", arg=msg.get("arg"))
    if "data" in msg:
        arg = msg.get("arg", {}) or {}
        return DecodedFrame(kind="data", channel=arg.get("channel"), inst_id=arg.get("instId"),
                            inst_type=arg.get("instType"), data=msg["data"], arg=arg)
    return DecodedFrame(kind="other")


def _default_connect_fn(url: str):
    """Real ``websockets`` connection (lazy import)."""
    import websockets

    return websockets.connect(url, ping_interval=None, max_size=None)


class OkxWsClient:
    """Own never-blocking recv loop: subscribe on connect, route data frames, reconnect on break."""

    def __init__(
        self,
        *,
        sub_args: Sequence[dict],
        on_frame: OnFrame,
        on_gap: Optional[OnGap] = None,
        url: str = WS_PUBLIC_BASE,
        connect_fn: Optional[Callable[[str], Any]] = None,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        rand_fn: Callable[[], float] = random.random,
        recv_clock: Callable[[], int] = time.time_ns,
        recv_timeout_s: float = PING_INTERVAL_S,
        max_reconnects: Optional[int] = None,
    ) -> None:
        self._sub_args = list(sub_args)
        self._on_frame = on_frame
        self._on_gap = on_gap
        self._url = url
        self._connect_fn = connect_fn or _default_connect_fn
        self._sleep = sleep_fn
        self._rand = rand_fn
        self._recv_clock = recv_clock
        self._recv_timeout_s = recv_timeout_s
        self._max_reconnects = max_reconnects
        self._stop = asyncio.Event()
        self.frames_routed = 0
        self.reconnects = 0

    def stop(self) -> None:
        self._stop.set()

    def handle_raw(self, raw: Any, recv_ns: int) -> None:
        """Decode one raw message and forward data frames to ``on_frame`` (ack/error dropped)."""
        frame = decode_envelope(raw)
        if frame.kind != "data":
            return
        self.frames_routed += 1
        self._on_frame(frame.channel, frame.inst_id, frame.data or [], recv_ns)

    async def run(self) -> None:
        reconnects = 0
        while not self._stop.is_set():
            try:
                async with self._connect_fn(self._url) as conn:
                    await conn.send(build_subscribe_frame(self._sub_args))
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(conn.recv(), self._recv_timeout_s)
                        except asyncio.TimeoutError:
                            await conn.send("ping")           # app-level keepalive
                            continue
                        self.handle_raw(raw, recv_ns=self._recv_clock())
            except Exception:                                 # noqa: BLE001 — any break -> reconnect
                if self._stop.is_set():
                    break
                if self._on_gap is not None:
                    self._on_gap("socket_break")
                reconnects += 1
                self.reconnects = reconnects
                if self._max_reconnects is not None and reconnects > self._max_reconnects:
                    break
                await self._sleep(compute_backoff(
                    reconnects, base=RECONNECT_BACKOFF_BASE_S, cap=RECONNECT_BACKOFF_MAX_S,
                    jitter=RECONNECT_JITTER, rand=self._rand))
