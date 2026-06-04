"""Long-horizon 1h klines store for capture-core (capture-completion piece 2).

This is the **cheap rolling long-context reference frame** ADR-035 provided for —
distinct from the 24h raw firehose buffer. It is a *present-context* store, not a
backtest dataset: seeded once (~90d) and then maintained forward, hourly, with
**closed bars only** (the in-progress bar is never persisted). Downstream features
(30d-high, SMA50, breakout distance, …) are derived from it; nothing here computes
them.

Reuse, not duplication:
  * MAINTENANCE reuses :class:`rest_collector.RestPresentStateCollector` wholesale
    via :data:`KLINES_1H_SPEC` — the shared /fapi weight self-pacer, the
    ``dedup_new_buckets`` cursor, ``store.dataset_writer``, the universe resolver,
    and the run loop. Because the collector process is long-running, its in-memory
    per-(series, symbol) cursor dedups across hourly polls (no write amplification).
  * SEED + RETENTION don't fit the per-cadence collector, so they live here and
    reuse the lower-level primitives: ``client.get_with_weight``,
    ``store.dataset_writer``, ``dedup_new_buckets``, ``fapi_over_budget``,
    ``fetch_usdtm_perp_universe``.

Public REST only; NEVER opens mhde.duckdb or the engine DB; writes only under the
capture-core parquet store.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

import pyarrow as pa

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import store
from crypto.research.capture_core.client import CaptureRestClient
from crypto.research.capture_core.rest_collector import (
    RestPresentStateCollector, dedup_new_buckets, fapi_over_budget,
)
from crypto.research.capture_core.rest_series import SeriesSpec

logger = logging.getLogger("mhde.crypto.capture_core.klines_store")

KLINES_PATH = "/fapi/v1/klines"

#: Full kline row kept raw (default-to-inclusion). Venue numeric fields stay
#: strings (lossless); the two times + trade count are ints. The venue "ignore"
#: trailing field is the only thing dropped (it is documented as unused).
KLINES_1H_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("s", pa.string()),
    ("openTime", pa.int64()),
    ("open", pa.string()),
    ("high", pa.string()),
    ("low", pa.string()),
    ("close", pa.string()),
    ("volume", pa.string()),
    ("closeTime", pa.int64()),
    ("quoteVolume", pa.string()),
    ("trades", pa.int64()),
    ("takerBuyBase", pa.string()),
    ("takerBuyQuote", pa.string()),
])


def parse_klines(data: Any, symbol: Optional[str], recv_ns: int) -> list[dict]:
    """Parse a /fapi/v1/klines array into full rows — **closed bars only**.

    Binance returns each kline as a 12-element array. The in-progress bar (its
    ``closeTime`` is still in the future) is dropped here so it is *never* persisted
    — re-fetched once it closes, on a later poll. ``recv_ns`` supplies "now".
    """
    now_ms = recv_ns // 1_000_000
    rows = []
    for k in data:
        close_time = int(k[6])
        if close_time > now_ms:        # in-progress bar -> never persist
            continue
        rows.append({
            "recv_ts_ns": recv_ns, "s": symbol,
            "openTime": int(k[0]), "open": k[1], "high": k[2], "low": k[3],
            "close": k[4], "volume": k[5], "closeTime": close_time,
            "quoteVolume": k[7], "trades": int(k[8]),
            "takerBuyBase": k[9], "takerBuyQuote": k[10],
        })
    return rows


#: The maintenance series. A /fapi per-symbol windowed series: each poll fetches a
#: few trailing bars (``KLINES_MAINT_LIMIT``) and the collector dedups on openTime.
#: Priority HIGH so it is never shed under /fapi pressure — its weight is trivial
#: (limit<100 => weight 1) and the shared pacer already spaces it politely.
KLINES_1H_SPEC = SeriesSpec(
    "klines_1h", KLINES_PATH, "per_symbol", "fapi", 1,
    cfg.KLINES_MAINT_CADENCE_S, "HIGH", KLINES_1H_SCHEMA, "s", "openTime",
    parse_klines, {"interval": cfg.KLINES_INTERVAL, "limit": cfg.KLINES_MAINT_LIMIT},
    dedup_ts_field="openTime",
)


def build_maintenance_collector(root: str, *, client: Any = None,
                                universe: Optional[Sequence[str]] = None,
                                **kwargs: Any) -> RestPresentStateCollector:
    """A klines-only collector: hourly forward maintenance reusing the full
    present-state machinery (pacer + dedup cursor + writer + universe + loop)."""
    client = client or CaptureRestClient()
    return RestPresentStateCollector(
        root=root, client=client,
        universe=universe,
        universe_fn=(None if universe is not None else client.fetch_usdtm_perp_universe),
        specs=[KLINES_1H_SPEC],
        tick_s=cfg.KLINES_MAINT_TICK_S,
        **kwargs,
    )


def _date_str(ms: int) -> str:
    """UTC ``YYYY-MM-DD`` for a ms timestamp — matches the store partition label."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def seed(root: str, *, days: int = cfg.KLINES_SEED_DAYS, client: Any = None,
         universe: Optional[Sequence[str]] = None, now_ms: Optional[int] = None,
         sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """One-time paginated ~``days`` backfill of closed 1h bars per symbol.

    Pages forward from ``now - days`` with ``limit`` bars/call (~2 calls/symbol at
    90d), paced under the shared /fapi weight budget. All backfilled bars are
    closed (the closed-bars parser is applied for safety). Returns rows written.
    """
    client = client or CaptureRestClient()
    universe = list(universe) if universe is not None else client.fetch_usdtm_perp_universe()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    writer = store.dataset_writer(root, cfg.KLINES_DATASET, KLINES_1H_SCHEMA,
                                  symbol_key="s", time_key="openTime")
    used = 0
    written = 0
    for symbol in universe:
        cursor: Optional[int] = None
        page_start = start_ms
        while page_start < now_ms:
            # shared /fapi self-pacer: back off when the live used-weight is high.
            if fapi_over_budget(used, limit=cfg.FAPI_WEIGHT_LIMIT,
                                fraction=cfg.REST_BUDGET_FRACTION):
                sleep_fn(cfg.REST_BUDGET_BACKOFF_S)
            params = {"symbol": symbol, "interval": cfg.KLINES_INTERVAL,
                      "startTime": page_start, "limit": cfg.KLINES_SEED_LIMIT}
            data, weight = client.get_with_weight(KLINES_PATH, params)
            if weight is not None:
                used = weight
            if not data:
                break
            rows = parse_klines(data, symbol, now_ms * 1_000_000)
            kept, cursor = dedup_new_buckets(rows, "openTime", cursor)
            for r in kept:
                writer.append(r)
            written += len(kept)
            page_start = int(data[-1][0]) + cfg.HOUR_MS   # advance past last openTime
            if len(data) < cfg.KLINES_SEED_LIMIT:
                break                                     # reached the present
    writer.flush_all()
    logger.info("klines seed complete: %d symbols, %d closed bars written",
                len(universe), written)
    return written


def expire_klines_partitions(root: str, *, days: int = cfg.KLINES_RETENTION_DAYS,
                             now_ms: Optional[int] = None) -> list[str]:
    """Delete ``klines_1h`` date partitions older than ``days`` (rolling retention).

    Keeps partitions whose date is >= the cutoff (now - days); removes older ones.
    Returns the removed partition directories. No DB; filesystem-only under the
    capture-core store.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = _date_str(now_ms - days * 86_400_000)
    base = os.path.join(root, cfg.KLINES_DATASET)
    removed: list[str] = []
    if not os.path.isdir(base):
        return removed
    for sym_entry in os.scandir(base):
        if not (sym_entry.is_dir() and sym_entry.name.startswith("symbol=")):
            continue
        for date_entry in os.scandir(sym_entry.path):
            if not (date_entry.is_dir() and date_entry.name.startswith("date=")):
                continue
            day = date_entry.name.split("date=", 1)[1]
            if day < cutoff:                  # ISO dates sort lexicographically
                shutil.rmtree(date_entry.path)
                removed.append(date_entry.path)
    if removed:
        logger.info("klines retention: expired %d partitions older than %s",
                    len(removed), cutoff)
    return removed
