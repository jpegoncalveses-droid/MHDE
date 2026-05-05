"""Build crypto universe from Binance perpetual futures by trading volume."""
from __future__ import annotations

import logging
from datetime import date

import duckdb

from crypto.config import STABLECOIN_EXCLUDE, WRAPPED_EXCLUDE, UNIVERSE_SIZE
from crypto.ingestion.binance_client import BinanceClient
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.universe")


def build_universe(conn: duckdb.DuckDBPyConnection, top_n: int = UNIVERSE_SIZE) -> list[str]:
    create_all_tables(conn)
    client = BinanceClient()

    perp_symbols = client.fetch_futures_exchange_info()
    symbol_set = {s["symbol"]: s["base_asset"] for s in perp_symbols}

    tickers = client.fetch_24hr_tickers()
    tickers = [t for t in tickers if t["symbol"] in symbol_set]
    tickers = [t for t in tickers if t["symbol"] not in STABLECOIN_EXCLUDE]
    tickers = [t for t in tickers if t["symbol"] not in WRAPPED_EXCLUDE]
    tickers.sort(key=lambda t: t["quote_volume"], reverse=True)
    tickers = tickers[:top_n]

    conn.execute("DELETE FROM crypto_universe")

    today = date.today()
    selected = []
    for rank, t in enumerate(tickers, 1):
        sym = t["symbol"]
        base = symbol_set[sym]
        conn.execute("""
            INSERT INTO crypto_universe (symbol, base_asset, avg_daily_volume_30d, rank_by_volume, is_active, added_date)
            VALUES (?, ?, ?, ?, true, ?)
            ON CONFLICT (symbol) DO UPDATE SET
                avg_daily_volume_30d = excluded.avg_daily_volume_30d,
                rank_by_volume = excluded.rank_by_volume,
                is_active = true
        """, [sym, base, t["quote_volume"], rank, today])
        selected.append(sym)

    logger.info("Universe built: %d coins selected", len(selected))
    return selected
