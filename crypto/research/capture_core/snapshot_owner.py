"""Snapshot-owner: the sole ``/fapi/v1/depth`` broker over a unix socket (ADR-039 2a).

The owner is the only process that calls ``fetch_depth_snapshot``; shard clients ask
for a symbol over a unix request/response socket and receive ``{symbol, snapshot}`` or
a typed error. All fetches serialize through ONE :class:`WeightThrottle` (the global
REST budget, structural for any number of clients) and are deduped per symbol
(concurrent requests for the same symbol collapse to one REST call). Owner-down
surfaces to the client as a clean :class:`SnapshotOwnerUnavailable` (never a hang); a
mid-request connection drop replays on reconnect (idempotent — the owner dedupes). The
owner NEVER writes parquet: the shard writes its own ``depth_snapshot`` row from the
returned payload, preserving the one-writer-per-partition invariant.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from typing import Any, Awaitable, Callable, Optional

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import sd_notify
from crypto.research.capture_core import rest_throttle as rt
from crypto.research.capture_core.client import RateLimited
from crypto.research.capture_core.rest_header_gate import HeaderGate

logger = logging.getLogger("mhde.crypto.capture_core.snapshot_owner")


class SnapshotOwnerUnavailable(Exception):
    """The owner socket is unreachable (owner down, or it dropped the request twice)."""


FetchFn = Callable[[str, int], dict]


class SnapshotOwner:
    def __init__(
        self,
        *,
        fetch_fn: FetchFn,
        throttle: "rt.WeightThrottle",
        socket_path: str,
        limit: int = cfg.DEPTH_SNAPSHOT_LIMIT,
        gate: Optional[HeaderGate] = None,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ) -> None:
        self._fetch = fetch_fn
        self._throttle = throttle
        # All-traffic header-gate (ADR-039 2b). When set, ``fetch_fn`` returns
        # ``(snapshot, used_weight)`` and the gate backs off on the live per-IP
        # X-MBX-USED-WEIGHT-1M; when None, ``fetch_fn`` returns the snapshot dict and only
        # the throttle paces (the single-process / stage-2a behavior, unchanged).
        self._gate = gate
        self._path = socket_path
        self._limit = limit
        self._to_thread = to_thread
        self._pending: "dict[str, asyncio.Future[dict]]" = {}
        self._tasks: "set[asyncio.Task]" = set()
        self._server: Any = None
        self.fetched = 0
        self.errors = 0
        self.served = 0

    async def start(self) -> None:
        d = os.path.dirname(self._path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            os.unlink(self._path)              # clear a stale socket from a prior run
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle, path=self._path)

    async def serve(self) -> None:
        """Start (if needed) and serve until closed — the runnable entry point."""
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    req = json.loads(line)
                    symbol = req["symbol"] if isinstance(req, dict) else None
                except (ValueError, TypeError, KeyError):
                    symbol = None
                if not isinstance(symbol, str) or not symbol:
                    await self._send(writer, {"error": "bad_request"})
                    continue
                await self._send(writer, await self._snapshot(symbol))
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _snapshot(self, symbol: str) -> dict:
        fut = self._pending.get(symbol)
        if fut is None:                        # first requester -> launch ONE fetch
            fut = asyncio.get_running_loop().create_future()
            self._pending[symbol] = fut
            task = asyncio.ensure_future(self._do_fetch(symbol, fut))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        try:
            snap = await fut                   # later requesters piggyback (dedup)
        except Exception as exc:  # noqa: BLE001
            return {"symbol": symbol, "error": "fetch_failed", "detail": str(exc)}
        self.served += 1
        return {"symbol": symbol, "snapshot": snap}

    async def _do_fetch(self, symbol: str, fut: "asyncio.Future[dict]") -> None:
        try:
            if self._gate is not None:
                await self._gate.acquire()        # all-traffic header backstop (wall-clock)
            await self._throttle.acquire(cfg.DEPTH_SNAPSHOT_WEIGHT)  # steady pacer (monotonic)
            try:
                result = await self._to_thread(self._fetch, symbol, self._limit)
            except RateLimited as rl:             # hard backstop: respect Retry-After
                if self._gate is not None:
                    self._gate.handle_429(rl.retry_after)
                raise
            if self._gate is not None:
                snap, used = result               # weight-aware fetch -> (snapshot, used)
                self._gate.observe(used)          # None header -> graceful throttle-only
            else:
                snap = result                     # plain fetch -> snapshot dict (stage-2a)
            self.fetched += 1
            if not fut.done():
                fut.set_result(snap)
        except Exception as exc:  # noqa: BLE001 - isolate one symbol's fetch
            self.errors += 1
            logger.warning("snapshot-owner fetch failed for %s (%s: %s)",
                           symbol, type(exc).__name__, exc)
            if not fut.done():
                fut.set_exception(exc)
        finally:
            self._pending.pop(symbol, None)

    async def _send(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()


class SnapshotClient:
    """Shard-side helper: request a depth snapshot from the owner over the socket."""

    def __init__(
        self,
        socket_path: str,
        *,
        open_conn: Callable[..., Awaitable[tuple]] = asyncio.open_unix_connection,
    ) -> None:
        self._path = socket_path
        self._open = open_conn

    async def request(self, symbol: str) -> dict:
        return await self._roundtrip(json.dumps({"symbol": symbol}))

    async def request_raw(self, raw: str) -> dict:
        """Send a raw (possibly malformed) line — exercises the owner's typed errors."""
        return await self._roundtrip(raw)

    async def _roundtrip(self, payload: str) -> dict:
        last: Any = None
        for _ in (1, 2):                       # one reconnect+resend = in-flight replay
            try:
                reader, writer = await self._open(self._path)
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                raise SnapshotOwnerUnavailable(str(exc)) from exc
            try:
                writer.write((payload + "\n").encode())
                await writer.drain()
                line = await reader.readline()
                if not line:                   # dropped before a response -> replay
                    raise ConnectionResetError("owner closed before responding")
                return json.loads(line)
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                last = exc
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
        raise SnapshotOwnerUnavailable(f"owner dropped request after retry: {last}")


