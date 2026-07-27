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
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from crypto.research.capture_core.conn_manager import compute_backoff

logger = logging.getLogger("mhde.crypto.capture_core_okx.ws_client")

WS_PUBLIC_BASE = "wss://ws.okx.com:8443/ws/v5/public"   # keyless single plane
PING_INTERVAL_S = 20.0                                   # app-level ping well under OKX's 30s idle
RECONNECT_BACKOFF_BASE_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0
RECONNECT_JITTER = 0.2
#: OKX closes a WS request >64 KiB with 1009; the full 273-instrument universe is a single
#: ~71 KiB subscribe frame, so the args are chunked to stay comfortably under the limit.
MAX_SUBSCRIBE_FRAME_BYTES = 60000

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
    code: Optional[str] = None      # OKX error code (kind == 'error')
    msg: Optional[str] = None       # OKX error message


def build_subscribe_frame(args: Sequence[dict]) -> str:
    """The OKX ``{op:subscribe, args:[{channel,instId|instType}, ...]}`` frame."""
    return json.dumps({"op": "subscribe", "args": list(args)})


def chunk_subscribe_args(args: Sequence[dict],
                         max_bytes: int = MAX_SUBSCRIBE_FRAME_BYTES) -> list[list[dict]]:
    """Split ``args`` into batches whose subscribe frame each stays under ``max_bytes``.

    OKX closes the socket (WS 1009) on a subscribe request over its 64 KiB frame limit, so the
    full universe cannot be subscribed in one frame. Greedy, order-preserving; the per-arg size
    is estimated conservatively (``+2`` for the ``", "`` separator) so every emitted frame is
    guaranteed under the limit.
    """
    base = len(build_subscribe_frame([]))
    chunks: list[list[dict]] = []
    batch: list[dict] = []
    size = base
    for arg in args:
        arg_len = len(json.dumps(arg)) + 2
        if batch and size + arg_len > max_bytes:
            chunks.append(batch)
            batch, size = [], base
        batch.append(arg)
        size += arg_len
    if batch:
        chunks.append(batch)
    return chunks


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
        return DecodedFrame(kind="error", arg=msg.get("arg"),
                            code=msg.get("code"), msg=msg.get("msg"))
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
        subscribe_max_bytes: int = MAX_SUBSCRIBE_FRAME_BYTES,
        monotonic_fn: Callable[[], float] = time.monotonic,
        log_throttle_s: float = 60.0,
        max_reconnects: Optional[int] = None,
    ) -> None:
        self._sub_args = list(sub_args)
        self._on_frame = on_frame
        self._on_gap = on_gap
        self._url = url
        self._connect_fn = connect_fn or _default_connect_fn
        self._subscribe_max_bytes = subscribe_max_bytes
        self._sleep = sleep_fn
        self._rand = rand_fn
        self._recv_clock = recv_clock
        self._recv_timeout_s = recv_timeout_s
        self._max_reconnects = max_reconnects
        self._monotonic = monotonic_fn
        self._log_throttle_s = log_throttle_s
        self._log_last: dict = {}
        self._log_suppressed: dict = {}
        self._log_seen: set = set()
        self._stop = asyncio.Event()
        self.frames_routed = 0
        self.frame_errors = 0
        self.reconnects = 0

    def stop(self) -> None:
        self._stop.set()

    def _throttled_log(self, key) -> tuple:
        """KI-165 guard: cap a per-frame/per-error log to at most one line per throttle window
        per ``key``. Returns ``(should_log, suppressed_count, is_first_ever)`` — callers attach a
        traceback only on the first occurrence and an aggregate count on the throttled resume."""
        now = self._monotonic()
        last = self._log_last.get(key)
        if last is None or now - last >= self._log_throttle_s:
            suppressed = self._log_suppressed.pop(key, 0)
            self._log_last[key] = now
            first = key not in self._log_seen
            self._log_seen.add(key)
            return True, suppressed, first
        self._log_suppressed[key] = self._log_suppressed.get(key, 0) + 1
        return False, 0, False

    def handle_raw(self, raw: Any, recv_ns: int) -> None:
        """Decode one raw message and forward data frames to ``on_frame`` (ack/error dropped).

        A malformed payload (KeyError/IndexError from a one-sided book, a field OKX renamed,
        etc.) is ISOLATED here — dropped and counted — so it can never propagate to ``run``'s
        socket-break handler and tear down the single universe-wide connection (mirrors the
        Binance ``conn_manager._dispatch`` isolation).
        """
        frame = decode_envelope(raw)
        if frame.kind == "error":                          # surface OKX errors loudly, never drop silently
            self.frame_errors += 1
            do, supp, _ = self._throttled_log(("error", frame.code))
            if do:
                extra = f" (+{supp} more suppressed)" if supp else ""
                logger.warning("okx ws: OKX error frame code=%s msg=%s arg=%s%s",
                               frame.code, frame.msg, frame.arg, extra)
            return
        if frame.kind != "data":
            return
        self.frames_routed += 1
        try:
            self._on_frame(frame.channel, frame.inst_id, frame.data or [], recv_ns)
        except Exception:                              # noqa: BLE001 — isolate one bad frame
            self.frame_errors += 1
            # KI-165: rate-limit — a structurally-bad channel raises on EVERY frame at firehose
            # rate; a per-frame traceback would flood /var/log/syslog and fill the disk.
            do, supp, first = self._throttled_log(("malformed", frame.channel))
            if do:
                extra = f" (+{supp} suppressed since last)" if supp else ""
                logger.warning("okx ws: dropped malformed %s frame%s",
                               frame.channel, extra, exc_info=first)

    async def run(self) -> None:
        reconnects = 0
        while not self._stop.is_set():
            try:
                async with self._connect_fn(self._url) as conn:
                    for batch in chunk_subscribe_args(self._sub_args, self._subscribe_max_bytes):
                        await conn.send(build_subscribe_frame(batch))   # chunked <64 KiB (WS 1009)
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(conn.recv(), self._recv_timeout_s)
                        except asyncio.TimeoutError:
                            await conn.send("ping")           # app-level keepalive
                            continue
                        self.handle_raw(raw, recv_ns=self._recv_clock())
            except Exception as exc:                          # noqa: BLE001 — any break -> reconnect
                if self._stop.is_set():
                    break
                if self._on_gap is not None:
                    self._on_gap("socket_break")
                reconnects += 1
                self.reconnects = reconnects
                logger.warning("okx ws: connection broke (%r); reconnecting (attempt %d)",
                               exc, reconnects)
                if self._max_reconnects is not None and reconnects > self._max_reconnects:
                    break
                await self._sleep(compute_backoff(
                    reconnects, base=RECONNECT_BACKOFF_BASE_S, cap=RECONNECT_BACKOFF_MAX_S,
                    jitter=RECONNECT_JITTER, rand=self._rand))
