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


def _trading_days_between(start: date, end: date) -> int:
    """Inclusive trading-day count (Mon-Fri) between two dates. start <= end."""
    if start > end:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def check_equity_freshness(
    conn: duckdb.DuckDBPyConnection,
    today: Optional[date] = None,
    max_trading_days: int = 2,
) -> FreshnessReport:
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
    trading_gap = _trading_days_between(latest + timedelta(days=1), today)
    is_fresh = trading_gap <= max_trading_days
    msg = (f"Equity prices_daily latest={latest} "
           f"({trading_gap} trading-day gap; threshold={max_trading_days})")
    return FreshnessReport(
        engine="equity", is_fresh=is_fresh, latest=latest, age=age,
        age_str=_format_age(age), threshold=f"{max_trading_days} trading days",
        message=msg,
    )


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
