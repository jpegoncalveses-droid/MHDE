"""Pre-cutover state report — what's in each FX price table right now."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = os.environ.get("MHDE_DB_PATH", str(REPO_ROOT / "data" / "mhde.duckdb"))


def main() -> int:
    conn = duckdb.connect(DB_PATH, read_only=True)
    try:
        for tbl in (
            "fx_prices_hourly",
            "fx_prices_hourly_twelvedata",
            "fx_prices_hourly_twelvedata_backfill",
        ):
            row = conn.execute(
                f"SELECT COUNT(*), MIN(datetime_utc), MAX(datetime_utc) FROM {tbl}"
            ).fetchone()
            n, lo, hi = row
            span_days = ((hi - lo).total_seconds() / 86400) if (lo and hi) else 0
            print(f"{tbl}")
            print(f"  rows: {n:>5}  range: {lo} → {hi}  ({span_days:.1f} days)")
        print()

        # 30-day overlap gaps Dukascopy is missing relative to TwelveData backfill
        gap = conn.execute("""
            SELECT COUNT(*) FROM fx_prices_hourly_twelvedata_backfill t
            WHERE NOT EXISTS (
              SELECT 1 FROM fx_prices_hourly d
              WHERE d.datetime_utc = t.datetime_utc
            )
        """).fetchone()[0]
        print(f"Bars in TwelveData backfill but missing from fx_prices_hourly: {gap}")

        # 30-day overlap gaps TwelveData backfill is missing relative to Dukascopy
        gap_other = conn.execute("""
            SELECT COUNT(*) FROM fx_prices_hourly d
            WHERE d.datetime_utc >= (SELECT MIN(datetime_utc) FROM fx_prices_hourly_twelvedata_backfill)
              AND d.datetime_utc <= (SELECT MAX(datetime_utc) FROM fx_prices_hourly_twelvedata_backfill)
              AND NOT EXISTS (
                SELECT 1 FROM fx_prices_hourly_twelvedata_backfill t
                WHERE t.datetime_utc = d.datetime_utc
              )
        """).fetchone()[0]
        print(f"Bars in fx_prices_hourly but missing from TwelveData backfill: {gap_other}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
