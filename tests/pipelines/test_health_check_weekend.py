"""KI-128: weekday-aware health check behavior.

Pins the regression that pipelines/health_check.py::_check_equity
must NOT alert on Sun/Mon mornings when Friday's equity row exists
(the ML predict pipeline writes prediction_date = last closed
market day, which is Friday from Sat 00:15 UTC through Tue 00:14
UTC).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


def test_fx_check_ok_on_saturday_with_pre_close_bar(temp_db):
    """KI-128: forex closed Fri 22:00 UTC -> Sun 22:00 UTC. With a
    bar at Fri 21:00 UTC the check must pass."""
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC, naive
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 16, 12)):  # Sat 12:00 UTC
        result = _check_fx(temp_db)
    assert result.ok, f"expected ok during close with pre-close bar; detail={result.detail}"


def test_fx_check_ok_on_sunday_evening_with_pre_close_bar(temp_db):
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 17, 21)):  # Sun 21:00 UTC, still closed
        result = _check_fx(temp_db)
    assert result.ok


def test_fx_check_fails_during_close_with_outage_in_flight(temp_db):
    """Real outage starting before the close: latest predates fx_close_floor."""
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 13, 10, 0, 0)  # Wed 10:00 UTC
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 16, 12)):  # Sat 12:00 UTC
        result = _check_fx(temp_db)
    assert not result.ok
    assert "floor" in result.detail.lower() or "predates" in result.detail.lower() or "older" in result.detail.lower()


def test_fx_check_fails_post_resume_with_stale_data(temp_db):
    """Sun 23:00 UTC -- closed window ended at Sun 22:00. 2h budget
    active. Stale Friday bar must alert."""
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 17, 23)):  # Sun 23:00 UTC, post-resume
        result = _check_fx(temp_db)
    assert not result.ok


def test_fx_check_ok_midweek_with_recent_bar(temp_db):
    """Sanity: existing 2h-budget behavior unchanged outside the window."""
    from pipelines.health_check import _check_fx
    now = _utc(2026, 5, 13, 12)  # Wed 12:00 UTC
    fx_dt = (now - timedelta(hours=1)).replace(tzinfo=None)
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(now):
        result = _check_fx(temp_db)
    assert result.ok
