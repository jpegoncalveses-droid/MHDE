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


from pipelines.market_calendar import is_forex_closed, fx_close_floor


def _utc_full(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


# is_forex_closed: True iff Fri 22:00 UTC <= now < Sun 22:00 UTC.

def test_is_forex_closed_friday_before_close():
    # Fri 21:59 UTC → False.
    assert is_forex_closed(_utc_full(2026, 5, 15, 21, 59)) is False


def test_is_forex_closed_friday_at_close():
    # Fri 22:00 UTC → True (boundary inclusive on the lower side).
    assert is_forex_closed(_utc_full(2026, 5, 15, 22, 0)) is True


def test_is_forex_closed_saturday_noon():
    assert is_forex_closed(_utc_full(2026, 5, 16, 12, 0)) is True


def test_is_forex_closed_sunday_before_resume():
    # Sun 21:59 UTC → True.
    assert is_forex_closed(_utc_full(2026, 5, 17, 21, 59)) is True


def test_is_forex_closed_sunday_at_resume():
    # Sun 22:00 UTC → False (boundary exclusive on upper side).
    assert is_forex_closed(_utc_full(2026, 5, 17, 22, 0)) is False


def test_is_forex_closed_midweek_is_open():
    # Wed 12:00 UTC → False.
    assert is_forex_closed(_utc_full(2026, 5, 13, 12, 0)) is False


# fx_close_floor: returns the Fri 22:00 UTC of the active closure.

def test_fx_close_floor_saturday():
    # Sat 2026-05-16 12:00 → Fri 2026-05-15 22:00.
    assert fx_close_floor(_utc_full(2026, 5, 16, 12, 0)) == _utc_full(2026, 5, 15, 22, 0)


def test_fx_close_floor_sunday_before_resume():
    # Sun 2026-05-17 21:59 → Fri 2026-05-15 22:00.
    assert fx_close_floor(_utc_full(2026, 5, 17, 21, 59)) == _utc_full(2026, 5, 15, 22, 0)


def test_fx_close_floor_friday_after_close():
    # Fri 2026-05-15 22:30 → Fri 2026-05-15 22:00 (same day).
    assert fx_close_floor(_utc_full(2026, 5, 15, 22, 30)) == _utc_full(2026, 5, 15, 22, 0)
