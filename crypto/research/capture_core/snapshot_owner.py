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
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import rest_throttle as rt

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
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ) -> None:
        self._fetch = fetch_fn
        self._throttle = throttle
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
            await self._throttle.acquire(cfg.DEPTH_SNAPSHOT_WEIGHT)
            snap = await self._to_thread(self._fetch, symbol, self._limit)
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


def build_owner(
    client: Any,
    *,
    socket_path: str = cfg.CAPTURE_SNAPSHOT_SOCKET_PATH,
    reserved: int = cfg.CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN,
    limit: int = cfg.DEPTH_SNAPSHOT_LIMIT,
    clock: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
) -> SnapshotOwner:
    """Wire an owner: read the live REQUEST_WEIGHT cap, reserve headroom, throttle."""
    cap = client.fetch_request_weight_limit(fallback=cfg.FAPI_WEIGHT_LIMIT)
    budget = rt.snapshot_weight_budget(cap, reserved)
    throttle = rt.WeightThrottle(budget, clock=clock, sleep_fn=sleep_fn)
    return SnapshotOwner(fetch_fn=client.fetch_depth_snapshot, throttle=throttle,
                         socket_path=socket_path, limit=limit)
