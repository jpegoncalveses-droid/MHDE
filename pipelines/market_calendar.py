"""Pure UTC helpers for market-clock decisions.

Single source of truth for weekday and forex-closed-window logic
across pipelines/health_check.py, monitoring/pipeline_execution.py,
and pipelines/freshness.py.

No DB. No network. No I/O. All callers must pass a tz-aware UTC
datetime as `now` so tests are deterministic.

See docs/superpowers/specs/2026-05-10-ki128-weekday-aware-recency-design.md
for the full design and DECISIONS.md ADR-018 for the rationale.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def expected_equity_prediction_date(now: datetime) -> date:
    """Return the most recent Mon-Fri *strictly before* now.date().

    Equity ML predict runs at 00:15 UTC and writes
    prediction_date = "latest closed market day", which is the most
    recent weekday before today. By 06:00 UTC of any day, that's:

      Mon → Fri (Sat/Sun closed, so back to Fri)
      Tue → Mon
      Wed → Tue
      Thu → Wed
      Fri → Thu
      Sat → Fri
      Sun → Fri

    Replaces the literal `now.date() - 1` previously used in
    pipelines/health_check.py::_check_equity, which silently returned
    Sat or Sun on Sun/Mon mornings — neither has equity data because
    NYSE is closed.

    `now` must be tz-aware UTC; .date() is taken in the UTC frame.
    """
    cur = now.date() - timedelta(days=1)
    while cur.weekday() >= 5:  # Sat=5, Sun=6
        cur -= timedelta(days=1)
    return cur


# Forex spot trades roughly Sun 22:00 UTC → Fri 22:00 UTC. The
# closed window is the rest. Lower bound inclusive, upper exclusive
# so a `now` exactly at Sun 22:00 UTC is treated as open.
_FRIDAY = 4
_SATURDAY = 5
_SUNDAY = 6
_FOREX_CLOSE_HOUR_UTC = 22
_LAST_FX_BAR_HOUR_UTC = 21


def is_forex_closed(now: datetime) -> bool:
    """True iff Fri 22:00 UTC <= now < Sun 22:00 UTC.

    `now` must be tz-aware UTC.
    """
    wd = now.weekday()
    if wd == _SATURDAY:
        return True
    if wd == _FRIDAY and now.hour >= _FOREX_CLOSE_HOUR_UTC:
        return True
    if wd == _SUNDAY and now.hour < _FOREX_CLOSE_HOUR_UTC:
        return True
    return False


def fx_close_floor(now: datetime) -> datetime:
    """Return the timestamp of the last FX bar expected before the
    forex close that contains `now`. Caller is expected to pass a
    `now` for which `is_forex_closed(now)` is True; behavior outside
    that window is undefined.

    FX hourly bars stamp `datetime_utc` at the start of the hour they
    cover. Forex closes at 22:00 UTC, so the bar covering 21:00-22:00
    UTC trading has `datetime_utc = 21:00:00` and is the last bar
    that exists before close. The floor is therefore Fri 21:00 UTC
    of the active closure.

    Used as the lower bound the latest FX bar must satisfy
    (`latest >= fx_close_floor(now)`) during the closed window for
    the data to count as healthy.
    """
    wd = now.weekday()
    # Days back to the most recent Friday: Fri=0, Sat=1, Sun=2.
    days_back = (wd - _FRIDAY) % 7
    floor_date = (now - timedelta(days=days_back)).date()
    return datetime(
        floor_date.year, floor_date.month, floor_date.day,
        _LAST_FX_BAR_HOUR_UTC, 0, 0, tzinfo=timezone.utc,
    )


def trading_days_between(start: date, end: date) -> int:
    """Inclusive Mon-Fri count between two dates. Returns 0 if start > end.

    Moved verbatim from pipelines/freshness.py during the KI-128 fix
    so all market-clock helpers live together.
    """
    if start > end:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days
