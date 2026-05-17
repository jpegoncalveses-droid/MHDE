"""Build the top-N USDT-M perp universe for sentiment funding aggregation.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Sentiment
ingestion". Snapshot stored in sentiment_funding_universe; refreshed when
this is re-run (DELETE + INSERT pattern).

Mirrors crypto/ingestion/universe_builder.py for MHDE's strategy universe;
this is a separate, narrower universe used only for the sentiment funding
aggregate (does not affect MHDE's trade signals).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import duckdb

from crypto.config import STABLECOIN_EXCLUDE, WRAPPED_EXCLUDE
from crypto.ingestion.binance_client import BinanceClient
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.sentiment_funding_universe")

SENTIMENT_UNIVERSE_SIZE = 20


def build_sentiment_funding_universe(
    conn: duckdb.DuckDBPyConnection,
    *,
    client: BinanceClient | None = None,
    top_n: int = SENTIMENT_UNIVERSE_SIZE,
) -> list[str]:
    """Pick top-N USDT-M perps by 24h quote volume, snapshot to DB.

    Note: Binance's free /ticker/24hr endpoint reports trailing-24h volume,
    not 24-month. We use that as a proxy for "currently most-liquid perps"
    which is what determines today's funding rate weight. Re-snapshotting
    monthly during Phase 3 would track drift, but Week 1 uses point-in-time.
    """
    create_all_tables(conn)
    client = client or BinanceClient()

    info = client.fetch_futures_exchange_info()
    symbol_set = {s["symbol"]: s["base_asset"] for s in info}

    tickers = client.fetch_24hr_tickers()
    tickers = [t for t in tickers if t["symbol"] in symbol_set]
    tickers = [t for t in tickers if t["symbol"] not in STABLECOIN_EXCLUDE]
    tickers = [t for t in tickers if t["symbol"] not in WRAPPED_EXCLUDE]
    tickers.sort(key=lambda t: t["quote_volume"], reverse=True)
    tickers = tickers[:top_n]

    # Refresh snapshot atomically.
    conn.execute("DELETE FROM sentiment_funding_universe")
    selected: list[str] = []
    for rank, t in enumerate(tickers, 1):
        sym = t["symbol"]
        conn.execute(
            "INSERT INTO sentiment_funding_universe "
            "(symbol, rank_by_volume, quote_volume_24mo) "
            "VALUES (?, ?, ?)",
            [sym, rank, float(t["quote_volume"])],
        )
        selected.append(sym)

    logger.info("Sentiment funding universe built: %d symbols", len(selected))
    return selected


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crypto.ingestion.sentiment_funding_universe")
    parser.add_argument("--db", required=True)
    parser.add_argument("--top-n", type=int, default=SENTIMENT_UNIVERSE_SIZE)
    args = parser.parse_args(argv)
    _setup_logging()

    from storage.db import get_connection
    from storage.migrations import run_migrations

    conn = get_connection(args.db)
    run_migrations(conn)
    syms = build_sentiment_funding_universe(conn, top_n=args.top_n)
    print(f"Sentiment funding universe: {len(syms)} symbols")
    for s in syms:
        print(f"  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
