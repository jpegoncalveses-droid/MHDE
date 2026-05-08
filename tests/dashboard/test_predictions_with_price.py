"""Regression tests for the price_at_prediction JOIN used by the dashboard
prediction tabs. Each tab joins predictions to its engine's price table on
the prediction key; this test verifies that JOIN produces a non-null price
where the corresponding price row exists, and a null price (without
dropping the row) where it does not.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from dashboard.services.queries import (
    get_crypto_predictions,
    get_crypto_recent_outcomes,
    get_equity_predictions,
    get_equity_recent_outcomes,
    get_fx_recent_predictions,
)


# ──────────────────────────────────────────────────────────────────────
# Equity
# ──────────────────────────────────────────────────────────────────────


def test_equity_predictions_join_pulls_price_when_row_exists(temp_db):
    pred_date = date(2026, 5, 5)
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["p1", "AAPL", pred_date, 187.42],
    )
    temp_db.execute(
        """
        INSERT INTO ml_predictions
            (ticker, prediction_date, model_id, horizon, predicted_probability,
             prediction_threshold, sector, market_cap_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ["AAPL", pred_date, "model-1", "20d", 0.72, 0.50, "Tech", "large"],
    )

    df = get_equity_predictions(temp_db, pred_date)
    assert len(df) == 1
    assert df.iloc[0]["price_at_prediction"] == pytest.approx(187.42)


def test_equity_predictions_null_price_when_no_row(temp_db):
    """LEFT JOIN: prediction without a matching price row is preserved
    with a NULL price_at_prediction (never silently dropped)."""
    pred_date = date(2026, 5, 5)
    temp_db.execute(
        """
        INSERT INTO ml_predictions
            (ticker, prediction_date, model_id, horizon, predicted_probability,
             prediction_threshold, sector, market_cap_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ["NEWCO", pred_date, "model-1", "20d", 0.61, 0.50, "Tech", "small"],
    )
    df = get_equity_predictions(temp_db, pred_date)
    assert len(df) == 1
    assert pd.isna(df.iloc[0]["price_at_prediction"])


def test_equity_recent_outcomes_pulls_price(temp_db):
    pred_date = date(2026, 4, 10)
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["p1", "MSFT", pred_date, 411.10],
    )
    temp_db.execute(
        """
        INSERT INTO ml_predictions
            (ticker, prediction_date, model_id, horizon, predicted_probability,
             actual_max_return, actual_max_drawdown, actual_hit, outcome_filled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ["MSFT", pred_date, "m1", "20d", 0.66, 0.082, -0.014, True,
         datetime(2026, 4, 30, 12, 0)],
    )

    df = get_equity_recent_outcomes(temp_db, limit=10)
    assert len(df) == 1
    assert df.iloc[0]["price_at_prediction"] == pytest.approx(411.10)


# ──────────────────────────────────────────────────────────────────────
# Crypto
# ──────────────────────────────────────────────────────────────────────


def test_crypto_predictions_join_pulls_price(temp_db):
    pred_date = date(2026, 5, 5)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["BTCUSDT", pred_date, 102_345.67],
    )
    temp_db.execute(
        """
        INSERT INTO crypto_ml_predictions
            (symbol, prediction_date, model_id, horizon, predicted_probability,
             prediction_threshold, market_cap_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ["BTCUSDT", pred_date, "c1", "10d", 0.71, 0.50, "large"],
    )
    df = get_crypto_predictions(temp_db, pred_date)
    assert len(df) == 1
    assert df.iloc[0]["price_at_prediction"] == pytest.approx(102_345.67)


def test_crypto_predictions_null_price_when_missing(temp_db):
    pred_date = date(2026, 5, 5)
    temp_db.execute(
        """
        INSERT INTO crypto_ml_predictions
            (symbol, prediction_date, model_id, horizon, predicted_probability,
             prediction_threshold, market_cap_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ["NEWCOIN", pred_date, "c1", "10d", 0.55, 0.50, "small"],
    )
    df = get_crypto_predictions(temp_db, pred_date)
    assert len(df) == 1
    assert pd.isna(df.iloc[0]["price_at_prediction"])


def test_crypto_recent_outcomes_pulls_price(temp_db):
    pred_date = date(2026, 4, 1)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["ETHUSDT", pred_date, 3_511.90],
    )
    temp_db.execute(
        """
        INSERT INTO crypto_ml_predictions
            (symbol, prediction_date, model_id, horizon, predicted_probability,
             actual_max_return, actual_max_drawdown, actual_hit, outcome_filled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ["ETHUSDT", pred_date, "c1", "10d", 0.70, 0.06, -0.02, True,
         datetime(2026, 4, 11, 0, 0)],
    )
    df = get_crypto_recent_outcomes(temp_db, limit=10)
    assert len(df) == 1
    assert df.iloc[0]["price_at_prediction"] == pytest.approx(3_511.90)


# ──────────────────────────────────────────────────────────────────────
# FX
# ──────────────────────────────────────────────────────────────────────


def test_fx_recent_predictions_pulls_price(temp_db):
    bar_dt = datetime(2026, 5, 7, 18, 0, 0)
    temp_db.execute(
        """
        INSERT INTO fx_prices_hourly
            (datetime_utc, date, weekday, hour_utc, gbpeur_close)
        VALUES (?, ?, ?, ?, ?)
        """,
        [bar_dt, bar_dt.date(), bar_dt.strftime("%A"), bar_dt.hour, 1.16482],
    )
    temp_db.execute(
        """
        INSERT INTO fx_ml_predictions
            (datetime_utc, model_id, direction, horizon, predicted_probability,
             prediction_threshold)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [bar_dt, "fxm1", "up", "24h", 0.68, 0.50],
    )
    df = get_fx_recent_predictions(temp_db, limit=10)
    assert len(df) == 1
    assert df.iloc[0]["price_at_prediction"] == pytest.approx(1.16482)


def test_fx_recent_predictions_null_price_when_missing(temp_db):
    bar_dt = datetime(2026, 5, 7, 18, 0, 0)
    temp_db.execute(
        """
        INSERT INTO fx_ml_predictions
            (datetime_utc, model_id, direction, horizon, predicted_probability,
             prediction_threshold)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [bar_dt, "fxm1", "down", "48h", 0.55, 0.50],
    )
    df = get_fx_recent_predictions(temp_db, limit=10)
    assert len(df) == 1
    assert pd.isna(df.iloc[0]["price_at_prediction"])
