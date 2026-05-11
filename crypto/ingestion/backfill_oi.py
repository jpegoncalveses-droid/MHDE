"""Backfill open interest history from Binance futures."""
from __future__ import annotations

import logging

import duckdb

from crypto.ingestion.binance_client import BinanceClient
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.backfill_oi")


def backfill_open_interest(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None) -> int:
    create_all_tables(conn)
    client = BinanceClient()

    if symbols is None:
        symbols = [r[0] for r in conn.execute(
            "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY rank_by_volume"
        ).fetchall()]

    if not symbols:
        logger.warning("No symbols in universe.")
        return 0

    total = 0
    for i, sym in enumerate(symbols, 1):
        logger.info("  [%d/%d] %s", i, len(symbols), sym)

        try:
            rows = client.fetch_open_interest_hist(sym, period="1d", limit=30)
        except Exception as e:
            logger.error("    No OI data for %s: %s", sym, e)
            continue

        if not rows:
            logger.warning("    No OI data (may not be available)")
            continue

        for row in rows:
            # UPSERT (was DO NOTHING). ``openInterestHist`` already re-fetches a
            # rolling 30-day window every run, but the most-recent point is the
            # in-progress day; without the UPSERT that partial snapshot would be
            # frozen the same way OHLCV candles were.
            conn.execute("""
                INSERT INTO crypto_open_interest (symbol, trade_date, open_interest, open_interest_value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (symbol, trade_date) DO UPDATE SET
                    open_interest = excluded.open_interest,
                    open_interest_value = excluded.open_interest_value
            """, [row["symbol"], row["trade_date"], row["open_interest"], row["open_interest_value"]])

        total += len(rows)
        logger.info("    Inserted %d OI records", len(rows))

    return total
