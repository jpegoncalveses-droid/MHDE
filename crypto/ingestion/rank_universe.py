"""Ranking buffer population for the hysteresis-based universe rebuild.

Two entry points share the same eligibility + persistence logic:

- ``rank_universe_daily`` (mhde-crypto-rank-universe-daily.timer): writes
  one set of rows for today using the live 30-day average per symbol.
- ``backfill_universe_rankings``: writes one set of rows per date in a
  range using point-in-time 30-day averages (Binance klines endTime).
  Used once to seed enough history for the first daily rebuild to
  evaluate 7-consecutive-day hysteresis rules.

Neither touches crypto_universe — that's the daily rebuild's job
(see build_universe).

Top N (default 100) gives hysteresis visibility around the rank-50 cutoff:
ranks 1-50 get in_top_50=True, 51-100 get in_top_50=False.

Transactional safety: Binance fetches happen BEFORE any DB write per date,
so an API failure leaves the buffer for that date untouched. The DB write
itself is a single BEGIN/DELETE/INSERT*/COMMIT block per date.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import duckdb

from crypto.config import STABLECOIN_EXCLUDE, WRAPPED_EXCLUDE
from crypto.ingestion.binance_client import BinanceClient
from crypto.ingestion.universe_builder import _is_safe_symbol
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.universe")


def _collect_candidates(client: BinanceClient, end_date: date | None) -> list[tuple[str, str, float]]:
    """For every eligible USDT perp, return (symbol, base, avg_quote_volume).

    If ``end_date`` is None, uses the live 30-day window (today). Otherwise
    uses a point-in-time window ending on ``end_date``. Sorted descending
    by volume.
    """
    perp_symbols = client.fetch_futures_exchange_info()
    candidates: list[tuple[str, str, float]] = []
    for s in perp_symbols:
        sym = s["symbol"]
        base = s["base_asset"]
        if sym in STABLECOIN_EXCLUDE or sym in WRAPPED_EXCLUDE:
            continue
        if not _is_safe_symbol(sym):
            logger.warning(
                "rank_universe: skipping symbol %r (rejected by safe-symbol guard)",
                sym,
            )
            continue
        if end_date is None:
            avg_qv = client.fetch_30d_avg_quote_volume(sym)
        else:
            avg_qv = client.fetch_30d_avg_quote_volume_at(sym, end_date)
        if avg_qv is None:
            continue
        candidates.append((sym, base, avg_qv))
    candidates.sort(key=lambda c: c[2], reverse=True)
    return candidates


def _persist_ranking(
    conn: duckdb.DuckDBPyConnection,
    candidates: list[tuple[str, str, float]],
    ranking_date: date,
    top_n: int,
    in_top_50_cutoff: int,
) -> int:
    """Atomic-replace the rows for ``ranking_date`` with the top-N candidates."""
    top = candidates[:top_n]
    conn.execute("BEGIN")
    try:
        conn.execute(
            "DELETE FROM crypto_universe_ranking_buffer WHERE ranking_date = ?",
            [ranking_date],
        )
        for rank, (sym, _base, avg_qv) in enumerate(top, 1):
            conn.execute(
                """
                INSERT INTO crypto_universe_ranking_buffer
                    (symbol, ranking_date, avg_daily_volume_30d,
                     rank_by_volume, in_top_50)
                VALUES (?, ?, ?, ?, ?)
                """,
                [sym, ranking_date, avg_qv, rank, rank <= in_top_50_cutoff],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(top)


def rank_universe_daily(
    conn: duckdb.DuckDBPyConnection,
    ranking_date: date | None = None,
    top_n: int = 100,
    in_top_50_cutoff: int = 50,
) -> int:
    """Compute 30-day avg quote volume for all eligible perps; persist top-N
    for ``ranking_date`` (default: today UTC). Returns rows written.
    """
    create_all_tables(conn)
    if ranking_date is None:
        ranking_date = datetime.now(tz=timezone.utc).date()

    client = BinanceClient()
    candidates = _collect_candidates(client, end_date=None)
    n = _persist_ranking(conn, candidates, ranking_date, top_n, in_top_50_cutoff)
    logger.info(
        "rank_universe_daily: wrote %d rows for %s (top_n=%d, in_top_50_cutoff=%d)",
        n, ranking_date, top_n, in_top_50_cutoff,
    )
    return n


def backfill_universe_rankings(
    conn: duckdb.DuckDBPyConnection,
    start_date: date,
    end_date: date | None = None,
    top_n: int = 100,
    in_top_50_cutoff: int = 50,
) -> dict[date, int]:
    """Backfill the ranking buffer for every date in [start_date, end_date].

    Each date uses a point-in-time 30-day window ending on that date — so the
    historical ranking reflects what the universe would have looked like if
    we'd been ranking daily. Per-date atomic replace; per-date failure logged
    and skipped (returned dict has -1 for failed dates).
    """
    create_all_tables(conn)
    if end_date is None:
        end_date = datetime.now(tz=timezone.utc).date()
    if start_date > end_date:
        raise ValueError(f"start_date {start_date} > end_date {end_date}")

    client = BinanceClient()
    results: dict[date, int] = {}
    current = start_date
    while current <= end_date:
        t0 = time.time()
        try:
            candidates = _collect_candidates(client, end_date=current)
            n = _persist_ranking(conn, candidates, current, top_n, in_top_50_cutoff)
            elapsed = time.time() - t0
            top1_sym = candidates[0][0] if candidates else "—"
            top1_qv = candidates[0][2] if candidates else 0.0
            logger.info(
                "backfill %s: %d symbols ranked, top-1=%s (%.2fM), persisted in %.1fs",
                current, n, top1_sym, top1_qv / 1e6, elapsed,
            )
            results[current] = n
        except Exception as e:
            logger.error("backfill %s failed: %s", current, e)
            results[current] = -1
        current += timedelta(days=1)
    return results
