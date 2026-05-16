"""Daily volume-weighted funding rate aggregate.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Composite
sentiment score" — input to the Week 2 composite. Week 1 just computes
the per-day aggregate and persists it.

Aggregation:
  per_symbol_daily_rate(t) = SUM(funding_rate over settlements that day)
  per_symbol_daily_volume(t) = crypto_prices_daily.volume (quote volume proxy)
  daily_aggregate(t) = SUM(per_symbol_rate * per_symbol_volume) /
                      SUM(per_symbol_volume), over the sentiment universe

A symbol-day with no volume row is dropped from that day's weighted mean.
Days where no constituent has both rate and volume are omitted entirely.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import duckdb
import pandas as pd

from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.sentiment.funding_aggregate")


def compute_daily_aggregate(
    rates: pd.DataFrame, volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Pure function: combine per-symbol daily rates + volumes into a daily
    weighted aggregate.

    `rates` columns: symbol, trade_date, daily_funding_rate.
    `volumes` columns: symbol, trade_date, quote_volume.
    Returns: trade_date, volume_weighted_funding_rate, n_constituents.
    """
    merged = rates.merge(volumes, on=["symbol", "trade_date"], how="inner")
    if merged.empty:
        return pd.DataFrame(columns=[
            "trade_date", "volume_weighted_funding_rate", "n_constituents",
        ])
    merged["weighted"] = merged["daily_funding_rate"] * merged["quote_volume"]
    grouped = merged.groupby("trade_date").agg(
        weighted_sum=("weighted", "sum"),
        volume_sum=("quote_volume", "sum"),
        n_constituents=("symbol", "nunique"),
    ).reset_index()
    # Days with zero total volume → drop (would divide by zero).
    grouped = grouped[grouped["volume_sum"] > 0].copy()
    grouped["volume_weighted_funding_rate"] = (
        grouped["weighted_sum"] / grouped["volume_sum"]
    )
    return grouped[["trade_date", "volume_weighted_funding_rate", "n_constituents"]]


def persist_aggregate(
    conn: duckdb.DuckDBPyConnection, df: pd.DataFrame,
) -> int:
    """Upsert daily aggregate rows into sentiment_funding_aggregate."""
    create_all_tables(conn)
    count = 0
    for row in df.itertuples(index=False):
        conn.execute(
            """
            INSERT INTO sentiment_funding_aggregate
                (trade_date, volume_weighted_funding_rate, n_constituents)
            VALUES (?, ?, ?)
            ON CONFLICT (trade_date) DO UPDATE SET
                volume_weighted_funding_rate = excluded.volume_weighted_funding_rate,
                n_constituents = excluded.n_constituents,
                computed_at = now()
            """,
            [row.trade_date, float(row.volume_weighted_funding_rate),
             int(row.n_constituents)],
        )
        count += 1
    return count


def _load_inputs(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read per-symbol daily funding sums + per-symbol daily volumes for the
    sentiment universe.
    """
    rates = conn.execute(
        """
        SELECT
            r.symbol,
            CAST(r.funding_time AS DATE) AS trade_date,
            SUM(r.funding_rate) AS daily_funding_rate
        FROM crypto_funding_rates r
        JOIN sentiment_funding_universe u ON r.symbol = u.symbol
        GROUP BY r.symbol, CAST(r.funding_time AS DATE)
        """
    ).fetchdf()
    volumes = conn.execute(
        """
        SELECT
            p.symbol,
            p.trade_date,
            p.volume AS quote_volume
        FROM crypto_prices_daily p
        JOIN sentiment_funding_universe u ON p.symbol = u.symbol
        """
    ).fetchdf()
    # DuckDB returns trade_date as Timestamp — convert to python date for the
    # downstream compute_daily_aggregate test alignment.
    if not rates.empty:
        rates["trade_date"] = rates["trade_date"].apply(
            lambda t: t.date() if hasattr(t, "date") else t
        )
    if not volumes.empty:
        volumes["trade_date"] = volumes["trade_date"].apply(
            lambda t: t.date() if hasattr(t, "date") else t
        )
    return rates, volumes


def rebuild_aggregate(conn: duckdb.DuckDBPyConnection) -> int:
    """End-to-end: load DB inputs, compute, persist. Returns rows written."""
    create_all_tables(conn)
    rates, volumes = _load_inputs(conn)
    df = compute_daily_aggregate(rates, volumes)
    return persist_aggregate(conn, df)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crypto.sentiment.funding_aggregate")
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    _setup_logging()

    from storage.db import get_connection
    from storage.migrations import run_migrations

    conn = get_connection(args.db)
    run_migrations(conn)
    n = rebuild_aggregate(conn)
    print(f"sentiment_funding_aggregate: {n} day(s) computed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
