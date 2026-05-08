"""Regression tests for the maturity / % move / time-remaining columns
shown by the dashboard prediction tabs.

The maturity computation MUST mirror each engine's `fill_outcomes` logic:
    - equity   → trading rows (ROW_NUMBER on prices_daily)
    - crypto   → calendar days (prediction_date + INTERVAL N days)
    - fx       → calendar hours (datetime_utc + INTERVAL N hours)

These tests verify both:
    1. The SQL helpers return the right maturity_date / price_at_maturity.
    2. The pure Python format helpers handle pending vs filled, near-due,
       far-future, and past-due cases correctly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from dashboard.services.maturity import (
    TimeRemaining,
    format_pct_move,
    format_time_remaining,
    pct_move_equity_or_crypto,
    pct_move_fx,
    time_remaining_days,
    time_remaining_hours,
)
from dashboard.services.queries import (
    get_crypto_predictions,
    get_equity_predictions,
    get_fx_recent_predictions,
)


def _as_date(value) -> date:
    """DuckDB DATE columns surface as pandas.Timestamp through fetchdf();
    coerce to plain date for unambiguous equality."""
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        return value.to_pydatetime().date()
    return value


# ──────────────────────────────────────────────────────────────────────
# Maturity matches fill_outcomes — equity (TRADING ROWS)
# ──────────────────────────────────────────────────────────────────────


def test_equity_maturity_is_5th_trading_row(temp_db):
    """horizon=5d → maturity_date = trade_date of the 5th trading row
    after the entry row (matches ml/predict.py::fill_outcomes which uses
    `mat.rn = entry.rn + 5`)."""
    pred_date = date(2026, 4, 6)  # Mon
    # 1 entry row + 5 forward rows + 1 buffer = 7 weekday rows
    cur = pred_date
    rows = []
    while len(rows) < 7:
        if cur.weekday() < 5:
            rows.append(cur)
        cur = cur + timedelta(days=1)
    for i, d in enumerate(rows):
        temp_db.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
            [f"r{i}", "AAPL", d, 100.0 + i],
        )
    temp_db.execute(
        """
        INSERT INTO ml_predictions
            (ticker, prediction_date, model_id, horizon, predicted_probability,
             prediction_threshold)
        VALUES ('AAPL', ?, 'm1', '5d', 0.7, 0.5)
        """,
        [pred_date],
    )
    df = get_equity_predictions(temp_db, pred_date)
    assert len(df) == 1
    # Entry rn=1, maturity at rn=6 → that is rows[5] (0-indexed)
    assert _as_date(df.iloc[0]["maturity_date"]) == rows[5]
    assert df.iloc[0]["price_at_maturity"] == pytest.approx(105.0)


def test_equity_maturity_skips_weekends_via_row_number(temp_db):
    """ROW_NUMBER on actual trade_dates means weekends aren't counted —
    horizon='10d' lands on the 10th trading row, NOT calendar day +10."""
    pred_date = date(2026, 4, 6)  # Mon
    cur, rows = pred_date, []
    while len(rows) < 12:
        if cur.weekday() < 5:
            rows.append(cur)
        cur += timedelta(days=1)
    for i, d in enumerate(rows):
        temp_db.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
            [f"r{i}", "MSFT", d, 200.0],
        )
    temp_db.execute(
        """
        INSERT INTO ml_predictions
            (ticker, prediction_date, model_id, horizon, predicted_probability)
        VALUES ('MSFT', ?, 'm1', '10d', 0.6)
        """,
        [pred_date],
    )
    df = get_equity_predictions(temp_db, pred_date)
    assert _as_date(df.iloc[0]["maturity_date"]) == rows[10]
    # That date is later than calendar +10 days because the window spans 2 weekends
    assert (rows[10] - pred_date).days > 10


def test_equity_maturity_null_when_not_enough_rows(temp_db):
    """If only entry row exists, no 5th-row trade_date yet → NULL."""
    pred_date = date(2026, 5, 5)
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["r0", "NEWCO", pred_date, 50.0],
    )
    temp_db.execute(
        """
        INSERT INTO ml_predictions
            (ticker, prediction_date, model_id, horizon, predicted_probability)
        VALUES ('NEWCO', ?, 'm1', '5d', 0.55)
        """,
        [pred_date],
    )
    df = get_equity_predictions(temp_db, pred_date)
    assert pd.isna(df.iloc[0]["maturity_date"])
    assert pd.isna(df.iloc[0]["price_at_maturity"])


# ──────────────────────────────────────────────────────────────────────
# Maturity matches fill_outcomes — crypto (CALENDAR DAYS)
# ──────────────────────────────────────────────────────────────────────


def test_crypto_maturity_is_prediction_plus_n_days(temp_db):
    """horizon='5d' → maturity = prediction_date + 5 calendar days
    (matches crypto/ml/predict.py::fill_outcomes interval CASE)."""
    pred_date = date(2026, 4, 1)
    expected_maturity = date(2026, 4, 6)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["BTCUSDT", pred_date, 100_000.0],
    )
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["BTCUSDT", expected_maturity, 105_000.0],
    )
    temp_db.execute(
        """
        INSERT INTO crypto_ml_predictions
            (symbol, prediction_date, model_id, horizon, predicted_probability)
        VALUES ('BTCUSDT', ?, 'c1', '5d', 0.7)
        """,
        [pred_date],
    )
    df = get_crypto_predictions(temp_db, pred_date)
    assert _as_date(df.iloc[0]["maturity_date"]) == expected_maturity
    assert df.iloc[0]["price_at_maturity"] == pytest.approx(105_000.0)


def test_crypto_maturity_horizon_10d(temp_db):
    pred_date = date(2026, 4, 1)
    expected_maturity = date(2026, 4, 11)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["ETHUSDT", pred_date, 3500.0],
    )
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["ETHUSDT", expected_maturity, 3700.0],
    )
    temp_db.execute(
        """
        INSERT INTO crypto_ml_predictions
            (symbol, prediction_date, model_id, horizon, predicted_probability)
        VALUES ('ETHUSDT', ?, 'c1', '10d', 0.6)
        """,
        [pred_date],
    )
    df = get_crypto_predictions(temp_db, pred_date)
    assert _as_date(df.iloc[0]["maturity_date"]) == expected_maturity
    assert df.iloc[0]["price_at_maturity"] == pytest.approx(3700.0)


def test_crypto_price_at_maturity_null_when_no_row(temp_db):
    pred_date = date(2026, 4, 1)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["NEWCOIN", pred_date, 1.0],
    )
    temp_db.execute(
        """
        INSERT INTO crypto_ml_predictions
            (symbol, prediction_date, model_id, horizon, predicted_probability)
        VALUES ('NEWCOIN', ?, 'c1', '5d', 0.55)
        """,
        [pred_date],
    )
    df = get_crypto_predictions(temp_db, pred_date)
    # Maturity date is computed (calendar) but price row doesn't exist yet
    assert _as_date(df.iloc[0]["maturity_date"]) == date(2026, 4, 6)
    assert pd.isna(df.iloc[0]["price_at_maturity"])


# ──────────────────────────────────────────────────────────────────────
# Maturity matches fill_outcomes — FX (CALENDAR HOURS)
# ──────────────────────────────────────────────────────────────────────


def test_fx_maturity_is_datetime_plus_n_hours(temp_db):
    """horizon='24h' → maturity = datetime_utc + 24 hours
    (matches fx/ml/predict.py::fill_outcomes interval CASE)."""
    bar_dt = datetime(2026, 5, 7, 18, 0, 0)
    mat_dt = bar_dt + timedelta(hours=24)
    temp_db.execute(
        """INSERT INTO fx_prices_hourly
           (datetime_utc, date, weekday, hour_utc, gbpeur_close)
           VALUES (?, ?, ?, ?, ?)""",
        [bar_dt, bar_dt.date(), bar_dt.strftime("%A"), bar_dt.hour, 1.16500],
    )
    temp_db.execute(
        """INSERT INTO fx_prices_hourly
           (datetime_utc, date, weekday, hour_utc, gbpeur_close)
           VALUES (?, ?, ?, ?, ?)""",
        [mat_dt, mat_dt.date(), mat_dt.strftime("%A"), mat_dt.hour, 1.16700],
    )
    temp_db.execute(
        """INSERT INTO fx_ml_predictions
            (datetime_utc, model_id, direction, horizon, predicted_probability)
           VALUES (?, 'fxm1', 'up', '24h', 0.65)""",
        [bar_dt],
    )
    df = get_fx_recent_predictions(temp_db, limit=10)
    assert df.iloc[0]["maturity_datetime"] == mat_dt
    assert df.iloc[0]["price_at_maturity"] == pytest.approx(1.16700)


def test_fx_maturity_horizon_48h(temp_db):
    bar_dt = datetime(2026, 5, 7, 18, 0, 0)
    mat_dt = bar_dt + timedelta(hours=48)
    temp_db.execute(
        """INSERT INTO fx_prices_hourly
           (datetime_utc, date, weekday, hour_utc, gbpeur_close)
           VALUES (?, ?, ?, ?, ?)""",
        [bar_dt, bar_dt.date(), bar_dt.strftime("%A"), bar_dt.hour, 1.165],
    )
    temp_db.execute(
        """INSERT INTO fx_prices_hourly
           (datetime_utc, date, weekday, hour_utc, gbpeur_close)
           VALUES (?, ?, ?, ?, ?)""",
        [mat_dt, mat_dt.date(), mat_dt.strftime("%A"), mat_dt.hour, 1.170],
    )
    temp_db.execute(
        """INSERT INTO fx_ml_predictions
            (datetime_utc, model_id, direction, horizon, predicted_probability)
           VALUES (?, 'fxm1', 'down', '48h', 0.6)""",
        [bar_dt],
    )
    df = get_fx_recent_predictions(temp_db, limit=10)
    assert df.iloc[0]["maturity_datetime"] == mat_dt
    assert df.iloc[0]["price_at_maturity"] == pytest.approx(1.170)


# ──────────────────────────────────────────────────────────────────────
# % move — pending vs filled
# ──────────────────────────────────────────────────────────────────────


def test_pct_move_equity_filled_uses_actual_max_return():
    """Filled equity prediction: % move comes from actual_max_return × 100."""
    val = pct_move_equity_or_crypto(
        actual_max_return=0.0823,
        price_at_prediction=100.0,
        current_price=110.0,
        outcome_filled=True,
    )
    # Uses actual_max_return, not current_price
    assert val == pytest.approx(8.23)


def test_pct_move_equity_pending_uses_current_price():
    """Pending: actual_max_return is None → use (current/entry − 1) × 100."""
    val = pct_move_equity_or_crypto(
        actual_max_return=None,
        price_at_prediction=100.0,
        current_price=104.5,
        outcome_filled=False,
    )
    assert val == pytest.approx(4.5)


def test_pct_move_equity_pending_negative():
    val = pct_move_equity_or_crypto(
        actual_max_return=None,
        price_at_prediction=100.0,
        current_price=97.7,
        outcome_filled=False,
    )
    assert val == pytest.approx(-2.3)


def test_pct_move_equity_no_data_returns_none():
    assert pct_move_equity_or_crypto(None, None, None, False) is None
    assert pct_move_equity_or_crypto(None, 100.0, None, False) is None


def test_pct_move_fx_filled_up_direction_positive():
    """For 'up' filled, max_pips=50 at entry 1.16482 → +50*0.0001/1.16482*100"""
    val = pct_move_fx(
        direction="up",
        actual_max_pips=50.0,
        price_at_prediction=1.16482,
        current_price=1.17000,
        outcome_filled=True,
    )
    expected = 50.0 * 0.0001 / 1.16482 * 100
    assert val == pytest.approx(expected, abs=1e-6)


def test_pct_move_fx_filled_down_direction_negative():
    val = pct_move_fx(
        direction="down",
        actual_max_pips=80.0,
        price_at_prediction=1.16482,
        current_price=1.15000,
        outcome_filled=True,
    )
    expected = -80.0 * 0.0001 / 1.16482 * 100
    assert val == pytest.approx(expected, abs=1e-6)


def test_pct_move_fx_pending_uses_current_price():
    val = pct_move_fx(
        direction="up",
        actual_max_pips=None,
        price_at_prediction=1.16500,
        current_price=1.16800,
        outcome_filled=False,
    )
    expected = (1.16800 / 1.16500 - 1) * 100
    assert val == pytest.approx(expected, abs=1e-6)


def test_format_pct_move_signs():
    assert format_pct_move(1.83) == "+1.83%"
    assert format_pct_move(-2.31) == "-2.31%"
    assert format_pct_move(0.0) == "+0.00%"
    assert format_pct_move(None) == ""


# ──────────────────────────────────────────────────────────────────────
# Time remaining — past due, near, far, filled
# ──────────────────────────────────────────────────────────────────────


def test_time_remaining_far_future_days():
    today = date(2026, 5, 1)
    tr = time_remaining_days(date(2026, 5, 21), today=today, outcome_filled=False)
    assert tr == TimeRemaining(value=20, unit="d", past_due=False)
    assert format_time_remaining(tr) == "20d"


def test_time_remaining_near_due_days():
    today = date(2026, 5, 7)
    tr = time_remaining_days(date(2026, 5, 8), today=today, outcome_filled=False)
    assert tr.value == 1 and not tr.past_due
    assert format_time_remaining(tr) == "1d"


def test_time_remaining_past_due_days():
    """Maturity in the past but outcome not yet filled → 'Past due'."""
    today = date(2026, 5, 7)
    tr = time_remaining_days(date(2026, 5, 1), today=today, outcome_filled=False)
    assert tr.past_due is True
    assert format_time_remaining(tr) == "Past due"


def test_time_remaining_filled_returns_none():
    """Filled prediction: empty cell, never 'Past due'."""
    today = date(2026, 5, 7)
    tr = time_remaining_days(date(2026, 5, 1), today=today, outcome_filled=True)
    assert tr is None
    assert format_time_remaining(tr) == ""


def test_time_remaining_no_maturity_returns_none():
    tr = time_remaining_days(None, today=date(2026, 5, 7), outcome_filled=False)
    assert tr is None
    assert format_time_remaining(tr) == ""


def test_time_remaining_hours_far_future():
    now = datetime(2026, 5, 7, 12, 0)
    tr = time_remaining_hours(datetime(2026, 5, 8, 12, 0), now_utc=now,
                                outcome_filled=False)
    assert tr == TimeRemaining(value=24, unit="h", past_due=False)
    assert format_time_remaining(tr) == "24h"


def test_time_remaining_hours_near_due():
    now = datetime(2026, 5, 7, 12, 0)
    tr = time_remaining_hours(datetime(2026, 5, 7, 18, 0), now_utc=now,
                                outcome_filled=False)
    assert tr.value == 6 and not tr.past_due


def test_time_remaining_hours_past_due():
    now = datetime(2026, 5, 7, 12, 0)
    tr = time_remaining_hours(datetime(2026, 5, 7, 6, 0), now_utc=now,
                                outcome_filled=False)
    assert tr.past_due is True
    assert format_time_remaining(tr) == "Past due"


def test_time_remaining_hours_filled():
    now = datetime(2026, 5, 7, 12, 0)
    tr = time_remaining_hours(datetime(2026, 5, 7, 6, 0), now_utc=now,
                                outcome_filled=True)
    assert tr is None


# ──────────────────────────────────────────────────────────────────────
# Pandas missing-value handling — NaT and NaN flow in from DuckDB
# ──────────────────────────────────────────────────────────────────────


def test_time_remaining_days_handles_pandas_NaT():
    """Equity predictions with no matured row yet surface as pd.NaT, not
    None. Must be treated as missing rather than raising TypeError."""
    tr = time_remaining_days(pd.NaT, today=date(2026, 5, 7), outcome_filled=False)
    assert tr is None


def test_time_remaining_hours_handles_pandas_NaT():
    tr = time_remaining_hours(pd.NaT, now_utc=datetime(2026, 5, 7, 12, 0),
                                outcome_filled=False)
    assert tr is None


def test_time_remaining_days_handles_pandas_timestamp():
    """DuckDB DATE → pandas.Timestamp; helper must accept it."""
    today = date(2026, 5, 1)
    tr = time_remaining_days(pd.Timestamp("2026-05-08"), today=today,
                                outcome_filled=False)
    assert tr is not None and tr.value == 7


def test_time_remaining_hours_handles_pandas_timestamp():
    now = datetime(2026, 5, 7, 12, 0)
    tr = time_remaining_hours(pd.Timestamp("2026-05-08 12:00:00"), now_utc=now,
                                outcome_filled=False)
    assert tr is not None and tr.value == 24


def test_pct_move_handles_NaN_inputs():
    """If price_at_prediction is NaN (no entry row), don't blow up."""
    nan = float("nan")
    assert pct_move_equity_or_crypto(nan, nan, nan, outcome_filled=True) is None
    assert pct_move_equity_or_crypto(None, nan, 100.0, outcome_filled=False) is None


def test_format_pct_move_handles_NaN():
    assert format_pct_move(float("nan")) == ""
