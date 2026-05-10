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

from datetime import date, datetime, timedelta


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
