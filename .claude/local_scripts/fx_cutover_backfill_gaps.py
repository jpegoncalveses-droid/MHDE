"""Cutover step: copy missing TwelveData bars into fx_prices_hourly.

Reads from fx_prices_hourly_twelvedata_backfill (the 30-day snapshot),
inserts any datetime_utc that does NOT already exist in the production
fx_prices_hourly table. Existing rows are not touched. Idempotent.

Run after the cutover code change (refresh.py → TwelveData) and before
the next mhde-fx-predict.timer firing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = os.environ.get("MHDE_DB_PATH", str(REPO_ROOT / "data" / "mhde.duckdb"))


def main() -> int:
    conn = duckdb.connect(DB_PATH, read_only=False)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM fx_prices_hourly"
        ).fetchone()[0]
        gaps = conn.execute("""
            SELECT COUNT(*) FROM fx_prices_hourly_twelvedata_backfill t
            WHERE NOT EXISTS (
              SELECT 1 FROM fx_prices_hourly d
              WHERE d.datetime_utc = t.datetime_utc
            )
        """).fetchone()[0]
        print(f"fx_prices_hourly rows before:    {before:>6}")
        print(f"Gaps to fill from backfill:      {gaps:>6}")
        if gaps == 0:
            print("Nothing to do.")
            return 0

        conn.execute("""
            INSERT INTO fx_prices_hourly
              (datetime_utc, date, weekday, hour_utc, gbpeur_open,
               gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality)
            SELECT datetime_utc, date, weekday, hour_utc, gbpeur_open,
                   gbpeur_high, gbpeur_low, gbpeur_close, tick_count,
                   'OK' AS data_quality
            FROM fx_prices_hourly_twelvedata_backfill t
            WHERE NOT EXISTS (
              SELECT 1 FROM fx_prices_hourly d
              WHERE d.datetime_utc = t.datetime_utc
            )
        """)
        after = conn.execute(
            "SELECT COUNT(*) FROM fx_prices_hourly"
        ).fetchone()[0]
        print(f"fx_prices_hourly rows after:     {after:>6}  (+{after - before})")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
