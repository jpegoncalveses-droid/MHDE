"""Capture-core load test: size the 529-symbol aggTrade firehose.

Runs the real connection manager against every TRADING USDT-M perp's
``@aggTrade`` stream for a bounded window, counts messages + raw wire bytes, and
projects daily volume (raw, and parquet-compressed if a sample is written).

This is the PR-1 instrument for the operator's GO condition: if all-529 proves
infeasible, we report the measured throughput/disk numbers and HALT rather than
pre-committing a trim rule.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import store
from crypto.research.capture_core.client import CaptureRestClient
from crypto.research.capture_core.conn_manager import ConnectionManager
from crypto.research.capture_core.service import aggtrade_row, aggtrade_streams

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
) -> dict:
    """Drive aggTrade capture for the full universe for ``duration_s`` seconds.

    If ``write_root`` is given, frames are also written to a parquet sample so
    the report can include a real compression ratio.
    """
    client = client or CaptureRestClient()
    universe = await asyncio.to_thread(client.fetch_usdtm_perp_universe)
    streams = aggtrade_streams(universe)

    writer = store.aggtrade_writer(write_root) if write_root else None
    count = {"n": 0}

    def on_message(stream: str, data: dict, recv_ns: int) -> None:
        count["n"] += 1
        if writer is not None and stream.endswith("@aggTrade"):
            writer.append(aggtrade_row(data, recv_ns))

    mgr = ConnectionManager(
        streams=streams, on_message=on_message, connect_fn=connect_fn,
    )

    started = time.monotonic()

    async def _stopper() -> None:
        await asyncio.sleep(duration_s)
        mgr.stop()

    logger.info("capture-core loadtest: %d symbols, %d streams, %.0fs window",
                len(universe), len(streams), duration_s)
    await asyncio.gather(mgr.run(), _stopper())
    elapsed = time.monotonic() - started

    parquet_bytes = None
    if writer is not None:
        writer.flush_all()
        parquet_bytes = _dir_bytes(write_root)

    return summarize(messages=mgr.dispatched, bytes_in=mgr.bytes_in,
                     duration_s=elapsed, n_symbols=len(universe),
                     parquet_bytes=parquet_bytes)


def _dir_bytes(root: str) -> int:
    import pathlib
    return sum(p.stat().st_size for p in pathlib.Path(root).rglob("*.parquet"))
