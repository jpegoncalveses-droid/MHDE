"""Unit tests for pipelines/market_calendar.py — pure UTC helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone

from pipelines.market_calendar import expected_equity_prediction_date, trading_days_between


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


def _utc(year: int, month: int, day: int, hour: int = 6) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def test_expected_equity_prediction_date_tuesday_returns_monday():
    # Tue 2026-05-12 06:00 UTC → Mon 2026-05-11.
    assert expected_equity_prediction_date(_utc(2026, 5, 12)) == date(2026, 5, 11)


def test_expected_equity_prediction_date_wednesday_returns_tuesday():
    assert expected_equity_prediction_date(_utc(2026, 5, 13)) == date(2026, 5, 12)


def test_expected_equity_prediction_date_thursday_returns_wednesday():
    assert expected_equity_prediction_date(_utc(2026, 5, 14)) == date(2026, 5, 13)


def test_expected_equity_prediction_date_friday_returns_thursday():
    assert expected_equity_prediction_date(_utc(2026, 5, 15)) == date(2026, 5, 14)


def test_expected_equity_prediction_date_saturday_returns_friday():
    # Sat 2026-05-16 → Fri 2026-05-15.
    assert expected_equity_prediction_date(_utc(2026, 5, 16)) == date(2026, 5, 15)


def test_expected_equity_prediction_date_sunday_returns_friday():
    # Sun 2026-05-17 → Fri 2026-05-15 (skip Sat).
    assert expected_equity_prediction_date(_utc(2026, 5, 17)) == date(2026, 5, 15)


def test_expected_equity_prediction_date_monday_returns_friday():
    # Mon 2026-05-18 → Fri 2026-05-15 (skip Sun + Sat).
    assert expected_equity_prediction_date(_utc(2026, 5, 18)) == date(2026, 5, 15)


def test_expected_equity_prediction_date_independent_of_hour():
    # Same day, different hour → same result.
    assert expected_equity_prediction_date(_utc(2026, 5, 18, 0)) == date(2026, 5, 15)
    assert expected_equity_prediction_date(_utc(2026, 5, 18, 23)) == date(2026, 5, 15)
