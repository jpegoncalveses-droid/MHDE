"""Fetch macro indicators from FRED API for FX features."""
from __future__ import annotations

import logging
import os

import duckdb
import requests

from fx.config import FRED_SERIES
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.macro")

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


def _get_api_key() -> str | None:
    from dotenv import load_dotenv
    load_dotenv()
    return os.environ.get("FRED_API_KEY")


def fetch_fred_series(series_id: str, api_key: str,
                      start_date: str = "2014-01-01") -> list[dict]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
    }
    resp = requests.get(FRED_BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    observations = resp.json().get("observations", [])

    rows = []
    for obs in observations:
        if obs["value"] == ".":
            continue
        rows.append({
            "observation_date": obs["date"],
            "value": float(obs["value"]),
        })
    return rows


def backfill_macro(conn: duckdb.DuckDBPyConnection) -> int:
    create_all_tables(conn)
    api_key = _get_api_key()
    if not api_key:
        logger.error("FRED_API_KEY not set. Add to .env or export it.")
        return 0

    total = 0
    for indicator_name, series_id in FRED_SERIES.items():
        logger.info("  Fetching %s (%s)...", indicator_name, series_id)
        try:
            rows = fetch_fred_series(series_id, api_key)
        except Exception as e:
            logger.error("    Failed: %s", e)
            continue

        if not rows:
            logger.warning("    No data for %s", indicator_name)
            continue

        for row in rows:
            conn.execute("""
                INSERT INTO fx_macro (indicator, observation_date, value)
                VALUES (?, ?, ?)
                ON CONFLICT (indicator, observation_date) DO UPDATE SET value = excluded.value
            """, [indicator_name, row["observation_date"], row["value"]])

        total += len(rows)
        logger.info("    %d observations", len(rows))

    logger.info("Total macro observations: %d", total)
    return total
