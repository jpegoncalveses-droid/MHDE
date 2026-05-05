"""Backfill daily OHLCV candles from Binance futures."""
from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

from crypto.ingestion.binance_client import BinanceClient
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.backfill_ohlcv")


def backfill_ohlcv(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None,
                   start_date: date | None = None, end_date: date | None = None) -> int:
    create_all_tables(conn)
    client = BinanceClient()

    if symbols is None:
        symbols = [r[0] for r in conn.execute(
            "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY rank_by_volume"
        ).fetchall()]

    if not symbols:
        logger.warning("No symbols in universe. Run build_universe first.")
        return 0

    if start_date is None:
        start_date = date.today() - timedelta(days=365 * 2 + 30)
    if end_date is None:
        end_date = date.today()

    total = 0
    for i, sym in enumerate(symbols, 1):
        logger.info("  [%d/%d] %s", i, len(symbols), sym)

        existing_max = conn.execute(
            "SELECT MAX(trade_date) FROM crypto_prices_daily WHERE symbol = ?", [sym]
        ).fetchone()[0]
        fetch_start = existing_max + timedelta(days=1) if existing_max else start_date

        if fetch_start > end_date:
            logger.info("    Already up to date")
            continue

        try:
            rows = client.fetch_daily_klines(sym, start_date=fetch_start, end_date=end_date, futures=True)
        except Exception as e:
            logger.error("    Failed to fetch %s: %s", sym, e)
            continue

        if not rows:
            logger.warning("    No data returned")
            continue

        for row in rows:
            conn.execute("""
                INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, close, volume, trades, taker_buy_volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, trade_date) DO NOTHING
            """, [sym, row["trade_date"], row["open"], row["high"], row["low"],
                  row["close"], row["volume"], row["trades"], row["taker_buy_volume"]])

        total += len(rows)
        logger.info("    Inserted %d rows (total so far: %d)", len(rows), total)

    return total
