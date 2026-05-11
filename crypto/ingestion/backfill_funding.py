"""Backfill funding rate history from Binance futures."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import duckdb

from crypto.ingestion.binance_client import BinanceClient
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.backfill_funding")


def backfill_funding(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None,
                     start_date: date | None = None, end_date: date | None = None) -> int:
    create_all_tables(conn)
    client = BinanceClient()

    if symbols is None:
        symbols = [r[0] for r in conn.execute(
            "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY rank_by_volume"
        ).fetchall()]

    if not symbols:
        logger.warning("No symbols in universe.")
        return 0

    if start_date is None:
        start_date = date.today() - timedelta(days=365 + 30)
    if end_date is None:
        end_date = date.today()

    total = 0
    for i, sym in enumerate(symbols, 1):
        logger.info("  [%d/%d] %s", i, len(symbols), sym)

        existing_max = conn.execute(
            "SELECT MAX(funding_time)::DATE FROM crypto_funding_rates WHERE symbol = ?", [sym]
        ).fetchone()[0]

        fetch_start = existing_max if existing_max else start_date

        try:
            rows = client.fetch_funding_rates(sym, start_date=fetch_start, end_date=end_date)
        except Exception as e:
            logger.error("    Failed to fetch %s: %s", sym, e)
            continue

        if not rows:
            logger.warning("    No funding data")
            continue

        for row in rows:
            # UPSERT (was DO NOTHING) so a re-fetched/late-corrected settlement
            # overwrites in place. Funding events are final once published, so
            # unlike OHLCV there is no in-progress "partial" row to guard against
            # and ``end_date`` can stay at ``date.today()``.
            conn.execute("""
                INSERT INTO crypto_funding_rates (symbol, funding_time, funding_rate, mark_price)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (symbol, funding_time) DO UPDATE SET
                    funding_rate = excluded.funding_rate,
                    mark_price = excluded.mark_price
            """, [row["symbol"], row["funding_time"], row["funding_rate"], row["mark_price"]])

        total += len(rows)
        logger.info("    Inserted %d funding records", len(rows))

    return total
