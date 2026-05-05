"""Import existing GBP/EUR hourly bars from CSV into DuckDB."""
from __future__ import annotations

import logging

import duckdb
import pandas as pd

from fx.config import SOURCE_CSV
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.import_csv")


def import_hourly_csv(conn: duckdb.DuckDBPyConnection, csv_path: str = SOURCE_CSV) -> int:
    create_all_tables(conn)

    logger.info("Reading CSV from %s", csv_path)
    df = pd.read_csv(csv_path, parse_dates=["datetime_utc"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    logger.info("  Loaded %d rows, date range: %s to %s", len(df), df["date"].min(), df["date"].max())

    conn.execute("DROP TABLE IF EXISTS fx_prices_hourly")
    create_all_tables(conn)

    conn.execute("""
        INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc,
                                      gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close,
                                      tick_count, data_quality)
        SELECT datetime_utc, date, weekday, hour_utc,
               gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close,
               tick_count, data_quality
        FROM df
    """)

    count = conn.execute("SELECT COUNT(*) FROM fx_prices_hourly").fetchone()[0]
    logger.info("  Imported %d rows into fx_prices_hourly", count)
    return count
