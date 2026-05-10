"""Unit tests for pipelines/market_calendar.py — pure UTC helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone

from pipelines.market_calendar import trading_days_between


def test_trading_days_between_same_weekday():
    # Wed only.
    assert trading_days_between(date(2026, 5, 6), date(2026, 5, 6)) == 1


def test_trading_days_between_skips_weekend():
    # Fri 2026-05-08 → Mon 2026-05-11 inclusive: Fri + Mon = 2 trading days.
    assert trading_days_between(date(2026, 5, 8), date(2026, 5, 11)) == 2


def test_trading_days_between_full_week():
    # Mon → Fri inclusive = 5.
    assert trading_days_between(date(2026, 5, 4), date(2026, 5, 8)) == 5


def test_trading_days_between_empty_range():
    # start > end → 0.
    assert trading_days_between(date(2026, 5, 8), date(2026, 5, 4)) == 0
