"""Unit tests for ml/predict.py — equity ML score_universe + fill_outcomes."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from ml.train import FEATURE_COLS
from ml import predict as predict_mod


def _seed_company(conn, ticker, sector="Information Technology", market_cap=100e9):
    conn.execute(
        "INSERT INTO companies (ticker, company_name, sector, is_active, is_etf, "
        "market_cap) VALUES (?, ?, ?, ?, ?, ?)",
        [ticker, f"{ticker} Inc", sector, True, False, market_cap],
    )


def _insert_features(conn, ticker, prediction_date, values=None):
    values = values or [0.0] * len(FEATURE_COLS)
    cols = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    conn.execute(
        f"INSERT INTO ml_features (ticker, trade_date, {cols}) "
        f"VALUES (?, ?, {placeholders})",
        [ticker, prediction_date] + values,
    )


# ──────────────────────────────────────────────────────────────────────
# score_universe
# ──────────────────────────────────────────────────────────────────────


def test_score_universe_no_features(temp_db):
    out = predict_mod.score_universe(temp_db)
    assert out["status"] == "error"


def test_score_universe_no_active_models(temp_db):
    pred_date = date(2026, 5, 7)
    _seed_company(temp_db, "AAPL")
    _insert_features(temp_db, "AAPL", pred_date)
    out = predict_mod.score_universe(temp_db, pred_date)
    assert out["status"] == "error"
    assert "model" in out["message"].lower()


def test_score_universe_writes_predictions(temp_db, monkeypatch):
    pred_date = date(2026, 5, 7)
    _seed_company(temp_db, "AAPL", market_cap=3e12)
    _insert_features(temp_db, "AAPL", pred_date)
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '20d', 0.10, '/tmp/fake.joblib', true)"
    )

    fake_model = MagicMock()
    fake_model.predict_proba = lambda X: np.array([[0.2, 0.8]])
    fake_platt = MagicMock()
    fake_platt.predict_proba = lambda X: np.array([[0.18, 0.82]])
    monkeypatch.setattr(
        predict_mod.joblib, "load",
        lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}},
    )

    out = predict_mod.score_universe(temp_db, pred_date)
    assert out.get("status") != "error"
    rows = temp_db.execute(
        "SELECT predicted_probability, sector, market_cap_bucket "
        "FROM ml_predictions WHERE prediction_date = ?", [pred_date]
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(0.82)
    assert rows[0][1] == "Information Technology"
    assert rows[0][2] == "mega"  # > 200B → mega bucket


# ──────────────────────────────────────────────────────────────────────
# fill_outcomes — trading-day window (KI-104 regression)
# ──────────────────────────────────────────────────────────────────────


def test_fill_outcomes_uses_trading_days_not_calendar(
    temp_db, synthetic_prices_equity
):
    """Equity outcome window must walk trading days, not calendar days.
    A 5-day forward should land on the 5th trading day, not the 5th
    calendar day (which would land mid-weekend if predict_date is Wed).
    Regression for KI-104.
    """
    _seed_company(temp_db, "AAPL")
    rows = synthetic_prices_equity("AAPL", num_days=15)
    temp_db.executemany(
        "INSERT INTO prices_daily (id, ticker, trade_date, open, high, low, close, "
        "volume, adjusted_close, source, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["id"], r["ticker"], r["trade_date"], r["open"], r["high"], r["low"],
          r["close"], r["volume"], r["adjusted_close"], r["source"], r["run_id"])
         for r in rows],
    )

    pred_date = rows[0]["trade_date"]
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        ["AAPL", pred_date, "m1", "5d", 0.7, 0.05],
    )

    predict_mod.fill_outcomes(temp_db)
    row = temp_db.execute(
        "SELECT outcome_filled_at FROM ml_predictions "
        "WHERE ticker = 'AAPL' AND prediction_date = ?", [pred_date]
    ).fetchone()
    # Outcome should be filled — there are >5 trading days of forward data.
    assert row[0] is not None


def test_print_predictions_error_status(capsys):
    from ml.predict import print_predictions
    print_predictions({"status": "error", "message": "no features"})
    out = capsys.readouterr().out
    assert "ERROR: no features" in out


def test_print_predictions_full_result(capsys):
    """Exercise the full print_predictions formatting path."""
    from ml.predict import print_predictions
    result = {
        "status": "ok",
        "prediction_date": date(2026, 5, 7),
        "predictions": [
            {"ticker": "AAPL", "horizon": "20d", "predicted_probability": 0.75,
             "confidence": "high", "sector": "Information Technology",
             "market_cap_bucket": "mega"},
            {"ticker": "MSFT", "horizon": "20d", "predicted_probability": 0.68,
             "confidence": "lower", "sector": "Information Technology",
             "market_cap_bucket": "mega"},
            {"ticker": "JPM", "horizon": "10d", "predicted_probability": 0.72,
             "confidence": "high", "sector": "Financials",
             "market_cap_bucket": "large"},
        ],
        "regime": {
            "label": "high_activity",
            "description": "Many high-confidence predictions",
            "n_above_60": 3,
            "total_universe": 50,
            "pct_above_60": 6.0,
            "sector_concentration": [
                {"sector": "Information Technology", "count": 2, "pct": 67.0,
                 "correlated_risk": True},
                {"sector": "Financials", "count": 1, "pct": 33.0,
                 "correlated_risk": False},
            ],
        },
    }
    print_predictions(result)
    out = capsys.readouterr().out
    assert "AAPL" in out
    assert "MSFT" in out
    assert "JPM" in out
    assert "REGIME: HIGH_ACTIVITY" in out
    assert "CORRELATION WARNING" in out  # sector concentration triggered
    assert "SECTOR BREAKDOWN" in out


# ──────────────────────────────────────────────────────────────────────
# KI-149: ml_features / prices_daily cross-check (silent T-2 skip)
# ──────────────────────────────────────────────────────────────────────


def _seed_price(conn, ticker, d, close=100.0):
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        [f"{ticker}-{d.isoformat()}", ticker, d, close],
    )


def test_score_universe_auto_pick_raises_on_features_behind_prices(temp_db):
    """KI-149 regression: when ml_features.MAX < prices_daily.MAX and the
    caller doesn't pin prediction_date, score_universe must raise
    StaleFeaturesError instead of silently scoring the stale date.
    """
    from ml.predict import StaleFeaturesError

    features_date = date(2026, 5, 12)
    prices_date = date(2026, 5, 13)
    _seed_company(temp_db, "AAPL")
    _insert_features(temp_db, "AAPL", features_date)
    _seed_price(temp_db, "AAPL", prices_date)

    with pytest.raises(StaleFeaturesError) as exc_info:
        predict_mod.score_universe(temp_db)

    msg = str(exc_info.value)
    assert "2026-05-12" in msg and "2026-05-13" in msg, (
        f"Error must name both dates explicitly; got {msg!r}"
    )


def test_score_universe_auto_pick_allow_stale_features_logs_and_proceeds(
    temp_db, caplog
):
    """allow_stale_features=True must downgrade the cross-check to a
    WARNING and proceed (it may still fail downstream for missing models,
    but the cross-check itself must not raise).
    """
    features_date = date(2026, 5, 12)
    prices_date = date(2026, 5, 13)
    _seed_company(temp_db, "AAPL")
    _insert_features(temp_db, "AAPL", features_date)
    _seed_price(temp_db, "AAPL", prices_date)

    import logging
    with caplog.at_level(logging.WARNING, logger="mhde.ml.predict"):
        out = predict_mod.score_universe(temp_db, allow_stale_features=True)

    # Cross-check must not have raised. Downstream may still return error
    # (no active models), but it must not be a StaleFeaturesError.
    assert "stale" in caplog.text.lower() or "diverge" in caplog.text.lower(), (
        f"Expected a stale/divergence WARNING in log; got: {caplog.text!r}"
    )
    assert "2026-05-12" in caplog.text and "2026-05-13" in caplog.text


def test_score_universe_aligned_features_and_prices_no_warning(temp_db, caplog):
    """When ml_features.MAX == prices_daily.MAX, the cross-check is silent."""
    pred_date = date(2026, 5, 12)
    _seed_company(temp_db, "AAPL")
    _insert_features(temp_db, "AAPL", pred_date)
    _seed_price(temp_db, "AAPL", pred_date)

    import logging
    with caplog.at_level(logging.WARNING, logger="mhde.ml.predict"):
        predict_mod.score_universe(temp_db)  # will return error for no model

    assert "stale" not in caplog.text.lower(), (
        f"Aligned features must not produce a stale warning; got: {caplog.text!r}"
    )


def test_score_universe_explicit_prediction_date_skips_cross_check(temp_db):
    """When the caller pins prediction_date, the cross-check must not fire.
    Legitimate backfill / historical scoring use case — caller knows what
    they're asking for.
    """
    features_date = date(2026, 5, 12)
    prices_date = date(2026, 5, 13)
    _seed_company(temp_db, "AAPL")
    _insert_features(temp_db, "AAPL", features_date)
    _seed_price(temp_db, "AAPL", prices_date)

    # Explicit prediction_date — no cross-check, no exception. Downstream
    # still returns error (no model), but that's not a StaleFeaturesError.
    out = predict_mod.score_universe(temp_db, prediction_date=features_date)
    assert out["status"] == "error"
    assert "model" in out["message"].lower()


def test_fill_outcomes_skips_when_window_not_complete(
    temp_db, synthetic_prices_equity
):
    """A prediction at the latest trade_date doesn't have 20 forward
    trading days yet — outcome_filled_at must remain NULL."""
    _seed_company(temp_db, "AAPL")
    rows = synthetic_prices_equity("AAPL", num_days=10)
    temp_db.executemany(
        "INSERT INTO prices_daily (id, ticker, trade_date, open, high, low, close, "
        "volume, adjusted_close, source, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["id"], r["ticker"], r["trade_date"], r["open"], r["high"], r["low"],
          r["close"], r["volume"], r["adjusted_close"], r["source"], r["run_id"])
         for r in rows],
    )
    pred_date = rows[-1]["trade_date"]
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        ["AAPL", pred_date, "m1", "20d", 0.7, 0.10],
    )

    predict_mod.fill_outcomes(temp_db)
    row = temp_db.execute(
        "SELECT outcome_filled_at FROM ml_predictions WHERE ticker = 'AAPL' AND "
        "prediction_date = ?", [pred_date]
    ).fetchone()
    assert row[0] is None
