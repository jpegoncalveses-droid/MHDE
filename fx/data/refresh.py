"""Refresh GBP/EUR hourly bars from Dukascopy via ATSRP refresher.

Calls /home/jpcg/ATSRP/research/gbpeur_personal_fx/refresh_gbpeur_1h.py
to append the latest completed 1H bar to gbpeur_1h.csv, then upserts any
bars in the CSV that are newer than what is already in fx_prices_hourly.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import duckdb
import pandas as pd

from fx.config import SOURCE_CSV
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.refresh")

ATSRP_PYTHON = "/home/jpcg/ATSRP/.venv/bin/python"
ATSRP_REFRESH_SCRIPT = "/home/jpcg/ATSRP/research/gbpeur_personal_fx/refresh_gbpeur_1h.py"


def fetch_latest_bar() -> dict:
    """Run the ATSRP refresh script. Returns its parsed JSON result.

    Status codes from refresh_gbpeur_1h.py:
      OK      — new bar fetched and CSV updated
      CLOSED  — target hour outside FX trading hours (not an error)
      NO_DATA — Dukascopy 404 (data not yet published)
      ERROR   — network or parse failure
    """
    proc = subprocess.run(
        [ATSRP_PYTHON, ATSRP_REFRESH_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode not in (0, 1):
        logger.error("ATSRP refresh exited %d: %s", proc.returncode, proc.stderr)
        return {"status": "ERROR", "error": f"exit {proc.returncode}: {proc.stderr.strip()}"}

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.error("ATSRP refresh returned non-JSON: %s", proc.stdout[:200])
        return {"status": "ERROR", "error": f"JSON decode failed: {exc}"}


def upsert_new_bars(conn: duckdb.DuckDBPyConnection, csv_path: str = SOURCE_CSV) -> int:
    """Insert any CSV rows newer than the max datetime in fx_prices_hourly.

    Returns the count of newly inserted rows.
    """
    create_all_tables(conn)

    max_dt = conn.execute(
        "SELECT MAX(datetime_utc) FROM fx_prices_hourly"
    ).fetchone()[0]

    df = pd.read_csv(Path(csv_path), parse_dates=["datetime_utc"])
    df["date"] = pd.to_datetime(df["date"]).dt.date

    if max_dt is not None:
        new_rows = df[df["datetime_utc"] > pd.Timestamp(max_dt)]
    else:
        new_rows = df

    if new_rows.empty:
        logger.info("No new bars to insert (max in DB: %s)", max_dt)
        return 0

    conn.register("new_rows", new_rows)
    conn.execute("""
        INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc,
                                      gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close,
                                      tick_count, data_quality)
        SELECT datetime_utc, date, weekday, hour_utc,
               gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close,
               tick_count, data_quality
        FROM new_rows
        WHERE datetime_utc NOT IN (SELECT datetime_utc FROM fx_prices_hourly)
    """)
    conn.unregister("new_rows")

    inserted = len(new_rows)
    logger.info("Upserted %d new bar(s); latest now: %s",
                inserted, new_rows["datetime_utc"].max())
    return inserted


def refresh_prices(conn: duckdb.DuckDBPyConnection) -> dict:
    """Full refresh: fetch latest bar from Dukascopy then upsert into DB."""
    fetch_result = fetch_latest_bar()
    logger.info("ATSRP fetch: status=%s, bar=%s",
                fetch_result.get("status"), fetch_result.get("fetched_hour"))

    inserted = upsert_new_bars(conn)
    return {
        "fetch_status": fetch_result.get("status"),
        "fetched_hour": fetch_result.get("fetched_hour"),
        "fetch_error": fetch_result.get("error"),
        "rows_inserted": inserted,
    }
