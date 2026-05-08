"""One-shot historical backfill: pull 30 days of GBP/EUR 1h bars from TwelveData.

Writes into fx_prices_hourly_twelvedata_backfill — a NEW table created on
demand. Does NOT touch the live mirror table fx_prices_hourly_twelvedata
(which the systemd timer keeps writing into hourly) or the production
fx_prices_hourly (Dukascopy).

Usage:
  venv/bin/python .claude/local_scripts/fx_backfill_twelvedata_30d.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from fx.data.refresh_twelvedata import API_URL, INTERVAL, SYMBOL, _api_key

DB_PATH = os.environ.get("MHDE_DB_PATH", str(REPO_ROOT / "data" / "mhde.duckdb"))
BACKFILL_TABLE = "fx_prices_hourly_twelvedata_backfill"

DAYS_BACK = 30
HTTP_TIMEOUT_S = 30


def _ensure_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {BACKFILL_TABLE} (
            datetime_utc TIMESTAMP NOT NULL PRIMARY KEY,
            date DATE NOT NULL,
            weekday VARCHAR NOT NULL,
            hour_utc INTEGER NOT NULL,
            gbpeur_open DOUBLE,
            gbpeur_high DOUBLE,
            gbpeur_low DOUBLE,
            gbpeur_close DOUBLE,
            tick_count INTEGER,
            data_quality VARCHAR DEFAULT 'good',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _fetch_history(start_date: datetime, end_date: datetime) -> list[dict]:
    """Single TwelveData time_series call for the full window.

    Free-tier outputsize cap is 5000; 30 days × 24h = 720 fits easily."""
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "timezone": "UTC",
        "apikey": _api_key(),
        "start_date": start_date.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date": end_date.strftime("%Y-%m-%d %H:%M:%S"),
        "outputsize": 5000,
    }
    resp = requests.get(API_URL, params=params, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(f"TwelveData error: {payload.get('message')}")
    return payload.get("values") or []


def _parse_bar(raw: dict) -> dict:
    dt = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S")
    return {
        "datetime_utc": dt,
        "date": dt.date(),
        "weekday": dt.strftime("%A"),
        "hour_utc": dt.hour,
        "gbpeur_open": float(raw["open"]),
        "gbpeur_high": float(raw["high"]),
        "gbpeur_low": float(raw["low"]),
        "gbpeur_close": float(raw["close"]),
        "tick_count": None,
        "data_quality": "OK",
    }


def main() -> int:
    end_dt = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    start_dt = end_dt - timedelta(days=DAYS_BACK)
    print(f"Backfill window: {start_dt} → {end_dt} UTC ({DAYS_BACK} days)")
    print(f"Target table:    {BACKFILL_TABLE}")
    print(f"DB:              {DB_PATH}")
    print()

    t0 = time.monotonic()
    raw_bars = _fetch_history(start_dt, end_dt)
    fetch_s = time.monotonic() - t0
    print(f"TwelveData returned {len(raw_bars)} raw bars in {fetch_s:.2f}s")
    if not raw_bars:
        print("No bars returned — aborting.")
        return 1

    parsed = [_parse_bar(r) for r in raw_bars]
    parsed.sort(key=lambda b: b["datetime_utc"])

    conn = duckdb.connect(DB_PATH)
    try:
        _ensure_table(conn)
        before = conn.execute(f"SELECT COUNT(*) FROM {BACKFILL_TABLE}").fetchone()[0]

        inserted = 0
        skipped = 0
        for bar in parsed:
            exists = conn.execute(
                f"SELECT 1 FROM {BACKFILL_TABLE} WHERE datetime_utc = ?",
                [bar["datetime_utc"]],
            ).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(
                f"INSERT INTO {BACKFILL_TABLE} "
                "(datetime_utc, date, weekday, hour_utc, gbpeur_open, "
                " gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [bar["datetime_utc"], bar["date"], bar["weekday"], bar["hour_utc"],
                 bar["gbpeur_open"], bar["gbpeur_high"], bar["gbpeur_low"],
                 bar["gbpeur_close"], bar["tick_count"], bar["data_quality"]],
            )
            inserted += 1

        after = conn.execute(f"SELECT COUNT(*) FROM {BACKFILL_TABLE}").fetchone()[0]
        first = conn.execute(
            f"SELECT MIN(datetime_utc), MAX(datetime_utc) FROM {BACKFILL_TABLE}"
        ).fetchone()
        print()
        print(f"Rows in {BACKFILL_TABLE}: {before} → {after}  (+{inserted}, skipped {skipped})")
        print(f"Date range:              {first[0]} → {first[1]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
