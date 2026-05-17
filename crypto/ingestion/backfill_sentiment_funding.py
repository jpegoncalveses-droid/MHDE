"""Backfill funding rates for the sentiment funding universe.

Thin orchestration: reads symbol list from sentiment_funding_universe,
delegates per-symbol fetch + write to existing
crypto/ingestion/backfill_funding.py (idempotent). Funding rates land
in the existing crypto_funding_rates table.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Week 1".
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

import duckdb

from crypto.ingestion.backfill_funding import backfill_funding
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.backfill_sentiment_funding")


def sentiment_universe_symbols(conn: duckdb.DuckDBPyConnection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT symbol FROM sentiment_funding_universe ORDER BY rank_by_volume"
        ).fetchall()
    ]


def backfill_sentiment_funding(
    conn: duckdb.DuckDBPyConnection,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    create_all_tables(conn)
    symbols = sentiment_universe_symbols(conn)
    if not symbols:
        logger.warning(
            "sentiment_funding_universe empty — run "
            "crypto.ingestion.sentiment_funding_universe first."
        )
        return 0

    if start_date is None:
        # 24-month lookback per Phase 3 amendment §"Sentiment ingestion".
        start_date = date.today() - timedelta(days=24 * 30)
    if end_date is None:
        end_date = date.today()

    logger.info("Backfilling funding for %d sentiment universe symbols, "
                "[%s .. %s]", len(symbols), start_date, end_date)
    n = backfill_funding(conn, symbols=symbols,
                         start_date=start_date, end_date=end_date)
    logger.info("Backfilled %d funding records across %d symbols",
                n, len(symbols))
    return n


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m crypto.ingestion.backfill_sentiment_funding"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--months", type=int, default=24)
    args = parser.parse_args(argv)
    _setup_logging()

    from storage.db import get_connection
    from storage.migrations import run_migrations

    conn = get_connection(args.db)
    run_migrations(conn)
    start = date.today() - timedelta(days=args.months * 30)
    end = date.today()
    n = backfill_sentiment_funding(conn, start_date=start, end_date=end)
    print(f"Backfilled {n} sentiment funding records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
