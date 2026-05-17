"""Backfill Fear & Greed Index history from alternative.me.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Week 1".
Idempotent: re-running upserts existing rows (later values win, which
covers the case where alternative.me issues late corrections).
"""
from __future__ import annotations

import argparse
import logging
import sys

import duckdb

from crypto.ingestion.fear_greed_client import FearGreedClient
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.backfill_fear_greed")


def backfill_fear_greed(
    conn: duckdb.DuckDBPyConnection,
    *,
    client: FearGreedClient | None = None,
    limit: int = 0,
) -> int:
    """Fetch F&G history and upsert into sentiment_fear_greed.

    `limit=0` requests full available history from alternative.me (~9 years).
    Returns rows upserted.
    """
    create_all_tables(conn)
    client = client or FearGreedClient()

    rows = client.fetch_history(limit=limit)
    if not rows:
        logger.warning("Empty F&G response.")
        return 0

    for row in rows:
        conn.execute(
            """
            INSERT INTO sentiment_fear_greed
                (date, value, value_classification, source)
            VALUES (?, ?, ?, 'alternative.me')
            ON CONFLICT (date) DO UPDATE SET
                value = excluded.value,
                value_classification = excluded.value_classification,
                source = excluded.source,
                ingested_at = now()
            """,
            [row["date"], row["value"], row["value_classification"]],
        )

    logger.info("Backfilled F&G: %d rows upserted", len(rows))
    return len(rows)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crypto.ingestion.backfill_fear_greed")
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=0,
                        help="0 = full history (default), N = last N days.")
    args = parser.parse_args(argv)
    _setup_logging()

    from storage.db import get_connection
    from storage.migrations import run_migrations

    conn = get_connection(args.db)
    run_migrations(conn)
    n = backfill_fear_greed(conn, limit=args.limit)
    print(f"F&G backfilled: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
