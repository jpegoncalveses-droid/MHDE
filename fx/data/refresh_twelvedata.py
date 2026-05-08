"""Refresh GBP/EUR hourly bars from TwelveData REST API.

Parallel fetcher to fx/data/refresh.py (Dukascopy via ATSRP). Used
during the FX data-source migration (Sessions 1-2 of the migration
plan); writes to a mirror table fx_prices_hourly_twelvedata, NOT to
the production fx_prices_hourly. Predict / features / labels /
freshness all continue to read fx_prices_hourly.

Public surface mirrors fx/data/refresh.py exactly so the systemd
ExecStart line and downstream log readers can be cut over with a
one-import change in Session 2.

Requires the TWELVEDATA_API_KEY environment variable. The key is
also surfaced via storage.config.load_engine_config() under the key
"twelvedata_api_key".
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb
import requests

from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.refresh_twelvedata")

API_URL = "https://api.twelvedata.com/time_series"
SYMBOL = "GBP/EUR"
INTERVAL = "1h"
HTTP_TIMEOUT_S = 15

# FX market is closed Sat 21:00 → Sun 21:00 UTC. Pre-validation showed
# TwelveData mirrors this (returns no data over the weekend window).
# We short-circuit before issuing the HTTP call to save the API budget.
def _is_fx_closed(now_utc: datetime) -> bool:
    wd, hr = now_utc.weekday(), now_utc.hour
    return (wd == 5 and hr >= 21) or (wd == 6 and hr < 21)


def _api_key() -> str:
    """Resolve the API key from env or load_engine_config. Raises
    RuntimeError before the HTTP call if neither is set."""
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        # Fallback to the engine config overlay (storage/config.py).
        try:
            from storage.config import load_engine_config
            cfg = load_engine_config()
            key = cfg.get("twelvedata_api_key") or ""
        except Exception:
            key = ""
    if not key:
        raise RuntimeError(
            "TWELVEDATA_API_KEY is not set. Add it to .env or export it. "
            "See OPERATIONS.md \"FX data source migration\" for setup."
        )
    return key


def fetch_latest_bar(now_utc: Optional[datetime] = None) -> dict[str, Any]:
    """Pull the most recent completed 1h bar from TwelveData.

    Returns a dict with the same status-code shape as the Dukascopy
    fetcher in fx/data/refresh.py:fetch_latest_bar:

      OK      — bar fetched successfully (`bar` key has the row dict)
      CLOSED  — FX market closed (Sat 21:00 → Sun 21:00 UTC); no API call made
      NO_DATA — API responded but returned no values for the window
      ERROR   — network / HTTP / parse failure (`error` key has detail)

    The `bar` dict is shaped to fit fx_prices_hourly_twelvedata.
    """
    now = now_utc or datetime.now(tz=timezone.utc).replace(tzinfo=None)

    if _is_fx_closed(now):
        return {
            "status": "CLOSED",
            "fetched_hour": None,
            "error": None,
            "bar": None,
        }

    try:
        api_key = _api_key()
    except RuntimeError as exc:
        return {"status": "ERROR", "fetched_hour": None,
                "error": str(exc), "bar": None}

    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "timezone": "UTC",
        "apikey": api_key,
        "outputsize": 1,
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.error("TwelveData request failed: %s", exc)
        return {"status": "ERROR", "fetched_hour": None,
                "error": str(exc), "bar": None}

    if resp.status_code != 200:
        return {"status": "ERROR", "fetched_hour": None,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "bar": None}

    try:
        payload = resp.json()
    except ValueError as exc:
        return {"status": "ERROR", "fetched_hour": None,
                "error": f"non-JSON response: {exc}", "bar": None}

    # TwelveData error responses come back as 200 with status="error".
    if isinstance(payload, dict) and payload.get("status") == "error":
        return {"status": "ERROR", "fetched_hour": None,
                "error": payload.get("message", "unknown TwelveData error"),
                "bar": None}

    values = payload.get("values") or []
    if not values:
        return {"status": "NO_DATA", "fetched_hour": None,
                "error": None, "bar": None}

    raw = values[0]
    # TwelveData datetime format: "2026-05-07 18:00:00" (with timezone=UTC)
    try:
        dt = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError) as exc:
        return {"status": "ERROR", "fetched_hour": None,
                "error": f"unparseable datetime in payload: {exc}",
                "bar": None}

    bar = {
        "datetime_utc": dt,
        "date": dt.date(),
        "weekday": dt.strftime("%A"),
        "hour_utc": dt.hour,
        "gbpeur_open": float(raw["open"]),
        "gbpeur_high": float(raw["high"]),
        "gbpeur_low": float(raw["low"]),
        "gbpeur_close": float(raw["close"]),
        # TwelveData free tier doesn't expose tick_count for FX; leave NULL.
        "tick_count": None,
        "data_quality": "OK",
    }
    return {
        "status": "OK",
        "fetched_hour": dt.strftime("%Y-%m-%d %H:%M UTC"),
        "error": None,
        "bar": bar,
    }


_VALID_TABLE_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _validate_table(name: str) -> str:
    if not name or any(c not in _VALID_TABLE_NAME_CHARS for c in name):
        raise ValueError(f"invalid table name: {name!r}")
    return name


def upsert_new_bars(
    conn: duckdb.DuckDBPyConnection,
    bar: dict[str, Any],
    table: str = "fx_prices_hourly_twelvedata",
) -> int:
    """Insert one bar into `table`. Default target is the parallel mirror
    used during migration; pass ``table="fx_prices_hourly"`` to write to
    the production table post-cutover.

    Returns 1 if inserted, 0 if a row with the same datetime_utc already
    existed."""
    if bar is None:
        return 0
    create_all_tables(conn)
    tbl = _validate_table(table)

    before = conn.execute(
        f"SELECT COUNT(*) FROM {tbl} WHERE datetime_utc = ?",
        [bar["datetime_utc"]],
    ).fetchone()[0]
    if before > 0:
        logger.info("Bar already in %s for %s; no-op", tbl, bar["datetime_utc"])
        return 0

    conn.execute(
        f"INSERT INTO {tbl} "
        "(datetime_utc, date, weekday, hour_utc, gbpeur_open, gbpeur_high, "
        " gbpeur_low, gbpeur_close, tick_count, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [bar["datetime_utc"], bar["date"], bar["weekday"], bar["hour_utc"],
         bar["gbpeur_open"], bar["gbpeur_high"], bar["gbpeur_low"],
         bar["gbpeur_close"], bar["tick_count"], bar["data_quality"]],
    )
    logger.info("Inserted TwelveData bar into %s for %s (close=%.5f)",
                tbl, bar["datetime_utc"], bar["gbpeur_close"])
    return 1


def refresh_prices(
    conn: duckdb.DuckDBPyConnection,
    table: str = "fx_prices_hourly_twelvedata",
) -> dict[str, Any]:
    """Fetch latest bar then upsert into `table`. Default writes to the
    parallel mirror; ``table="fx_prices_hourly"`` writes to production."""
    fetch_result = fetch_latest_bar()
    logger.info("TwelveData fetch: status=%s, bar=%s",
                fetch_result.get("status"), fetch_result.get("fetched_hour"))

    inserted = upsert_new_bars(conn, fetch_result.get("bar"), table=table)
    return {
        "fetch_status": fetch_result.get("status"),
        "fetched_hour": fetch_result.get("fetched_hour"),
        "fetch_error": fetch_result.get("error"),
        "rows_inserted": inserted,
    }
