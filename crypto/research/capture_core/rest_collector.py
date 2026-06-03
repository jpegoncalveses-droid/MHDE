"""Budget-aware REST present-state collector for capture-core.

Drives the :mod:`rest_series` registry on a self-pacing schedule:

  * **/fapi pool** (open_interest, premium_index) — weight-counted; paced off the
    live ``X-MBX-USED-WEIGHT-1M`` header, staying under a fraction of the limit so
    it COEXISTS with the depth ``SnapshotScheduler`` (which reads the same signal
    via its own calls). open_interest's cadence is budget-driven, not pre-coarsened.
  * **/futures/data pool** (ratios, basis) — a SEPARATE pool with NO weight header;
    paced by a fixed conservative interval and, on 429, DEGRADED by tier (LOW
    then MED). HIGH is never starved.

Each series writes its own parquet dataset (raw rows only — changes/zscores are
derived downstream). NEVER opens mhde.duckdb or the engine DB; public REST only.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any, Callable, Optional, Sequence

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import store
from crypto.research.capture_core.client import RateLimited
from crypto.research.capture_core.rest_series import SERIES, SeriesSpec

logger = logging.getLogger("mhde.crypto.capture_core.rest_collector")

_RANK = {"HIGH": 0, "MED": 1, "LOW": 2}


# -- pure scheduling helpers --

def due_series(specs: Sequence[SeriesSpec], now: float,
               last_run: dict[str, float]) -> list[SeriesSpec]:
    """Series whose target cadence has elapsed since their last run."""
    return [s for s in specs
            if now - last_run.get(s.name, float("-inf")) >= s.target_cadence_s]


def select_under_pressure(due: Sequence[SeriesSpec], *, used_weight: Optional[int],
                          limit: int, fraction: float) -> list[SeriesSpec]:
    """Under /fapi budget pressure, drop fapi non-HIGH series; never drop HIGH or
    any /futures/data series (separate pool, unaffected by the /fapi counter)."""
    if used_weight is None or used_weight < fraction * limit:
        return list(due)
    return [s for s in due if s.pool != "fapi" or s.priority == "HIGH"]


class RestPresentStateCollector:
    def __init__(
        self,
        *,
        root: str,
        client: Any,
        universe: Optional[Sequence[str]] = None,
        universe_fn: Optional[Callable[[], list[str]]] = None,
        specs: Sequence[SeriesSpec] = SERIES,
        budget_fraction: float = cfg.REST_BUDGET_FRACTION,
        weight_limit: int = cfg.FAPI_WEIGHT_LIMIT,
        futures_data_min_interval_s: float = cfg.FUTURES_DATA_MIN_INTERVAL_S,
        budget_backoff_s: float = cfg.REST_BUDGET_BACKOFF_S,
        degrade_cooldown_s: float = cfg.REST_DEGRADE_COOLDOWN_S,
        reresolve_interval_s: float = cfg.UNIVERSE_RERESOLVE_INTERVAL_S,
        tick_s: float = 1.0,
        flush_interval_s: float = cfg.FLUSH_INTERVAL_S,
        flush_max_bytes: int = cfg.FLUSH_MAX_BYTES,
        now_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        clock_ns: Callable[[], int] = time.time_ns,
        install_signals: bool = True,
    ) -> None:
        self._client = client
        self._universe = list(universe or [])
        self._universe_fn = universe_fn
        self._specs = list(specs)
        self._fraction = budget_fraction
        self._limit = weight_limit
        self._fd_interval = futures_data_min_interval_s
        self._backoff_s = budget_backoff_s
        self._degrade_cooldown = degrade_cooldown_s
        self._reresolve_s = reresolve_interval_s
        self._tick_s = tick_s
        self._now = now_fn
        self._sleep = sleep_fn
        self._clock_ns = clock_ns
        self._install_signals = install_signals

        self._writers = {
            s.name: store.dataset_writer(root, s.name, s.schema,
                                         symbol_key=s.symbol_key, time_key=s.time_key,
                                         flush_interval_s=flush_interval_s,
                                         flush_max_bytes=flush_max_bytes)
            for s in self._specs
        }
        self._last_run: dict[str, float] = {}
        self._used_weight = 0
        self._degraded_until: dict[str, float] = {}
        self._stop = asyncio.Event()

    # -- pacing / degradation --

    def _is_degraded(self, spec: SeriesSpec, now: float) -> bool:
        return spec.pool == "futures_data" and now < self._degraded_until.get(spec.priority, 0.0)

    def _degrade(self, spec: SeriesSpec, now: float) -> None:
        # futures_data 429 -> suppress the lowest active tier first (LOW then MED).
        # Deliberately decoupled from which series hit the limit: a 429 anywhere in
        # the shared /futures/data pool sheds the cheapest-to-lose tier first.
        if spec.pool != "futures_data":
            return
        if now >= self._degraded_until.get("LOW", 0.0):
            self._degraded_until["LOW"] = now + self._degrade_cooldown
        else:
            self._degraded_until["MED"] = now + self._degrade_cooldown
        logger.warning("capture-core REST degraded (%s 429); suppressing a tier", spec.name)

    async def _pace(self, spec: SeriesSpec) -> None:
        if spec.pool == "fapi":
            if self._used_weight >= self._fraction * self._limit:
                await self._sleep(self._backoff_s)
        else:
            await self._sleep(self._fd_interval)

    async def _get(self, path: str, params: dict) -> Any:
        data, used = await asyncio.to_thread(self._client.get_with_weight, path, params)
        if used is not None:
            self._used_weight = used
        return data

    # -- one pass over due series --

    async def collect_once(self, now: float) -> None:
        due = due_series(self._specs, now, self._last_run)
        due = select_under_pressure(due, used_weight=self._used_weight,
                                    limit=self._limit, fraction=self._fraction)
        due = [s for s in due if not self._is_degraded(s, now)]
        for spec in sorted(due, key=lambda s: _RANK[s.priority]):
            if self._stop.is_set():
                break
            await self._run_series(spec, now)

    async def _run_series(self, spec: SeriesSpec, now: float) -> None:
        writer = self._writers[spec.name]
        try:
            if spec.scope == "all":
                path, params = spec.request(None)
                await self._pace(spec)
                data = await self._get(path, params)
                for row in spec.parse(data, None, self._clock_ns()):
                    writer.append(row)
            else:
                for key in self._universe:
                    if self._stop.is_set():
                        break
                    await self._pace(spec)
                    path, params = spec.request(key)
                    data = await self._get(path, params)
                    for row in spec.parse(data, key, self._clock_ns()):
                        writer.append(row)
        except RateLimited:
            self._degrade(spec, now)
        except Exception as exc:  # noqa: BLE001 - isolate a series; try again next cadence
            logger.warning("capture-core REST series %s failed (%s: %s)",
                           spec.name, type(exc).__name__, exc)
        # Mark run regardless so a failing series waits its full cadence, not hammered.
        self._last_run[spec.name] = now

    # -- lifecycle --

    def stop(self) -> None:
        self._stop.set()

    def flush_all(self) -> None:
        for w in self._writers.values():
            w.flush_all()

    def _flush_due(self) -> None:
        for w in self._writers.values():
            w.flush_due()

    async def _wait_stop_or_timeout(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=timeout)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    async def run(self) -> None:
        if self._install_signals:
            self._install_signal_handlers()
        if self._universe_fn is not None:
            self._universe = await asyncio.to_thread(self._universe_fn)
        last_resolve = self._now()
        logger.info("capture-core REST present-state collector: %d symbols, %d series",
                    len(self._universe), len(self._specs))
        try:
            while not self._stop.is_set():
                await self.collect_once(self._now())
                self._flush_due()
                if (self._universe_fn is not None
                        and self._now() - last_resolve >= self._reresolve_s):
                    self._universe = await asyncio.to_thread(self._universe_fn)
                    last_resolve = self._now()
                await self._wait_stop_or_timeout(self._tick_s)
        finally:
            self._stop.set()
            self.flush_all()
            logger.info("capture-core REST collector stopped")
