"""30-day comparison: Dukascopy fx_prices_hourly vs TwelveData backfill.

Compares the live fx_prices_hourly (Dukascopy) against the one-shot
fx_prices_hourly_twelvedata_backfill table populated by
fx_backfill_twelvedata_30d.py.

Reports the standard compare_recent metrics, then layers a clustering
analysis: which breaches fall near scheduled high-impact macro releases
(expected microstructure) vs which are unexplained source disagreement.

Usage:
  venv/bin/python .claude/local_scripts/fx_compare_30d.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from fx.data.compare_sources import compare_recent, format_report

DB_PATH = os.environ.get("MHDE_DB_PATH", str(REPO_ROOT / "data" / "mhde.duckdb"))
HOURS = 24 * 30
THRESHOLD_PIPS = 5.0
BACKFILL_TABLE = "fx_prices_hourly_twelvedata_backfill"

# High-impact macro releases inside the 2026-04-08 → 2026-05-08 window.
# Times in UTC. ±window_minutes around each event is treated as
# "high-vol microstructure". HIGH = verified via WebSearch; MED = inferred
# from typical scheduling.
EVENTS: list[tuple[datetime, int, str, str]] = [
    # (event_time_utc, +/- minutes, label, confidence)
    (datetime(2026, 4, 10, 12, 30), 90, "US CPI (March)", "MED"),
    (datetime(2026, 4, 16, 12, 30), 90, "US Retail Sales (March)", "MED"),
    (datetime(2026, 4, 17, 6, 0), 120, "UK CPI (March)", "MED"),
    (datetime(2026, 4, 23, 11, 45), 90, "ECB rate decision", "MED"),
    (datetime(2026, 4, 23, 12, 30), 90, "ECB press conference", "MED"),
    (datetime(2026, 4, 29, 18, 0), 90, "FOMC rate decision", "HIGH"),
    (datetime(2026, 4, 29, 18, 30), 90, "FOMC press conference", "HIGH"),
    (datetime(2026, 4, 30, 12, 30), 90, "US Q1 GDP advance", "MED"),
    (datetime(2026, 5, 1, 9, 0), 120, "Eurozone CPI flash (April)", "MED"),
    (datetime(2026, 5, 8, 12, 30), 120, "US NFP (April)", "HIGH"),
]


def _classify_breach(dt: datetime) -> tuple[bool, str]:
    """Return (is_near_event, label). Picks the closest event within window."""
    best: tuple[int, str] | None = None
    for ev_time, win_min, label, conf in EVENTS:
        delta = abs((dt - ev_time).total_seconds()) / 60
        if delta <= win_min:
            score = int(delta)
            if best is None or score < best[0]:
                best = (score, f"{label} [{conf}, {int(delta)}min away]")
    if best is None:
        return False, "—"
    return True, best[1]


def main() -> int:
    conn = duckdb.connect(DB_PATH, read_only=False)
    try:
        # Anchor "now" at the end of the backfilled data so the 30-day
        # window matches what the backfill loaded.
        end_dt = conn.execute(
            f"SELECT MAX(datetime_utc) FROM {BACKFILL_TABLE}"
        ).fetchone()[0]
        if end_dt is None:
            print(f"ERROR: {BACKFILL_TABLE} is empty. Run fx_backfill_twelvedata_30d.py first.")
            return 1

        result = compare_recent(
            conn,
            hours=HOURS,
            threshold_pips=THRESHOLD_PIPS,
            now_utc=end_dt,
            twelvedata_table=BACKFILL_TABLE,
        )

        print(format_report(result))
        print()
        print("=" * 72)
        print(f"  30-DAY BREACH CLUSTERING ANALYSIS")
        print("=" * 72)
        print()

        breaches = result["breaches"]
        if not breaches:
            print("  No breaches in the 30-day window.")
            return 0

        # Date-level histogram
        by_date: Counter[str] = Counter()
        for b in breaches:
            by_date[str(b["datetime_utc"].date())] += 1

        print(f"  Breaches by date (top 10 of {len(by_date)} unique dates):")
        for date_str, count in by_date.most_common(10):
            bar = "#" * count
            print(f"    {date_str}  {count:>3}  {bar}")
        print()

        # Event tagging
        near_event = []
        no_event = []
        for b in breaches:
            is_near, label = _classify_breach(b["datetime_utc"])
            (near_event if is_near else no_event).append((b, label))

        print(f"  Near scheduled releases (±90-120 min): {len(near_event)} of {len(breaches)}")
        print(f"  Unexplained (no nearby release):      {len(no_event)} of {len(breaches)}")
        print()

        if near_event:
            print(f"  Breaches near scheduled releases:")
            print(f"    {'datetime_utc':<22} {'pip_diff':>9}  event")
            for b, label in sorted(near_event, key=lambda x: x[0]["datetime_utc"]):
                print(
                    f"    {str(b['datetime_utc']):<22} "
                    f"{b['pip_diff']:>+9.2f}  {label}"
                )
            print()

        if no_event:
            print(f"  Unexplained breaches (no nearby scheduled release):")
            print(f"    {'datetime_utc':<22} {'dukascopy':>10} {'twelvedata':>11} {'pip_diff':>9}")
            for b, _ in sorted(no_event, key=lambda x: x[0]["datetime_utc"]):
                print(
                    f"    {str(b['datetime_utc']):<22} "
                    f"{b['dukascopy_close']:>10.5f} "
                    f"{b['twelvedata_close']:>11.5f} "
                    f"{b['pip_diff']:>+9.2f}"
                )
            print()

        # Verdict
        print("=" * 72)
        ratio = len(near_event) / len(breaches)
        if ratio >= 0.7:
            print(f"  VERDICT: {ratio:.0%} of breaches cluster near scheduled releases.")
            print(f"           Consistent with expected high-vol microstructure.")
            print(f"           Sources are operationally interchangeable for FX modeling.")
        elif ratio >= 0.4:
            print(f"  VERDICT: {ratio:.0%} of breaches near releases — mixed signal.")
            print(f"           Investigate the {len(no_event)} unexplained cases before cutover.")
        else:
            print(f"  VERDICT: only {ratio:.0%} of breaches near releases.")
            print(f"           Most disagreement is NOT release-driven.")
            print(f"           Likely systematic source difference — DO NOT cutover yet.")
        print("=" * 72)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
