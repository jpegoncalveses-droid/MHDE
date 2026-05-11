"""Backfill daily OHLCV candles from Binance futures.

Two safeguards keep the daily 00:30-UTC timer from polluting the table with
partial candles (see ``crypto.config.INGESTION_LAG_DAYS`` / ``REFETCH_WINDOW_DAYS``
and the 2026-05-05/07 SKYAIUSDT incident):

* ``end_date`` defaults to ``date.today() - INGESTION_LAG_DAYS`` — we never
  request a kline for the in-progress UTC day, which at 00:30 would be only a
  ~30-minute partial candle.
* the per-symbol fetch starts ``REFETCH_WINDOW_DAYS - 1`` days *before* the last
  stored date and the INSERT is an UPSERT, so the trailing window is re-fetched
  and overwritten every run — any stale/partial/late-corrected row self-heals.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

from crypto.config import INGESTION_LAG_DAYS, REFETCH_WINDOW_DAYS
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
        # Only fully-closed UTC days — never the in-progress day.
        end_date = date.today() - timedelta(days=INGESTION_LAG_DAYS)

    total = 0
    for i, sym in enumerate(symbols, 1):
        logger.info("  [%d/%d] %s", i, len(symbols), sym)

        existing_max = conn.execute(
            "SELECT MAX(trade_date) FROM crypto_prices_daily WHERE symbol = ?", [sym]
        ).fetchone()[0]
        if existing_max:
            # Re-fetch a trailing window so a previously-written partial candle
            # (or a late venue correction) gets overwritten by the UPSERT below.
            fetch_start = existing_max - timedelta(days=REFETCH_WINDOW_DAYS - 1)
        else:
            fetch_start = start_date

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
                ON CONFLICT (symbol, trade_date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    trades = excluded.trades,
                    taker_buy_volume = excluded.taker_buy_volume
            """, [sym, row["trade_date"], row["open"], row["high"], row["low"],
                  row["close"], row["volume"], row["trades"], row["taker_buy_volume"]])

        total += len(rows)
        logger.info("    Inserted %d rows (total so far: %d)", len(rows), total)

    return total
