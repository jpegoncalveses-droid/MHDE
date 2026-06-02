"""Signal-probe collector: one 60s cycle.

Per cycle (driven by a systemd ``--user`` timer, see
``systemd/mhde-signal-probe-collector.*``):

  1. Fix ``cycle_close`` = the latest fully-closed 1-minute boundary (exchange
     time). Every feature is computed **causally** from bars that closed at or
     before ``cycle_close``; the in-progress minute is dropped.
  2. Fetch the universe-wide ``premiumIndex`` once (funding for all symbols).
  3. Per symbol: pull the 1m + 1h kline lookback, OI live + 5m history, and
     optional depth; compute every raw feature.
  4. Add the cross-sectional features (vs BTC / vs universe).
  5. UPSERT one row per symbol for ``ts = cycle_close`` into the research DB.

A symbol whose fetch/compute fails — or whose latest bar is stale — is logged
and skipped; the cycle still writes the symbols that succeeded. A missed cycle
is simply skipped (no backfill): a live model acts on *now*.

To refresh the probe universe, re-run::

    SELECT symbol FROM crypto_universe WHERE is_active = TRUE ORDER BY symbol

read-only against ``mhde.duckdb`` and update ``UNIVERSE`` in ``config.py``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from crypto.research.signal_probe import config as cfg
from crypto.research.signal_probe.features import (
    apply_cross_sectional, compute_base_features,
)
from crypto.research.signal_probe.store import upsert_rows

logger = logging.getLogger("mhde.crypto.signal_probe.collector")

#: A symbol's latest closed bar may lag ``cycle_close`` by at most this much
#: before we treat it as stale and skip it (avoids stamping stale features
#: under a fresh ts).
_MAX_BAR_LAG = timedelta(minutes=2)


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _closed(bars: Sequence[dict], boundary: datetime) -> list[dict]:
    """Bars whose ``open_time`` is strictly before ``boundary`` (i.e. closed)."""
    return [b for b in bars if b["open_time"] < boundary]


def _collect_symbol(
    client: Any, symbol: str, *, cycle_close: datetime, hour_floor: datetime,
    funding: Optional[dict], include_depth: bool,
) -> Optional[dict[str, Any]]:
    """Fetch + compute one symbol's base features, or ``None`` to skip."""
    bars_1m = _closed(client.fetch_klines(symbol, "1m", cfg.LOOKBACK_1M), cycle_close)
    if not bars_1m:
        logger.warning("signal-probe: %s has no closed 1m bars; skipping", symbol)
        return None
    if bars_1m[-1]["open_time"] < cycle_close - _MAX_BAR_LAG:
        logger.warning("signal-probe: %s latest bar %s lags cycle %s; skipping",
                       symbol, bars_1m[-1]["open_time"], cycle_close)
        return None

    bars_1h = _closed(client.fetch_klines(symbol, "1h", cfg.LOOKBACK_1H), hour_floor)
    # openInterestHist returns completed-period snapshots — its last point is
    # the most recent *closed* 5m period, never an in-progress one — so the OI
    # series is causal as returned (no in-progress-bar trim needed, unlike the
    # kline path above).
    oi_series = client.fetch_open_interest_hist(symbol, cfg.OI_HIST_PERIOD, cfg.OI_HIST_LIMIT)
    oi_live = client.fetch_open_interest(symbol)
    depth = client.fetch_depth(symbol, cfg.DEPTH_LIMIT) if include_depth else None

    return compute_base_features(
        bars_1m, bars_1h, ts=cycle_close, funding=funding,
        oi_series=oi_series, oi_live=oi_live, depth=depth,
    )


def run_cycle(
    client: Any,
    conn: Any,
    *,
    symbols: Sequence[str] = tuple(cfg.UNIVERSE),
    btc_symbol: str = cfg.BTC_SYMBOL,
    include_depth: bool = cfg.INCLUDE_DEPTH,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run one collection cycle and UPSERT the resulting rows.

    Returns a summary: ``ts``, ``rows_written``, ``symbols_ok``,
    ``symbols_skipped`` (list).
    """
    now = now or datetime.now(tz=timezone.utc)
    cycle_close = _floor_minute(now)
    hour_floor = _floor_hour(now)

    try:
        funding_all = client.fetch_premium_index_all()
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, no funding
        logger.warning("signal-probe: premiumIndex fetch failed (%s: %s); "
                       "funding columns will be NULL this cycle",
                       type(exc).__name__, exc)
        funding_all = {}

    base_by_symbol: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for symbol in symbols:
        try:
            feats = _collect_symbol(
                client, symbol, cycle_close=cycle_close, hour_floor=hour_floor,
                funding=funding_all.get(symbol), include_depth=include_depth,
            )
        except Exception as exc:  # noqa: BLE001 - per-symbol isolation
            logger.warning("signal-probe: skipping %s (%s: %s)",
                           symbol, type(exc).__name__, exc)
            skipped.append(symbol)
            continue
        if feats is None:
            skipped.append(symbol)
            continue
        base_by_symbol[symbol] = feats

    apply_cross_sectional(base_by_symbol, btc_symbol)

    ts = cycle_close.replace(tzinfo=None)
    collected_at = now.replace(tzinfo=None)
    rows = []
    for symbol, feats in base_by_symbol.items():
        row = dict(feats)
        row["ts"] = ts
        row["symbol"] = symbol
        row["collected_at"] = collected_at
        rows.append(row)

    written = upsert_rows(conn, rows)
    summary = {
        "ts": ts,
        "rows_written": written,
        "symbols_ok": len(rows),
        "symbols_skipped": skipped,
    }
    logger.info("signal-probe cycle %s: %d rows, %d ok, %d skipped",
                ts, written, len(rows), len(skipped))
    return summary
