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

from datetime import date, timedelta


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