class SnapshotClientScheduler:
    """Scheduler-shaped seeding adapter for a SHARD process (ADR-039 stage 2b).

    Drop-in for :class:`~crypto.research.capture_core.snapshot.SnapshotScheduler`
    (same ``request``/``run``/``stop`` interface), but instead of calling REST
    directly it requests each snapshot from the snapshot-owner over the socket
    (:class:`SnapshotClient`). The OWNER does the global REST throttling + per-symbol
    dedup; this just queues symbols, dials the owner, and forwards the returned
    payload to ``on_snapshot``. Owner-down is non-fatal: the symbol's seed is skipped
    (counted), and the book re-requests it on its next resync.
    """

    def __init__(
        self,
        *,
        client: "SnapshotClient",
        on_snapshot: Callable[[str, dict, int], None],
        clock_ns: Callable[[], int] = time.time_ns,
        sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        backoff_initial: Optional[float] = None,
        backoff_max: Optional[float] = None,
    ) -> None:
        self._client = client
        self._on_snapshot = on_snapshot
        self._clock_ns = clock_ns
        self._sleep = sleep_fn
        self._backoff_initial = (backoff_initial if backoff_initial is not None
                                 else cfg.CAPTURE_SEED_RETRY_BACKOFF_INITIAL_S)
        self._backoff_max = (backoff_max if backoff_max is not None
                             else cfg.CAPTURE_SEED_RETRY_BACKOFF_MAX_S)
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._pending: "set[str]" = set()
        self._retry_delay: "dict[str, float]" = {}
        self._retry_tasks: "set[asyncio.Future]" = set()
        self._stop = asyncio.Event()
        self.fetched = 0
        self.errors = 0

    def request(self, symbol: str) -> bool:
        """Queue a snapshot for ``symbol``. Returns False if already pending."""
        if symbol in self._pending:
            return False
        self._pending.add(symbol)
        self._queue.put_nowait(symbol)
        return True

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                symbol = await self._next()
                if symbol is None:
                    return
                seeded = False
                try:
                    resp = await self._client.request(symbol)
                    snap = resp.get("snapshot") if isinstance(resp, dict) else None
                    if snap is not None:
                        self._on_snapshot(symbol, snap, self._clock_ns())
                        self.fetched += 1
                        seeded = True
                    else:                              # typed error from the owner
                        self.errors += 1
                except SnapshotOwnerUnavailable:
                    self.errors += 1
                except Exception as exc:  # noqa: BLE001 - isolate ONE symbol's seed
                    # (parity with SnapshotScheduler): a malformed/raising owner response
                    # or an on_snapshot error must not kill the loop and silently stale
                    # the whole shard's books. NOT BaseException — CancelledError/
                    # KeyboardInterrupt/SystemExit still propagate for clean shutdown.
                    self.errors += 1
                    logger.warning("snapshot-client seed failed for %s (%s: %s)",
                                   symbol, type(exc).__name__, exc)
                if seeded:
                    self._pending.discard(symbol)       # done; clear dedup + backoff
                    self._retry_delay.pop(symbol, None)
                else:
                    # DURABLE retry: re-queue the symbol after a capped exponential
                    # backoff WITHOUT blocking the loop (other symbols keep seeding). It
                    # stays in _pending so request() still dedups. Closes the dropped-seed
                    # leak — a failed seed is never silently forgotten (which would leave
                    # the symbol's book unsynced and its diff buffer growing forever).
                    self._schedule_retry(symbol)
        finally:
            for t in self._retry_tasks:
                t.cancel()
            if self._retry_tasks:
                await asyncio.gather(*self._retry_tasks, return_exceptions=True)
            self._retry_tasks.clear()

    def _schedule_retry(self, symbol: str) -> None:
        delay = self._retry_delay.get(symbol, self._backoff_initial)
        self._retry_delay[symbol] = min(delay * 2.0, self._backoff_max)
        task = asyncio.ensure_future(self._delayed_requeue(symbol, delay))
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def _delayed_requeue(self, symbol: str, delay: float) -> None:
        await self._sleep(delay)
        if not self._stop.is_set():
            self._queue.put_nowait(symbol)

    async def _next(self):
        get_task = asyncio.ensure_future(self._queue.get())
        stop_task = asyncio.ensure_future(self._stop.wait())
        done, pending = await asyncio.wait(
            {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        if get_task in done:
            return get_task.result()
        return None


def build_owner(
    client: Any,
    *,
    socket_path: str = cfg.CAPTURE_SNAPSHOT_SOCKET_PATH,
    reserved: int = cfg.CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN,
    margin: int = cfg.CAPTURE_HEADER_GATE_MARGIN,
    limit: int = cfg.DEPTH_SNAPSHOT_LIMIT,
    clock: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
    wall_clock: Callable[[], float] = time.time,
) -> SnapshotOwner:
    """Wire an owner: read the live REQUEST_WEIGHT cap, set the throttle budget (reserve
    headroom) AND the all-traffic header-gate (back off on live used-weight). The owner
    fetches via ``get_with_weight`` so each depth response surfaces X-MBX-USED-WEIGHT-1M
    (and raises ``RateLimited`` on 429 for the gate's Retry-After backstop)."""
    cap = client.fetch_request_weight_limit(fallback=cfg.FAPI_WEIGHT_LIMIT)
    budget = rt.snapshot_weight_budget(cap, reserved)
    throttle = rt.WeightThrottle(budget, clock=clock, sleep_fn=sleep_fn)
    gate = HeaderGate(cap=cap, margin=margin, wall_clock=wall_clock, sleep_fn=sleep_fn)

    def _weight_fetch(symbol: str, lim: int):
        return client.get_with_weight("/fapi/v1/depth", {"symbol": symbol, "limit": lim})

    return SnapshotOwner(fetch_fn=_weight_fetch, throttle=throttle, gate=gate,
                         socket_path=socket_path, limit=limit)


async def run_owner(
    owner: SnapshotOwner,
    *,
    stop_event: Optional[asyncio.Event] = None,
    install_signal_handlers: bool = True,
    ready_event: Optional[asyncio.Event] = None,
    notifier: Optional[Any] = None,
) -> None:
    """Run the owner as a standalone process (the ``crypto capture-owner-run`` body).

    Binds the unix socket, serves until SIGTERM/SIGINT (or ``stop_event`` is set), then
    releases the socket. Single-box testable — pass ``stop_event`` to drive shutdown
    deterministically and ``ready_event`` to await the moment the socket is listening. The
    socket is ALWAYS unlinked on exit (no stale socket, no orphaned server), even if serving
    raises.

    ADR-039 gap 3 systemd wiring: ``owner.start()`` is INSIDE the try whose finally removes
    the signal handlers, so a bind failure no longer leaks them. When ``notifier`` is given,
    emit systemd ``READY=1`` once the socket is bound and run a STEADY ``WATCHDOG=1`` keepalive
    (the owner is request-driven and legitimately idle, so its watchdog is time-based, NOT
    activity-gated).
    """
    loop = asyncio.get_running_loop()
    stop = stop_event if stop_event is not None else asyncio.Event()
    installed: "list[signal.Signals]" = []
    if install_signal_handlers:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                # No signal support (non-main thread / unsupported platform): rely on
                # stop_event. The runner still serves and shuts down cleanly.
                pass

    serve_task = None
    stop_task = None
    wd_task = None
    try:
        await owner.start()                   # bind the socket up-front (listening now)
        if ready_event is not None:
            ready_event.set()
        if notifier is not None:
            notifier.ready()                  # systemd READY=1 — socket bound + serving
        serve_task = asyncio.ensure_future(owner.serve())
        stop_task = asyncio.ensure_future(stop.wait())
        wd_task = _spawn_owner_watchdog(notifier, stop)
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if serve_task.done() and not serve_task.cancelled():
            serve_task.result()               # surface a serve-time failure (e.g. bind)
    finally:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass
        # Guard each task with `is not None`: a start() failure leaves them unassigned, and
        # touching `t.done()` on None would mask the original bind exception with AttributeError.
        for t in (serve_task, stop_task, wd_task):
            if t is not None and not t.done():
                t.cancel()
        await owner.stop()                    # close server + unlink socket (idempotent)
        for t in (serve_task, stop_task, wd_task):   # drain the cancelled tasks
            if t is None:
                continue
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def _spawn_owner_watchdog(notifier, stop: asyncio.Event):
    """Steady systemd WATCHDOG=1 keepalive for the owner. Returns None when there is no
    notifier or no ``WATCHDOG_USEC`` (manual / non-systemd runs), so nothing is spawned."""
    if notifier is None:
        return None
    interval = sd_notify.watchdog_interval_s()
    if interval is None:
        return None

    async def _loop():
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                break
            notifier.watchdog()

    return asyncio.ensure_future(_loop())
