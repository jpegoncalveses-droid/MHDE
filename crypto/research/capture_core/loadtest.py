"""Capture-core load test: size the per-stream firehose over a bounded window.

Runs the real connection manager against a chosen stream set across every
TRADING USDT-M perp, counts messages + raw wire bytes, and projects daily
volume (raw, and parquet-compressed if a sample is written).

PR-2 use: a PARTIAL sizing of ``@depth@100ms`` + ``@bookTicker`` (the streams
that deliver from this host). aggTrade / markPrice / forceOrder are unmeasured
here (delivery-blocked or sparse) and ADD to the firehose — so the all-529
feasibility call stays OPEN until the full set is sized once delivery returns.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable, Optional

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core.client import CaptureRestClient
from crypto.research.capture_core.conn_manager import ConnectionManager
from crypto.research.capture_core.service import CaptureService, aggtrade_streams

logger = logging.getLogger("mhde.crypto.capture_core.loadtest")

_SECONDS_PER_DAY = 86_400


def summarize(*, messages: int, bytes_in: int, duration_s: float, n_symbols: int,
              parquet_bytes: Optional[int] = None) -> dict:
    """Compute throughput + daily projection from a load-test window."""
    mps = messages / duration_s if duration_s > 0 else 0.0
    bps = bytes_in / duration_s if duration_s > 0 else 0.0
    raw_gb_day = bps * _SECONDS_PER_DAY / 1e9
    out: dict[str, Any] = {
        "messages": messages,
        "bytes_in": bytes_in,
        "duration_s": duration_s,
        "n_symbols": n_symbols,
        "msgs_per_s": mps,
        "raw_bytes_per_s": bps,
        "raw_mib_per_min": bps * 60 / (1024 * 1024),
        "raw_gb_per_day": raw_gb_day,
    }
    if parquet_bytes is not None:
        ratio = bytes_in / parquet_bytes if parquet_bytes else None
        out["parquet_bytes"] = parquet_bytes
        out["compression_ratio"] = ratio
        out["parquet_gb_per_day"] = (raw_gb_day / ratio) if ratio else None
    return out


async def run_loadtest(
    *,
    duration_s: float,
    client: Any = None,
    connect_fn: Optional[Callable[[str], Any]] = None,
    write_root: Optional[str] = None,
    stream_factory: Callable[[list[str]], list[str]] = aggtrade_streams,
) -> dict:
    """Drive ``stream_factory(universe)`` capture for ``duration_s`` seconds.

    If ``write_root`` is set, frames are routed through a (snapshot-disabled)
    CaptureService so the report includes a real parquet compression ratio.
    """
    client = client or CaptureRestClient()
    universe = await asyncio.to_thread(client.fetch_usdtm_perp_universe)
    streams = stream_factory(universe)

    service = None
    if write_root:
        service = CaptureService(root=write_root, client=None,
                                 enable_snapshots=False, install_signals=False)

    def on_message(stream: str, data: Any, recv_ns: int) -> None:
        if service is not None:
            service._on_message(stream, data, recv_ns)

    mgr = ConnectionManager(streams=streams, on_message=on_message,
                            connect_fn=connect_fn)
    done = asyncio.Event()

    async def _stopper() -> None:
        await asyncio.sleep(duration_s)
        mgr.stop()
        done.set()

    async def _flusher() -> None:
        if service is None:
            return
        while not done.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), timeout=cfg.FLUSH_POLL_S)
            service.flush_due()

    logger.info("capture-core loadtest: %d symbols, %d streams, %.0fs window",
                len(universe), len(streams), duration_s)
    started = time.monotonic()
    await asyncio.gather(mgr.run(), _stopper(), _flusher())
    elapsed = time.monotonic() - started

    parquet_bytes = None
    if service is not None:
        service.flush_all()
        parquet_bytes = _dir_bytes(write_root)

    return summarize(messages=mgr.dispatched, bytes_in=mgr.bytes_in,
                     duration_s=elapsed, n_symbols=len(universe),
                     parquet_bytes=parquet_bytes)


def _dir_bytes(root: str) -> int:
    import pathlib
    return sum(p.stat().st_size for p in pathlib.Path(root).rglob("*.parquet"))
