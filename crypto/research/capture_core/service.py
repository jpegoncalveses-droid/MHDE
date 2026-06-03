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

logger = logging.getLogger("mhde.crypto.capture_core.service")


# -- pure helpers --

def aggtrade_streams(universe: Sequence[str]) -> list[str]:
    """Combined-stream names for per-symbol aggTrade across ``universe``."""
    return [f"{s.lower()}@aggTrade" for s in universe]


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
    ) -> None:
        self._root = root
        self._client = client
        self._connect_fn = connect_fn
        self._mgr_factory = mgr_factory or self._default_mgr_factory
        self._per_conn = streams_per_conn
        self._reresolve_interval_s = reresolve_interval_s
        self._flush_poll_s = flush_poll_s
        self._install_signals = install_signals

        self._agg = store.aggtrade_writer(
            root, flush_interval_s=flush_interval_s, flush_max_bytes=flush_max_bytes)
        self._gaps = store.gap_writer(root)

        self._stop = asyncio.Event()
        self._current_mgr: Any = None

    # -- routing --

    def _on_message(self, stream: str, data: dict, recv_ns: int) -> None:
        if stream.endswith("@aggTrade"):
            self._agg.append(aggtrade_row(data, recv_ns))

    def _on_gap(self, streams: list[str], reason: str, start_ms: int, end_ms: int) -> None:
        recorded = time.time_ns()
        for s in streams:
            symbol = s.split("@", 1)[0].upper()
            self._gaps.append({
                "symbol": symbol, "stream": s, "gap_start_ms": start_ms,
                "gap_end_ms": end_ms, "reason": reason,
                "recorded_recv_ts_ns": recorded,
            })

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

    def flush_all(self) -> None:
        self._agg.flush_all()
        self._gaps.flush_all()

    def stats(self) -> dict:
        return {
            "agg_rows_written": self._agg.rows_written,
            "agg_files_written": self._agg.files_written,
            "gap_rows_written": self._gaps.rows_written,
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
            self._agg.flush_due()
            self._gaps.flush_due()

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
        universe = await self._resolve_universe()
        mgr = self._mgr_factory(aggtrade_streams(universe))
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
                    universe = new_universe
                    mgr = self._mgr_factory(aggtrade_streams(universe))
                    self._current_mgr = mgr
                    mgr_task = asyncio.create_task(mgr.run())
                elif mgr_task.done():  # all shards exited unexpectedly -> restart
                    logger.warning("capture-core: manager ended; restarting")
                    mgr = self._mgr_factory(aggtrade_streams(universe))
                    self._current_mgr = mgr
                    mgr_task = asyncio.create_task(mgr.run())
        finally:
            self._stop.set()
            mgr.stop()
            with contextlib.suppress(Exception):
                await mgr_task
            flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flush_task
            self.flush_all()
            logger.info("capture-core service stopped: %s", self.stats())
