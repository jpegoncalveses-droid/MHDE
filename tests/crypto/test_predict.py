"""Unit tests for crypto/ml/predict.py — score_universe + fill_outcomes."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from crypto.config import FEATURE_COLS
from crypto.ml import predict as predict_mod


def _insert_features_for_date(conn, symbol, prediction_date, feature_values=None):
    feature_values = feature_values or [0.0] * len(FEATURE_COLS)
    cols = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    conn.execute(
        f"INSERT INTO crypto_ml_features (symbol, trade_date, {cols}) "
        f"VALUES (?, ?, {placeholders})",
        [symbol, prediction_date] + feature_values,
    )


# ──────────────────────────────────────────────────────────────────────
# score_universe
# ──────────────────────────────────────────────────────────────────────


def test_score_universe_no_active_models(temp_db):
    out = predict_mod.score_universe(temp_db)
    assert out["predictions"] == []
    assert out["regime"] == "unknown"


def test_score_universe_no_features_for_date(temp_db):
    """Active model exists but no features for the date → empty."""
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '5d', 0.10, '/tmp/fake.joblib', true)"
    )
    out = predict_mod.score_universe(temp_db, date(2026, 5, 7))
    assert out["predictions"] == []


def test_score_universe_writes_predictions_above_threshold(temp_db, monkeypatch):
    """Mock joblib model returns probability 0.8 → row in crypto_ml_predictions
    above the LOW_THRESHOLD (0.50)."""
    pred_date = date(2026, 5, 7)
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '5d', 0.10, '/tmp/fake.joblib', true)"
    )
    _insert_features_for_date(temp_db, "BTCUSDT", pred_date)

    fake_model = MagicMock()
    fake_model.predict_proba = lambda X: np.array([[0.2, 0.8]])
    fake_platt = MagicMock()
    fake_platt.predict_proba = lambda X: np.array([[0.18, 0.82]])
    monkeypatch.setattr(
        predict_mod.joblib, "load",
        lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}},
    )

    out = predict_mod.score_universe(temp_db, pred_date)
    assert len(out["predictions"]) == 1
    assert out["predictions"][0]["symbol"] == "BTCUSDT"
    assert out["predictions"][0]["predicted_probability"] == pytest.approx(0.82)


def test_score_universe_filters_low_probability(temp_db, monkeypatch):
    """Predictions below LOW_THRESHOLD (0.50) are dropped before write."""
    pred_date = date(2026, 5, 7)
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '5d', 0.10, '/tmp/fake.joblib', true)"
    )
    _insert_features_for_date(temp_db, "WEAKCOIN", pred_date)

    fake_model = MagicMock()
    fake_model.predict_proba = lambda X: np.array([[0.7, 0.3]])
    fake_platt = MagicMock()
    fake_platt.predict_proba = lambda X: np.array([[0.65, 0.35]])
    monkeypatch.setattr(
        predict_mod.joblib, "load",
        lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}},
    )

    out = predict_mod.score_universe(temp_db, pred_date)
    assert out["predictions"] == []


def test_score_universe_regime_classification(temp_db, monkeypatch):
    """High-conviction prediction count maps to regime label."""
    pred_date = date(2026, 5, 7)
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '5d', 0.10, '/tmp/fake.joblib', true)"
    )
    # 10 features rows; mock returns 0.8 for all → all above threshold
    for i in range(10):
        _insert_features_for_date(temp_db, f"COIN{i}USDT", pred_date)

    fake_model = MagicMock()
    fake_model.predict_proba = lambda X: np.array([[0.2, 0.8]] * len(X))
    fake_platt = MagicMock()
    fake_platt.predict_proba = lambda X: np.array([[0.2, 0.8]] * len(X))
    monkeypatch.setattr(
        predict_mod.joblib, "load",
        lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}},
    )

    out = predict_mod.score_universe(temp_db, pred_date)
    # 10/10 predictions ≥ 0.60 → 100% > 30% → high_activity
    assert out["regime"] == "high_activity"


# ──────────────────────────────────────────────────────────────────────
# fill_outcomes
# ──────────────────────────────────────────────────────────────────────


def _seed_prices_with_window(conn, symbol, anchor_date, n_days, peak_pct=0.20):
    """Seed n_days of prices with a peak `peak_pct` above entry on day 3."""
    base = 100.0
    for i in range(n_days):
        d = anchor_date + timedelta(days=i)
        if i == 3:
            high = base * (1 + peak_pct)
            low = base * 0.99
            close = base * (1 + peak_pct)
        else:
            high = base * 1.001
            low = base * 0.999
            close = base
        conn.execute(
            "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, "
            "close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [symbol, d, base, high, low, close, 1e8],
        )


def test_fill_outcomes_5d_horizon_hits_threshold(temp_db):
    anchor = date(2026, 4, 1)
    _seed_prices_with_window(temp_db, "BTCUSDT", anchor, n_days=10, peak_pct=0.20)

    temp_db.execute(
        "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        ["BTCUSDT", anchor, "m1", "5d", 0.7, 0.10],
    )

    predict_mod.fill_outcomes(temp_db)

    row = temp_db.execute(
        "SELECT actual_max_return, actual_hit FROM crypto_ml_predictions "
        "WHERE symbol = 'BTCUSDT' AND prediction_date = ?", [anchor]
    ).fetchone()
    assert row[0] == pytest.approx(0.20, rel=1e-3)
    assert row[1] is True  # 20% >= 10% threshold


def test_fill_outcomes_does_not_overfill(temp_db):
    """fill_outcomes only updates rows where outcome_filled_at IS NULL."""
    anchor = date(2026, 4, 1)
    _seed_prices_with_window(temp_db, "BTCUSDT", anchor, n_days=10, peak_pct=0.05)

    temp_db.execute(
        "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, horizon, "
        "predicted_probability, prediction_threshold, outcome_filled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        ["BTCUSDT", anchor, "m1", "5d", 0.7, 0.10],
    )

    # First call: row already filled, so outcome stays as-is (NULLs would
    # not be re-evaluated).
    predict_mod.fill_outcomes(temp_db)
    row = temp_db.execute(
        "SELECT actual_hit FROM crypto_ml_predictions "
        "WHERE symbol = 'BTCUSDT'"
    ).fetchone()
    # actual_hit was never set since outcome_filled_at was already non-NULL
    # but the UPDATE WHERE clause filters on outcome_filled_at IS NULL
    # → row should remain in pre-fill state.
    assert row[0] is None
