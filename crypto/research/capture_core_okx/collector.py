"""OKX Stage A collector factories: reuse ``RestPresentStateCollector`` unchanged.

The Binance collector loop (scheduling, budget hooks, windowed-bucket dedup,
per-series writers, universe re-resolve, clean shutdown) drives the OKX specs
as-is; this module adds only:

  * the :class:`GapTracker` — the collector-outage gap contract for REST
    series: a successful poll arriving more than ``factor x cadence`` after the
    previous success writes one ``_gaps`` manifest row for that series (reason
    ``rest_outage``, symbol ``*``), same schema/dataset as the Binance WS gap
    manifest. First-success-after-start records nothing (an unknown prior is a
    hole by absence — never guessed).
  * the two factories (as-of + klines) wiring client, universe, SymbolMap, and
    gap tracking together;
  * :func:`seed_klines` — the one-time ~90d backfill, paging BACKWARD through
    ``/api/v5/market/history-candles`` (OKX pages newest-first; ``after``
    returns strictly-older rows, so pages are disjoint by construction and the
    forward-cursor dedup helper does not apply);
  * :func:`collect_once_and_flush` — the ``--once`` path used by the reader-
    parity gate and operator smoke runs.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Callable, Optional, Sequence

from crypto.research.capture_core import config as cc_cfg
from crypto.research.capture_core import store
from crypto.research.capture_core.klines_store import KLINES_1H_SCHEMA
from crypto.research.capture_core.rest_collector import RestPresentStateCollector
from crypto.research.capture_core_okx import config as cfg
from crypto.research.capture_core_okx import series as okx_series
from crypto.research.capture_core_okx.client import OkxRestClient
from crypto.research.capture_core_okx.series import OkxSeriesSpec, parse_candles
from crypto.research.capture_core_okx.symbols import SymbolMap

logger = logging.getLogger("mhde.crypto.capture_core_okx.collector")


class GapTracker:
    """Wrap series specs so poll silences longer than the threshold are recorded
    as ``_gaps`` rows. Success times are tracked per series (one row per series
    per outage); rows are flushed immediately (gap events are rare)."""

    def __init__(self, root: str, *,
                 silence_factor: float = cfg.GAP_SILENCE_FACTOR) -> None:
        self._writer = store.gap_writer(root)
        self._factor = silence_factor
        self._last_success_ms: dict[str, int] = {}

    def wrap(self, spec: OkxSeriesSpec) -> OkxSeriesSpec:
        orig_parse = spec.parse

        def parse(data: Any, key: Optional[str], recv_ns: int) -> list[dict]:
            now_ms = recv_ns // 1_000_000
            last = self._last_success_ms.get(spec.name)
            threshold_ms = self._factor * spec.target_cadence_s * 1000.0
            if last is not None and now_ms - last > threshold_ms:
                self._writer.append({
                    "symbol": "*", "stream": spec.name,
                    "gap_start_ms": last, "gap_end_ms": now_ms,
                    "reason": cfg.GAP_REASON, "recorded_recv_ts_ns": recv_ns,
                })
                self._writer.flush_all()
                logger.warning("capture-okx gap recorded: %s silent %.0fs",
                               spec.name, (now_ms - last) / 1000.0)
            self._last_success_ms[spec.name] = now_ms
            return orig_parse(data, key, recv_ns)

        return dataclasses.replace(spec, parse=parse)


def _universe_fn(symbol_map: SymbolMap, client: OkxRestClient) -> Callable[[], list[str]]:
    def refresh() -> list[str]:
        inst_ids = client.fetch_okx_linear_usdt_universe()
        symbol_map.update(inst_ids)          # keeps the all-scope join filters current
        return inst_ids
    return refresh


def build_okx_asof_collector(
    root: str, *,
    client: Optional[OkxRestClient] = None,
    universe: Optional[Sequence[str]] = None,
    gap_silence_factor: float = cfg.GAP_SILENCE_FACTOR,
    **kwargs: Any,
) -> RestPresentStateCollector:
    """The 7-series as-of collector over the OKX root (universe = instIds)."""
    client = client or OkxRestClient()
    symbol_map = SymbolMap(universe or [])
    tracker = GapTracker(root, silence_factor=gap_silence_factor)
    specs = [tracker.wrap(s) for s in okx_series.build_series(symbol_map)]
    return RestPresentStateCollector(
        root=root, client=client, universe=universe,
        universe_fn=(None if universe is not None
                     else _universe_fn(symbol_map, client)),
        specs=specs,
        reresolve_interval_s=cfg.UNIVERSE_RERESOLVE_INTERVAL_S,
        **kwargs,
    )


def build_okx_klines_collector(
    root: str, *,
    client: Optional[OkxRestClient] = None,
    universe: Optional[Sequence[str]] = None,
    gap_silence_factor: float = cfg.GAP_SILENCE_FACTOR,
    **kwargs: Any,
) -> RestPresentStateCollector:
    """The hourly klines maintenance collector over the OKX root."""
    client = client or OkxRestClient()
    symbol_map = SymbolMap(universe or [])
    tracker = GapTracker(root, silence_factor=gap_silence_factor)
    return RestPresentStateCollector(
        root=root, client=client, universe=universe,
        universe_fn=(None if universe is not None
                     else _universe_fn(symbol_map, client)),
        specs=[tracker.wrap(okx_series.build_klines_spec())],
        tick_s=cc_cfg.KLINES_MAINT_TICK_S,
        reresolve_interval_s=cfg.UNIVERSE_RERESOLVE_INTERVAL_S,
        **kwargs,
    )


async def collect_once_and_flush(collector: RestPresentStateCollector,
                                 now: Optional[float] = None) -> None:
    """One collection pass + flush — the ``--once`` / gate path. The caller
    supplies an explicit universe (no in-loop resolve happens here)."""
    await collector.collect_once(time.monotonic() if now is None else now)
    collector.flush_all()


def seed_klines(
    root: str, *,
    days: int = cc_cfg.KLINES_SEED_DAYS,
    client: Optional[OkxRestClient] = None,
    universe: Optional[Sequence[str]] = None,
    now_ms: Optional[int] = None,
    page_limit: int = cfg.KLINES_SEED_PAGE_LIMIT,
) -> int:
    """One-time ~``days`` backfill of closed 1h bars per instrument.

    Pages backward from ``now`` via ``after`` (strictly-older rows, newest
    first) until the horizon is covered or the venue runs out of history.
    All history bars are closed (the ``confirm`` gate in the parser is kept
    for safety). Returns rows written. ~(days*24/page_limit) requests per
    symbol, paced by the client's fixed delay.
    """
    client = client or OkxRestClient()
    universe = (list(universe) if universe is not None
                else client.fetch_okx_linear_usdt_universe())
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    writer = store.dataset_writer(root, cc_cfg.KLINES_DATASET, KLINES_1H_SCHEMA,
                                  symbol_key="s", time_key="openTime")
    written = 0
    for inst_id in universe:
        cursor = now_ms
        while True:
            data, _ = client.get_with_weight(
                "/api/v5/market/history-candles",
                {"instId": inst_id, "bar": cfg.OKX_KLINES_BAR,
                 "limit": page_limit, "after": cursor})
            if not data:
                break
            rows = parse_candles(data, inst_id, now_ms * 1_000_000)
            kept = [r for r in rows if r["openTime"] >= start_ms]
            for r in kept:
                writer.append(r)
            written += len(kept)
            oldest_open = min(int(k[0]) for k in data)
            if oldest_open <= start_ms or len(data) < page_limit:
                break
            cursor = oldest_open
    writer.flush_all()
    logger.info("capture-okx klines seed: %d instruments, %d closed bars",
                len(universe), written)
    return written
