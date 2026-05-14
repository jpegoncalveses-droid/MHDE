"""Data-freshness checks for the three prediction engines.

Each check returns a `FreshnessReport` with:
    is_fresh   — True if data meets the engine's recency policy
    latest     — latest timestamp/date in the price table (None if empty)
    age        — timedelta from "now" (UTC) to `latest`; None if empty
    age_str    — human-readable age (e.g. "2h 14m", "3 days")
    threshold  — the staleness threshold used for the decision
    message    — human-readable status line

Policies:
    Equity:  prices_daily.trade_date must be within 2 trading days of today.
    Crypto:  crypto_prices_daily.trade_date must be within 1 calendar day of today.
    FX:      fx_prices_hourly.datetime_utc must be within 2 hours of now.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union

import duckdb

from pipelines.market_calendar import (
    trading_days_between,
    is_forex_closed,
    fx_close_floor,
)

logger = logging.getLogger("mhde.freshness")


@dataclass
class FreshnessReport:
    engine: str
    is_fresh: bool
    latest: Optional[Union[date, datetime]]
    age: Optional[timedelta]
    age_str: str
    threshold: str
    message: str
    # KI-149: coverage-aware fields for the equity check. Populated only when
    # the coverage path runs (currently equity only); None for other engines.
    reason: Optional[str] = None
    coverage_row_count: Optional[int] = None
    coverage_expected_min: Optional[int] = None


def _format_age(age: Optional[timedelta]) -> str:
    if age is None:
        return "n/a"
    total_seconds = int(age.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


_EQUITY_COVERAGE_RATIO = 0.5
_EQUITY_COVERAGE_WINDOW_DAYS = 30


def check_equity_freshness(
    conn: duckdb.DuckDBPyConnection,
    today: Optional[date] = None,
    max_trading_days: int = 2,
) -> FreshnessReport:
    """Equity freshness, KI-149 hardened.

    Two-stage check:
      1. Latest trade_date within `max_trading_days` (existing behavior).
      2. Latest trade_date row count ≥ ``_EQUITY_COVERAGE_RATIO`` × mean
         daily row count over the prior ``_EQUITY_COVERAGE_WINDOW_DAYS``
         trading dates. Closes the silent-T-2 skip: 4 fallback OTC rows
         on the current date used to satisfy a MAX-only check.
    """
    today = today or datetime.now(tz=timezone.utc).date()
    row = conn.execute("SELECT MAX(trade_date) FROM prices_daily").fetchone()
    latest = row[0] if row else None

    if latest is None:
        return FreshnessReport(
            engine="equity", is_fresh=False, latest=None, age=None,
            age_str="n/a", threshold=f"{max_trading_days} trading days",
            message="prices_daily is empty",
        )

    age = datetime.combine(today, datetime.min.time()) - datetime.combine(latest, datetime.min.time())
    trading_gap = trading_days_between(latest + timedelta(days=1), today)
    base_msg = (f"Equity prices_daily latest={latest} "
                f"({trading_gap} trading-day gap; threshold={max_trading_days})")

    if trading_gap > max_trading_days:
        return FreshnessReport(
            engine="equity", is_fresh=False, latest=latest, age=age,
            age_str=_format_age(age), threshold=f"{max_trading_days} trading days",
            message=base_msg,
        )

    latest_count, expected_min = _equity_latest_coverage(conn, latest)
    if latest_count < expected_min:
        msg = (f"{base_msg}; partial coverage on {latest}: "
               f"{latest_count} rows < expected ≥{expected_min}")
        return FreshnessReport(
            engine="equity", is_fresh=False, latest=latest, age=age,
            age_str=_format_age(age), threshold=f"{max_trading_days} trading days",
            message=msg,
            reason="partial_coverage",
            coverage_row_count=latest_count,
            coverage_expected_min=expected_min,
        )

    return FreshnessReport(
        engine="equity", is_fresh=True, latest=latest, age=age,
        age_str=_format_age(age), threshold=f"{max_trading_days} trading days",
        message=base_msg,
    )


def _equity_latest_coverage(
    conn: duckdb.DuckDBPyConnection, latest: date
) -> tuple[int, int]:
    """Return (row_count_on_latest, expected_min) for KI-149 coverage check.

    expected_min = round(_EQUITY_COVERAGE_RATIO × mean daily row count over
    the prior `_EQUITY_COVERAGE_WINDOW_DAYS` trade dates). Falls back to a
    permissive threshold (== latest_count) when there is no prior history,
    so seeding a single row still passes.
    """
    latest_count = conn.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE trade_date = ?", [latest]
    ).fetchone()[0]

    history = conn.execute(
        """
        WITH daily AS (
            SELECT trade_date, COUNT(*) AS n
            FROM prices_daily
            WHERE trade_date < ?
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT ?
        )
        SELECT AVG(n) FROM daily
        """,
        [latest, _EQUITY_COVERAGE_WINDOW_DAYS],
    ).fetchone()
    mean_prior = history[0] if history and history[0] is not None else None

    if mean_prior is None:
        return latest_count, latest_count

    expected_min = int(round(_EQUITY_COVERAGE_RATIO * float(mean_prior)))
    return latest_count, expected_min


def check_crypto_freshness(
    conn: duckdb.DuckDBPyConnection,
    today: Optional[date] = None,
    max_calendar_days: int = 1,
) -> FreshnessReport:
    today = today or datetime.now(tz=timezone.utc).date()
    row = conn.execute("SELECT MAX(trade_date) FROM crypto_prices_daily").fetchone()
    latest = row[0] if row else None

    if latest is None:
        return FreshnessReport(
            engine="crypto", is_fresh=False, latest=None, age=None,
            age_str="n/a", threshold=f"{max_calendar_days} day",
            message="crypto_prices_daily is empty",
        )

    gap_days = (today - latest).days
    age = datetime.combine(today, datetime.min.time()) - datetime.combine(latest, datetime.min.time())
    is_fresh = gap_days <= max_calendar_days
    msg = (f"Crypto crypto_prices_daily latest={latest} "
           f"({gap_days}-day gap; threshold={max_calendar_days})")
    return FreshnessReport(
        engine="crypto", is_fresh=is_fresh, latest=latest, age=age,
        age_str=_format_age(age), threshold=f"{max_calendar_days} day",
        message=msg,
    )


def check_fx_freshness(
    conn: duckdb.DuckDBPyConnection,
    now: Optional[datetime] = None,
    max_hours: int = 2,
) -> FreshnessReport:
    now = now or datetime.now(tz=timezone.utc).replace(tzinfo=None)
    row = conn.execute("SELECT MAX(datetime_utc) FROM fx_prices_hourly").fetchone()
    latest = row[0] if row else None

    if latest is None:
        return FreshnessReport(
            engine="fx", is_fresh=False, latest=None, age=None,
            age_str="n/a", threshold=f"{max_hours}h",
            message="fx_prices_hourly is empty",
        )

    # `now` enters tz-naive (the existing contract); helpers expect
    # tz-aware UTC. Convert at the boundary, branch, then return.
    now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    if is_forex_closed(now_aware):
        floor = fx_close_floor(now_aware).replace(tzinfo=None)
        is_fresh = latest >= floor
        age = now - latest
        msg = (
            f"FX fx_prices_hourly latest={latest} during forex-closed "
            f"window; floor={floor} (KI-128)"
        )
        return FreshnessReport(
            engine="fx", is_fresh=is_fresh, latest=latest, age=age,
            age_str=_format_age(age), threshold=f"forex-closed floor {floor}",
            message=msg,
        )

    age = now - latest
    is_fresh = age <= timedelta(hours=max_hours)
    msg = (f"FX fx_prices_hourly latest={latest} "
           f"(age={_format_age(age)}; threshold={max_hours}h)")
    return FreshnessReport(
        engine="fx", is_fresh=is_fresh, latest=latest, age=age,
        age_str=_format_age(age), threshold=f"{max_hours}h",
        message=msg,
    )


def check_all(conn: duckdb.DuckDBPyConnection) -> dict[str, FreshnessReport]:
    """Convenience: return all three reports keyed by engine."""
    return {
        "equity": check_equity_freshness(conn),
        "crypto": check_crypto_freshness(conn),
        "fx": check_fx_freshness(conn),
    }
