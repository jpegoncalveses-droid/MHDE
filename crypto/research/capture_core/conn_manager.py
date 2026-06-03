"""Capture-core WebSocket connection manager.

Subscribes to combined-stream connections across a sharded stream set and
forwards every valid frame to ``on_message`` (the socket path does nothing but
parse the envelope + stamp a receive timestamp; persistence happens elsewhere).

Discipline adapted from the engine's ``ws_consumer`` (reconnect with
exponential backoff + ±jitter, liveness/heartbeat) but implemented on the raw
``websockets`` client (MHDE does not depend on python-binance):

  * **Sharding** — streams are chunked across connections, well under Binance's
    1024-streams/connection cap.
  * **Heartbeat** — ``websockets`` ping/pong plus a silence timeout: no frame
    within ``silence_timeout_s`` is treated as a dead socket.
  * **Reconnect** — exponential backoff + ±jitter on disconnect; each break
    records a gap (start/end) via ``on_gap`` and is never backfilled.
  * **Proactive reconnect** — reconnect a shard before Binance's 24h limit so
    the drop is voluntary (recorded as a ``proactive_reconnect`` gap, no backoff).

All injection points (``connect_fn``/``sleep_fn``/``time_fn``/``rand_fn``/
``recv_clock``/``wall_ms_fn``) exist so the loop is driven deterministically in
tests without a network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Callable, Optional, Sequence

from crypto.research.capture_core import config as cfg

logger = logging.getLogger("mhde.crypto.capture_core.conn_manager")

OnMessage = Callable[[str, dict, int], None]
OnGap = Callable[[list[str], str, int, int], None]


# -- pure helpers --

def shard_streams(streams: Sequence[str], per_conn: int) -> list[list[str]]:
    """Chunk ``streams`` into per-connection shards of at most ``per_conn``."""
    return [list(streams[i:i + per_conn]) for i in range(0, len(streams), per_conn)]


def combined_url(base: str, streams: Sequence[str]) -> str:
    """Combined-stream URL: ``<base><a>/<b>/<c>``."""
    return base + "/".join(streams)


def compute_backoff(attempt: int, *, base: float, cap: float, jitter: float,
                    rand: Callable[[], float]) -> float:
    """Exponential backoff (capped) with centered ±jitter. ``attempt`` >= 1."""
    raw = min(cap, base * (2 ** (attempt - 1)))
    factor = 1.0 + (rand() * 2 * jitter - jitter)
    return max(0.0, raw * factor)


def proactive_threshold(base: float, shard_index: int, stagger: float) -> float:
    """Per-shard proactive-reconnect threshold so shards don't all trip at once.

    Shard ``N`` waits ``N * stagger`` longer than the base, spreading the daily
    reconnects instead of a near-total blackout when every shard cycles together.
    """
    return base * (1.0 + shard_index * stagger)


def _default_connect_fn(url: str):
    """Real ``websockets`` connection (lazy import; large frames allowed)."""
    import websockets

    return websockets.connect(
        url,
        ping_interval=cfg.WS_PING_INTERVAL_S,
        ping_timeout=cfg.WS_PING_TIMEOUT_S,
        max_size=None,
    )


class ConnectionManager:
    """Run one async task per shard; forward valid combined-stream frames."""

    def __init__(
        self,
        *,
        streams: Sequence[str],
        on_message: OnMessage,
        on_gap: Optional[OnGap] = None,
        streams_per_conn: int = cfg.STREAMS_PER_CONN,
        ws_base: str = cfg.WS_COMBINED_BASE,
        connect_fn: Optional[Callable[[str], Any]] = None,
        backoff_base: float = cfg.RECONNECT_BACKOFF_BASE_S,
        backoff_max: float = cfg.RECONNECT_BACKOFF_MAX_S,
        jitter: float = cfg.RECONNECT_JITTER,
        silence_timeout_s: float = cfg.SOCKET_SILENCE_TIMEOUT_S,
        proactive_reconnect_s: float = cfg.PROACTIVE_RECONNECT_S,
        proactive_stagger: float = cfg.PROACTIVE_STAGGER_FRAC,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        time_fn: Callable[[], float] = time.monotonic,
        rand_fn: Callable[[], float] = random.random,
        recv_clock: Callable[[], int] = time.time_ns,
        wall_ms_fn: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._streams = list(streams)
        self._on_message = on_message
        self._on_gap = on_gap
        self._per_conn = streams_per_conn
        self._ws_base = ws_base
        self._connect_fn = connect_fn or _default_connect_fn
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._jitter = jitter
        self._silence_s = silence_timeout_s
        self._proactive_s = proactive_reconnect_s
        self._proactive_stagger = proactive_stagger
        self._sleep = sleep_fn
        self._time = time_fn
        self._rand = rand_fn
        self._recv_clock = recv_clock
        self._wall_ms = wall_ms_fn

        self._stop = asyncio.Event()
        self.dispatched = 0
        self.dropped = 0          # frames that failed envelope validation
        self.sink_errors = 0      # valid frames whose on_message handler raised
        self.bytes_in = 0  # raw wire bytes seen (uncompressed proxy for load sizing)

    def stop(self) -> None:
        """Request a clean shutdown of all shards. Idempotent."""
        self._stop.set()

    async def run(self) -> None:
        """Connect every shard and run until :meth:`stop`."""
        shards = shard_streams(self._streams, self._per_conn)
        logger.info("capture-core conn_manager: %d streams across %d shards",
                    len(self._streams), len(shards))
        await asyncio.gather(
            *(self._run_shard(s, i) for i, s in enumerate(shards)))

    async def _run_shard(self, streams: list[str], shard_index: int = 0) -> None:
        proactive_s = proactive_threshold(self._proactive_s, shard_index,
                                          self._proactive_stagger)
        attempt = 0
        # A break leaves a pending gap (reason, start_ms); it is CLOSED on the
        # next successful connect, so gap_end bounds the true outage (backoff +
        # handshake), not just "backoff finished".
        pending: Optional[tuple[str, int]] = None
        while not self._stop.is_set():
            try:
                async with self._connect_fn(combined_url(self._ws_base, streams)) as conn:
                    if pending is not None and self._on_gap is not None:
                        reason, start_ms = pending
                        self._on_gap(streams, reason, start_ms, self._wall_ms())
                    pending = None
                    attempt = 0
                    connected_at = self._time()
                    while not self._stop.is_set():
                        if self._time() - connected_at >= proactive_s:
                            pending = ("proactive_reconnect", self._wall_ms())
                            break
                        try:
                            raw = await asyncio.wait_for(conn.recv(), self._silence_s)
                        except asyncio.TimeoutError:
                            pending = ("socket_silence", self._wall_ms())
                            break
                        self._dispatch(raw, self._recv_clock())
            except Exception as exc:  # noqa: BLE001 - any socket error -> reconnect
                pending = ("reconnect", self._wall_ms())
                logger.warning("capture-core shard disconnected (%s: %s); reconnecting",
                               type(exc).__name__, exc)

            if self._stop.is_set():
                return

            if pending is not None and pending[0] == "proactive_reconnect":
                backoff = 0.0  # voluntary cycle; reconnect immediately
            else:
                attempt += 1
                backoff = compute_backoff(attempt, base=self._backoff_base,
                                          cap=self._backoff_max, jitter=self._jitter,
                                          rand=self._rand)
            if backoff:
                await self._sleep(backoff)

    def _dispatch(self, raw: Any, recv_ns: int) -> None:
        """Parse a combined-stream envelope and forward its ``data`` payload."""
        try:
            self.bytes_in += len(raw)
        except TypeError:
            pass
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            self.dropped += 1
            return
        if not isinstance(msg, dict):
            self.dropped += 1
            return
        stream = msg.get("stream")
        data = msg.get("data")
        # `data` is a dict for per-symbol streams and a list for array streams
        # (!markPrice@arr); both are valid — only a missing stream/data is malformed.
        if not stream or not isinstance(data, (dict, list)):
            self.dropped += 1
            return
        self.dispatched += 1
        # A malformed payload must not tear down the whole shard (which would
        # trigger a reconnect + gap for one bad row); isolate the handler.
        try:
            self._on_message(stream, data, recv_ns)
        except Exception as exc:  # noqa: BLE001 - isolate a single bad frame
            self.sink_errors += 1
            logger.warning("capture-core sink error on %s (%s: %s)",
                           stream, type(exc).__name__, exc)
