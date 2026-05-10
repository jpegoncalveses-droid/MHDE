"""KI-128: weekday-aware health check behavior.

Pins the regression that pipelines/health_check.py::_check_equity
must NOT alert on Sun/Mon mornings when Friday's equity row exists
(the ML predict pipeline writes prediction_date = last closed
market day, which is Friday from Sat 00:15 UTC through Tue 00:14
UTC).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch


def _utc(year, month, day, hour=6) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def _patch_now(now: datetime):
    return patch("pipelines.health_check._today_utc", return_value=now)


def test_equity_check_ok_on_saturday_with_friday_row(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 16)):  # Saturday
        result = _check_equity(temp_db)
    assert result.ok, f"expected ok on Sat with Fri row; detail={result.detail}"


def test_equity_check_ok_on_sunday_with_friday_row(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 17)):  # Sunday — KI-128 regression
        result = _check_equity(temp_db)
    assert result.ok, f"expected ok on Sun with Fri row; detail={result.detail}"


def test_equity_check_ok_on_monday_with_friday_row(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 18)):  # Monday — KI-128 regression
        result = _check_equity(temp_db)
    assert result.ok, f"expected ok on Mon with Fri row; detail={result.detail}"


def test_equity_check_fails_on_monday_when_friday_row_missing(temp_db):
    """Outage detection must still work — empty predictions on Mon
    means the Friday fire never produced a row."""
    from pipelines.health_check import _check_equity
    with _patch_now(_utc(2026, 5, 18)):
        result = _check_equity(temp_db)
    assert not result.ok
    assert "no rows" in result.detail.lower() or "expected" in result.detail.lower()


def test_equity_check_ok_on_tuesday_with_monday_row(temp_db):
    from pipelines.health_check import _check_equity
    mon = date(2026, 5, 18)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [mon],
    )
    with _patch_now(_utc(2026, 5, 19)):  # Tuesday
        result = _check_equity(temp_db)
    assert result.ok


def test_equity_check_fails_on_tuesday_when_monday_row_missing(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 19)):  # Tuesday but only Fri row exists
        result = _check_equity(temp_db)
    assert not result.ok
