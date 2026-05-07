"""Compare GBP/EUR hourly bars between Dukascopy and TwelveData.

Used during the FX data-source migration (see DECISIONS.md ADR-013).
Reads from `fx_prices_hourly` (Dukascopy production) and
`fx_prices_hourly_twelvedata` (parallel mirror), joins on
datetime_utc, and reports per-bar pip divergence on close prices.

The Session 2 cutover gate is: every matched bar over the comparison
window has |pip_diff| ≤ threshold_pips.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb

from fx.config import PIP_SIZE
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.compare_sources")


def compare_recent(
    conn: duckdb.DuckDBPyConnection,
    hours: int = 24,
    threshold_pips: float = 5.0,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Compare the last `hours` of bars between the two sources.

    Returns a dict:
      {
        "window_start": datetime,
        "window_end":   datetime,
        "matched":      int,            # bars present in BOTH tables
        "missing_from_dukascopy":  int, # in TwelveData only
        "missing_from_twelvedata": int, # in Dukascopy only
        "within_threshold": int,        # matched bars within `threshold_pips`
        "breaches": [{datetime_utc, dukascopy_close, twelvedata_close, pip_diff}, ...],
        "all_within_threshold": bool,   # True iff matched > 0 and breaches == []
      }
    """
    create_all_tables(conn)

    now = (now_utc or datetime.now(tz=timezone.utc).replace(tzinfo=None))
    window_end = now
    window_start = now - timedelta(hours=hours)

    # Matched bars (present in both tables)
    matched_rows = conn.execute(
        """
        SELECT d.datetime_utc, d.gbpeur_close AS dukascopy_close,
               t.gbpeur_close AS twelvedata_close
        FROM fx_prices_hourly d
        JOIN fx_prices_hourly_twelvedata t
          ON d.datetime_utc = t.datetime_utc
        WHERE d.datetime_utc >= ? AND d.datetime_utc <= ?
        ORDER BY d.datetime_utc DESC
        """,
        [window_start, window_end],
    ).fetchall()

    breaches: list[dict[str, Any]] = []
    for dt, d_close, t_close in matched_rows:
        if d_close is None or t_close is None:
            continue
        pip_diff = (d_close - t_close) / PIP_SIZE
        if abs(pip_diff) > threshold_pips:
            breaches.append({
                "datetime_utc": dt,
                "dukascopy_close": float(d_close),
                "twelvedata_close": float(t_close),
                "pip_diff": round(pip_diff, 2),
            })

    matched = len(matched_rows)
    within = matched - len(breaches)

    # Asymmetric coverage (one source has the bar, the other doesn't)
    missing_from_twelvedata = conn.execute(
        """
        SELECT COUNT(*) FROM fx_prices_hourly d
        WHERE d.datetime_utc >= ? AND d.datetime_utc <= ?
          AND NOT EXISTS (
            SELECT 1 FROM fx_prices_hourly_twelvedata t
            WHERE t.datetime_utc = d.datetime_utc
          )
        """,
        [window_start, window_end],
    ).fetchone()[0]

    missing_from_dukascopy = conn.execute(
        """
        SELECT COUNT(*) FROM fx_prices_hourly_twelvedata t
        WHERE t.datetime_utc >= ? AND t.datetime_utc <= ?
          AND NOT EXISTS (
            SELECT 1 FROM fx_prices_hourly d
            WHERE d.datetime_utc = t.datetime_utc
          )
        """,
        [window_start, window_end],
    ).fetchone()[0]

    return {
        "window_start": window_start,
        "window_end": window_end,
        "threshold_pips": threshold_pips,
        "matched": matched,
        "missing_from_dukascopy": int(missing_from_dukascopy),
        "missing_from_twelvedata": int(missing_from_twelvedata),
        "within_threshold": within,
        "breaches": breaches,
        "all_within_threshold": matched > 0 and len(breaches) == 0,
    }


def format_report(result: dict[str, Any]) -> str:
    """Multi-line stdout report of a `compare_recent` result."""
    lines = [
        f"FX source comparison — Dukascopy vs TwelveData",
        f"  window: {result['window_start']} → {result['window_end']}",
        f"  threshold: {result['threshold_pips']} pips on close",
        f"",
        f"  matched bars (both sources):       {result['matched']}",
        f"  within threshold:                  {result['within_threshold']}",
        f"  missing from Dukascopy:            {result['missing_from_dukascopy']}",
        f"  missing from TwelveData:           {result['missing_from_twelvedata']}",
    ]
    if result["breaches"]:
        lines.append("")
        lines.append(f"  breaches ({len(result['breaches'])}):")
        lines.append(f"    {'datetime_utc':<22} {'dukascopy':>11} {'twelvedata':>11} {'pip_diff':>10}")
        for b in result["breaches"]:
            lines.append(
                f"    {str(b['datetime_utc']):<22} "
                f"{b['dukascopy_close']:>11.5f} "
                f"{b['twelvedata_close']:>11.5f} "
                f"{b['pip_diff']:>+10.2f}"
            )
    lines.append("")
    if result["matched"] == 0:
        lines.append("  STATUS: no overlapping bars in window — cannot decide cutover")
    elif result["all_within_threshold"]:
        lines.append(
            f"  STATUS: PASS — all {result['matched']} matched bars within "
            f"{result['threshold_pips']} pips. Eligible for cutover."
        )
    else:
        lines.append(
            f"  STATUS: FAIL — {len(result['breaches'])} bar(s) exceed "
            f"{result['threshold_pips']} pip threshold. Investigate before cutover."
        )
    return "\n".join(lines)
