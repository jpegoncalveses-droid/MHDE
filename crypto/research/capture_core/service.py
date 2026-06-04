"""Capture-core service: resolve universe -> capture aggTrade -> parquet.

Orchestrates the PR-1 capture path:

  1. Resolve the full TRADING USDT-M perp universe from ``exchangeInfo`` and
     **re-resolve on a cadence** — when the set changes, rebuild the connection
     manager so newly-listed symbols enter the substrate (a change-only rebuild,
     so an unchanged universe keeps its sockets and incurs no gap).
  2. Subscribe to ``<symbol>@aggTrade`` for every symbol; route each frame to the
     aggTrade parquet writer.
  3. Periodically flush due partitions; flush everything on SIGTERM/SIGINT.

NEVER opens ``mhde.duckdb`` or the engine DB; writes only under ``root``.
Depth / bookTicker / markPrice capture and order-book reconstruction land in PR-2.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any, Callable, Optional, Sequence

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import conn_manager as cm
from crypto.research.capture_core import store
from crypto.research.capture_core.book import DepthMaintainer
from crypto.research.capture_core.disk_guard import DiskGuard
from crypto.research.capture_core.snapshot import SnapshotScheduler

logger = logging.getLogger("mhde.crypto.capture_core.service")


# -- pure helpers --

def aggtrade_streams(universe: Sequence[str]) -> list[str]:
    """Combined-stream names for per-symbol aggTrade across ``universe``."""
    return [f"{s.lower()}@aggTrade" for s in universe]


#: Market-wide array streams (one connection covers every symbol).
MARKET_STREAMS = ["!forceOrder@arr", f"!markPrice@arr@{cfg.MARKPRICE_SPEED}"]


def per_symbol_streams(universe: Sequence[str]) -> list[str]:
    """Per-symbol capture streams: aggTrade + depth diff + bookTicker."""
    out: list[str] = []
    for s in universe:
        low = s.lower()
        out += [f"{low}@aggTrade", f"{low}@depth@{cfg.DEPTH_UPDATE_SPEED}",
                f"{low}@bookTicker"]
    return out


def capture_streams(universe: Sequence[str]) -> list[str]:
    """Full PR-2 capture set: per-symbol streams + market-wide array streams."""
    return per_symbol_streams(universe) + MARKET_STREAMS


def depth_bookticker_streams(universe: Sequence[str]) -> list[str]:
    """Just depth + bookTicker (the streams that deliver from this host; used by
    the partial firehose load test)."""
    out: list[str] = []
    for s in universe:
        low = s.lower()
        out += [f"{low}@depth@{cfg.DEPTH_UPDATE_SPEED}", f"{low}@bookTicker"]
    return out


def depth_row(data: dict, recv_ns: int) -> dict:
    """Map a raw depthUpdate diff to a store row (ids->int, levels kept verbatim)."""
    return {
        "recv_ts_ns": recv_ns, "e": data.get("e"),
        "E": int(data["E"]), "T": int(data["T"]), "s": data["s"],
        "U": int(data["U"]), "u": int(data["u"]), "pu": int(data["pu"]),
        "b": data.get("b", []), "a": data.get("a", []),
    }


def bookticker_row(data: dict, recv_ns: int) -> dict:
    return {
        "recv_ts_ns": recv_ns, "e": data.get("e"), "u": int(data["u"]),
        "s": data["s"], "b": data["b"], "B": data["B"], "a": data["a"],
        "A": data["A"], "T": int(data["T"]), "E": int(data["E"]),
    }


def forceorder_rows(data: dict, recv_ns: int) -> list[dict]:
    """Liquidation event -> a single per-symbol row (symbol is in ``o``)."""
    o = data["o"]
    return [{
        "recv_ts_ns": recv_ns, "E": int(data["E"]), "s": o["s"], "S": o["S"],
        "o": o["o"], "f": o["f"], "q": o["q"], "p": o["p"], "ap": o["ap"],
        "X": o["X"], "l": o["l"], "z": o["z"], "T": int(o["T"]),
    }]


def markprice_rows(data: list, recv_ns: int) -> list[dict]:
    """markPrice ARRAY -> one row per symbol."""
    return [{
        "recv_ts_ns": recv_ns, "e": el.get("e"), "E": int(el["E"]), "s": el["s"],
        "p": el["p"], "i": el["i"], "P": el["P"], "r": el["r"], "T": int(el["T"]),
    } for el in data]


def snapshot_row(symbol: str, snap: dict, recv_ns: int) -> dict:
    """Map a REST /fapi/v1/depth snapshot to a depth_snapshot row."""
    return {
        "recv_ts_ns": recv_ns, "s": symbol,
        "lastUpdateId": int(snap["lastUpdateId"]),
        # Partition on event time E; if a snapshot ever lacks it, fall back to the
        # recv-derived ms so the row never lands in date=1970-01-01.
        "E": int(snap.get("E") or recv_ns // 1_000_000), "T": int(snap.get("T", 0)),
        "b": snap.get("bids", []), "a": snap.get("asks", []),
    }


def aggtrade_row(data: dict, recv_ns: int) -> dict:
    """Map an aggTrade ``data`` payload to a store row (ids->int, price->str)."""
    return {
        "recv_ts_ns": recv_ns,
        "e": data.get("e"),
        "E": int(data["E"]),
        "a": int(data["a"]),
        "s": data["s"],
        "p": data["p"],          # venue string, kept lossless
        "q": data["q"],          # venue string, kept lossless
        "f": int(data["f"]),
        "l": int(data["l"]),
        "T": int(data["T"]),
        "m": bool(data["m"]),
    }


def universe_changed(old: Sequence[str], new: Sequence[str]) -> bool:
    """True iff the two universes differ as sets (order-insensitive)."""
    return set(old) != set(new)


class CaptureService:
    """Long-running aggTrade capture service with universe re-resolution."""

    def __init__(
        self,
        *,
        root: str,
        client: Any,
        connect_fn: Optional[Callable[[str], Any]] = None,
        mgr_factory: Optional[Callable[[list[str]], Any]] = None,
        streams_per_conn: int = cfg.STREAMS_PER_CONN,
        reresolve_interval_s: float = cfg.UNIVERSE_RERESOLVE_INTERVAL_S,
        flush_interval_s: float = cfg.FLUSH_INTERVAL_S,
        flush_max_bytes: int = cfg.FLUSH_MAX_BYTES,
        flush_poll_s: float = cfg.FLUSH_POLL_S,
        install_signals: bool = True,
        enable_snapshots: bool = True,
        snap_scheduler: Optional[Any] = None,
        disk_guard: Optional[Any] = None,
        disk_guard_enabled: bool = True,
        disk_check_interval_s: float = cfg.CAPTURE_DISK_CHECK_INTERVAL_S,
    ) -> None:
        self._root = root
        self._client = client
        self._connect_fn = connect_fn
        self._mgr_factory = mgr_factory or self._default_mgr_factory
        self._per_conn = streams_per_conn
        self._reresolve_interval_s = reresolve_interval_s
        self._flush_poll_s = flush_poll_s
        self._install_signals = install_signals

        # PR-3 disk guard: protects the volume by pruning the OLDEST firehose
        # partitions under the soft floor and halting firehose writes under the
        # critical floor (forward-only: dropped, never backfilled).
        if disk_guard is not None:
            self._disk_guard: Any = disk_guard
        elif disk_guard_enabled:
            self._disk_guard = DiskGuard(root)
        else:
            self._disk_guard = None
        self._disk_check_interval_s = disk_check_interval_s
        self._last_disk_check = 0.0

        _wkw = dict(flush_interval_s=flush_interval_s, flush_max_bytes=flush_max_bytes)
        self._agg = store.aggtrade_writer(root, **_wkw)
        self._depth = store.depth_writer(root, **_wkw)
        self._bookticker = store.bookticker_writer(root, **_wkw)
        self._forceorder = store.forceorder_writer(root, **_wkw)
        self._markprice = store.markprice_writer(root, **_wkw)
        self._snapshot = store.depth_snapshot_writer(root, **_wkw)
        self._gaps = store.gap_writer(root)
        self._writers = [self._agg, self._depth, self._bookticker,
                         self._forceorder, self._markprice, self._snapshot, self._gaps]

        # Per-symbol depth sequence maintenance (cursor only; not a level book).
        self._maintainers: dict[str, DepthMaintainer] = {}

        # REST snapshot scheduler (paced/deduped). Injected in tests; built from
        # the client otherwise. None disables depth seeding/resync.
        if snap_scheduler is not None:
            self._snap_sched: Any = snap_scheduler
        elif enable_snapshots and client is not None:
            self._snap_sched = SnapshotScheduler(
                client=client, on_snapshot=self._on_snapshot_arrived,
                min_interval_s=cfg.SNAPSHOT_MIN_INTERVAL_S,
                limit=cfg.DEPTH_SNAPSHOT_LIMIT)
        else:
            self._snap_sched = None

        self._stop = asyncio.Event()
        self._current_mgr: Any = None

    # -- routing --

    def _on_message(self, stream: str, data: Any, recv_ns: int) -> None:
        # Disk guard CRITICAL: drop incoming firehose data (forward-only — a hole is
        # recorded by absence; we never backfill). Resumes when free recovers.
        if self._disk_guard is not None and self._disk_guard.halted:
            return
        if stream.endswith("@aggTrade"):
            self._agg.append(aggtrade_row(data, recv_ns))
        elif "@depth@" in stream:
            self._handle_depth(data, recv_ns)
        elif stream.endswith("@bookTicker"):
            self._bookticker.append(bookticker_row(data, recv_ns))
        elif stream == "!forceOrder@arr":
            for row in forceorder_rows(data, recv_ns):
                self._forceorder.append(row)
        elif stream.startswith("!markPrice@arr"):
            for row in markprice_rows(data, recv_ns):
                self._markprice.append(row)

    # -- depth maintenance --

    def _handle_depth(self, data: dict, recv_ns: int) -> None:
        # Store EVERY raw diff regardless of maintenance state.
        self._depth.append(depth_row(data, recv_ns))
        symbol = data["s"]
        m = self._maintainers.get(symbol)
        if m is None:
            m = self._maintainers[symbol] = DepthMaintainer(symbol)
        # Maintenance works in EVENT-time ms so gap bounds match the manifest's
        # *_ms columns (and the conn-manager gaps).
        res = m.on_diff(int(data["U"]), int(data["u"]), int(data["pu"]),
                        int(data["E"]))
        self._apply_depth_result(symbol, res)

    def _on_snapshot_arrived(self, symbol: str, snap: dict, recv_ns: int) -> None:
        self._snapshot.append(snapshot_row(symbol, snap, recv_ns))
        m = self._maintainers.get(symbol)
        if m is None:
            m = self._maintainers[symbol] = DepthMaintainer(symbol)
        res = m.on_snapshot(int(snap["lastUpdateId"]), int(snap.get("E", 0)))
        self._apply_depth_result(symbol, res)

    def _apply_depth_result(self, symbol: str, res: Any) -> None:
        if res.gap is not None:
            start_ms, end_ms, reason = res.gap
            self._record_gap(symbol, "depth", start_ms, end_ms, reason)
        if res.needs_snapshot and self._snap_sched is not None:
            self._snap_sched.request(symbol)

    def seed_universe(self, symbols: Sequence[str]) -> None:
        """Request an initial depth snapshot for every symbol (paced by scheduler)."""
        if self._snap_sched is None:
            return
        for s in symbols:
            self._snap_sched.request(s)

    def _record_gap(self, symbol: str, stream: str, start_ms: int, end_ms: int,
                    reason: str) -> None:
        self._gaps.append({
            "symbol": symbol, "stream": stream, "gap_start_ms": start_ms,
            "gap_end_ms": end_ms, "reason": reason,
            "recorded_recv_ts_ns": time.time_ns(),
        })

    def _on_gap(self, streams: list[str], reason: str, start_ms: int, end_ms: int) -> None:
        # Connection-level gap (whole shard): one manifest row per per-symbol
        # stream. Array streams (! prefix) have no single symbol -> recorded as-is.
        for s in streams:
            symbol = s.split("@", 1)[0].upper() if not s.startswith("!") else s
            self._record_gap(symbol, s, start_ms, end_ms, reason)

    def _default_mgr_factory(self, streams: list[str]) -> cm.ConnectionManager:
        return cm.ConnectionManager(
            streams=streams, on_message=self._on_message, on_gap=self._on_gap,
            streams_per_conn=self._per_conn, connect_fn=self._connect_fn,
        )

    # -- lifecycle --

    def stop(self) -> None:
        """Request shutdown of the service and the live manager. Idempotent."""
        self._stop.set()
        if self._current_mgr is not None:
            self._current_mgr.stop()
        if self._snap_sched is not None:
            self._snap_sched.stop()

    def flush_all(self) -> None:
        for w in self._writers:
            w.flush_all()

    def flush_due(self) -> None:
        for w in self._writers:
            w.flush_due()

    def stats(self) -> dict:
        return {
            "agg_rows": self._agg.rows_written,
            "depth_rows": self._depth.rows_written,
            "bookticker_rows": self._bookticker.rows_written,
            "forceorder_rows": self._forceorder.rows_written,
            "markprice_rows": self._markprice.rows_written,
            "snapshot_rows": self._snapshot.rows_written,
            "gap_rows": self._gaps.rows_written,
        }

    async def _resolve_universe(self) -> list[str]:
        return await asyncio.to_thread(self._client.fetch_usdtm_perp_universe)

    async def _wait_stop_or_timeout(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=timeout)

    async def _flush_loop(self) -> None:
        # Poll on a short cadence so the per-partition 64 MiB size cap is a real
        # ceiling (a hot partition can exceed it well within the 30s age window);
        # flush_due() itself still honors the 30s age threshold per partition.
        while not self._stop.is_set():
            await self._wait_stop_or_timeout(self._flush_poll_s)
            if self._stop.is_set():
                break
            for w in self._writers:
                w.flush_due()
            self._maybe_enforce_disk_guard()

    def _maybe_enforce_disk_guard(self) -> None:
        """Run the disk guard on its own (coarser) cadence from the flush loop."""
        if self._disk_guard is None:
            return
        now = time.monotonic()
        if now - self._last_disk_check < self._disk_check_interval_s:
            return
        self._last_disk_check = now
        self._disk_guard.enforce()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    async def run(self) -> None:
        """Run until SIGTERM/SIGINT (or :meth:`stop`), re-resolving the universe."""
        if self._install_signals:
            self._install_signal_handlers()

        flush_task = asyncio.create_task(self._flush_loop())
        snap_task = (asyncio.create_task(self._snap_sched.run())
                     if self._snap_sched is not None else None)
        universe = await self._resolve_universe()
        self.seed_universe(universe)
        mgr = self._mgr_factory(capture_streams(universe))
        self._current_mgr = mgr
        mgr_task = asyncio.create_task(mgr.run())
        logger.info("capture-core service started: %d symbols", len(universe))

        try:
            while not self._stop.is_set():
                await self._wait_stop_or_timeout(self._reresolve_interval_s)
                if self._stop.is_set():
                    break
                new_universe = await self._resolve_universe()
                if universe_changed(universe, new_universe):
                    logger.info("capture-core: universe %d -> %d; rebuilding",
                                len(universe), len(new_universe))
                    mgr.stop()
                    await mgr_task
                    # Seed any newly-listed symbols (deduped by the scheduler).
                    self.seed_universe(sorted(set(new_universe) - set(universe)))
                    universe = new_universe
                    mgr = self._mgr_factory(capture_streams(universe))
                    self._current_mgr = mgr
                    mgr_task = asyncio.create_task(mgr.run())
                elif mgr_task.done():  # all shards exited unexpectedly -> restart
                    logger.warning("capture-core: manager ended; restarting")
                    mgr = self._mgr_factory(capture_streams(universe))
                    self._current_mgr = mgr
                    mgr_task = asyncio.create_task(mgr.run())
        finally:
            self._stop.set()
            mgr.stop()
            if self._snap_sched is not None:
                self._snap_sched.stop()
            with contextlib.suppress(Exception):
                await mgr_task
            for task in (flush_task, snap_task):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.flush_all()
            logger.info("capture-core service stopped: %s", self.stats())
