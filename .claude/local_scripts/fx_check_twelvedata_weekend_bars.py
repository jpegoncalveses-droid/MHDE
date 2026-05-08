"""Inspect weekend bars in fx_prices_hourly_twelvedata_backfill.

FX market is closed Fri 22:00 UTC → Sun 22:00 UTC. A real OTC bar in
that window will show genuine OHLC movement; a carried-forward bar
will show O==H==L==C (or near-zero movement) repeating.

Reports:
  1. Summary stats over ALL weekend bars in the 30-day backfill.
  2. Samples at Saturday 03:00 UTC and 12:00 UTC for the last 4 weekends.
  3. Verdict (carried-forward vs real movement).

Usage:
  venv/bin/python .claude/local_scripts/fx_check_twelvedata_weekend_bars.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = os.environ.get("MHDE_DB_PATH", str(REPO_ROOT / "data" / "mhde.duckdb"))
PIP = 0.0001
TABLE = "fx_prices_hourly_twelvedata_backfill"

# Closed window: Friday 22:00 UTC through Sunday 21:59 UTC (inclusive).
# DuckDB strftime('%w') returns Sunday=0, Monday=1, ..., Saturday=6.
WEEKEND_PREDICATE = """
    (
        (strftime('%w', datetime_utc) = '5' AND hour_utc >= 22)  -- Fri 22-23
        OR  strftime('%w', datetime_utc) = '6'                   -- All Sat
        OR (strftime('%w', datetime_utc) = '0' AND hour_utc < 22) -- Sun 00-21
    )
"""


def _run(conn: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    return conn.execute(sql, params or []).fetchall()


def main() -> int:
    conn = duckdb.connect(DB_PATH, read_only=True)
    try:
        total = _run(conn, f"SELECT COUNT(*) FROM {TABLE}")[0][0]
        weekend_total = _run(
            conn, f"SELECT COUNT(*) FROM {TABLE} WHERE {WEEKEND_PREDICATE}"
        )[0][0]
        print(f"Backfill rows total:      {total}")
        print(f"Weekend rows (Fri22-Sun22 UTC): {weekend_total}")
        print()

        if weekend_total == 0:
            print("No weekend bars in the backfill — TwelveData skips closed window.")
            print("VERDICT: REAL (TwelveData honors the FX session boundary).")
            return 0

        # Aggregate movement stats across all weekend bars.
        stats = _run(
            conn,
            f"""
            SELECT
              COUNT(*),
              SUM(CASE WHEN gbpeur_open = gbpeur_close
                       AND gbpeur_open = gbpeur_high
                       AND gbpeur_open = gbpeur_low THEN 1 ELSE 0 END),
              AVG(ABS(gbpeur_close - gbpeur_open) / {PIP}),
              MAX(ABS(gbpeur_close - gbpeur_open) / {PIP}),
              AVG((gbpeur_high - gbpeur_low) / {PIP}),
              MAX((gbpeur_high - gbpeur_low) / {PIP})
            FROM {TABLE}
            WHERE {WEEKEND_PREDICATE}
            """,
        )[0]
        n, all_equal, avg_co_pip, max_co_pip, avg_hl_pip, max_hl_pip = stats
        print(f"Weekend OHLC stats (n={n}):")
        print(f"  bars with O=H=L=C (zero range):     {all_equal} / {n}  ({all_equal/n:.1%})")
        print(f"  |close - open| (pips)  mean={avg_co_pip:.2f}  max={max_co_pip:.2f}")
        print(f"  (high - low)   (pips)  mean={avg_hl_pip:.2f}  max={max_hl_pip:.2f}")
        print()

        # Per-weekend close-to-prev-close: are values changing across hours?
        same_as_prev = _run(
            conn,
            f"""
            WITH w AS (
                SELECT datetime_utc, gbpeur_close,
                       LAG(gbpeur_close) OVER (ORDER BY datetime_utc) AS prev_close
                FROM {TABLE}
                WHERE {WEEKEND_PREDICATE}
            )
            SELECT COUNT(*) FROM w WHERE prev_close IS NOT NULL AND prev_close = gbpeur_close
            """,
        )[0][0]
        consec_pairs = _run(
            conn,
            f"""
            WITH w AS (
                SELECT datetime_utc, gbpeur_close,
                       LAG(gbpeur_close) OVER (ORDER BY datetime_utc) AS prev_close
                FROM {TABLE}
                WHERE {WEEKEND_PREDICATE}
            )
            SELECT COUNT(*) FROM w WHERE prev_close IS NOT NULL
            """,
        )[0][0]
        print(f"  consecutive weekend bars with identical close: {same_as_prev} / {consec_pairs}")
        print()

        # Sample: Sat 03:00 and 12:00 UTC for each weekend in the window.
        samples = _run(
            conn,
            f"""
            SELECT datetime_utc, gbpeur_open, gbpeur_high,
                   gbpeur_low, gbpeur_close
            FROM {TABLE}
            WHERE {WEEKEND_PREDICATE}
              AND strftime('%w', datetime_utc) = '6'
              AND hour_utc IN (3, 12)
            ORDER BY datetime_utc
            """,
        )
        print(f"Saturday 03:00 / 12:00 UTC samples ({len(samples)} rows):")
        print(f"  {'datetime_utc':<22} {'open':>9} {'high':>9} {'low':>9} {'close':>9} "
              f"{'C-O pip':>9} {'H-L pip':>9}")
        for dt, o, h, lo, c in samples:
            co = (c - o) / PIP
            hl = (h - lo) / PIP
            print(f"  {str(dt):<22} {o:>9.5f} {h:>9.5f} {lo:>9.5f} {c:>9.5f} "
                  f"{co:>+9.2f} {hl:>9.2f}")
        print()

        # Verdict
        zero_range_ratio = all_equal / n if n else 0
        avg_movement = avg_co_pip
        print("=" * 64)
        if zero_range_ratio >= 0.8 or avg_movement < 0.5:
            print(f"  VERDICT: CARRIED-FORWARD")
            print(f"           {zero_range_ratio:.0%} of weekend bars have zero OHLC range,")
            print(f"           avg close-open movement {avg_movement:.2f} pips.")
            print(f"           These are stale fills, NOT real OTC trading.")
            print(f"           ACTION: filter weekend bars before training/inference.")
        elif zero_range_ratio >= 0.3 or avg_movement < 1.5:
            print(f"  VERDICT: MIXED")
            print(f"           {zero_range_ratio:.0%} zero-range, avg movement {avg_movement:.2f} pips.")
            print(f"           Some real OTC, some carried-forward. Filter recommended.")
        else:
            print(f"  VERDICT: REAL OTC")
            print(f"           Only {zero_range_ratio:.0%} zero-range, avg {avg_movement:.2f} pips of")
            print(f"           movement. Bars reflect genuine weekend OTC quotes.")
            print(f"           No filtering needed.")
        print("=" * 64)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
