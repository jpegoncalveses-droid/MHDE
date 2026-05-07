"""Unit tests for fx/ml/predict.py — score_bar + fill_outcomes.

Coverage strategy:
  - score_bar requires real joblib model files. We mock joblib.load to
    return a fake model so we can exercise the SQL/insertion path
    without committing to a model artifact.
  - fill_outcomes is pure SQL over fx_ml_predictions × fx_prices_hourly,
    so it tests cleanly with synthetic data.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from fx.config import FEATURE_COLS, PIP_SIZE
from fx.ml import predict as predict_mod


# ──────────────────────────────────────────────────────────────────────
# score_bar
# ──────────────────────────────────────────────────────────────────────


def test_score_bar_no_active_models(temp_db):
    """With no rows in fx_ml_model_runs, score_bar returns an empty result."""
    out = predict_mod.score_bar(temp_db)
    assert out["predictions"] == {}
    assert out["price"] is None


def test_score_bar_skips_when_no_features(temp_db, monkeypatch):
    """Even with active models, missing features for the bar → empty result."""
    temp_db.execute(
        "INSERT INTO fx_ml_model_runs (model_id, direction, horizon, target_pips, model_path, is_active) "
        "VALUES ('m1', 'up', '24h', 20, '/tmp/fake.joblib', true)"
    )
    out = predict_mod.score_bar(temp_db, datetime(2026, 5, 7, 12, 0, 0))
    assert out["predictions"] == {}


def test_score_bar_writes_prediction_and_returns_probability(
    temp_db, monkeypatch, synthetic_prices_fx
):
    """Happy path: features exist, model is mocked → row in fx_ml_predictions."""
    bar_dt = datetime(2026, 5, 7, 12, 0, 0)

    # Active model
    temp_db.execute(
        "INSERT INTO fx_ml_model_runs (model_id, direction, horizon, target_pips, model_path, is_active) "
        "VALUES ('m1', 'up', '24h', 20, '/tmp/fake.joblib', true)"
    )

    # Features row
    feature_cols_sql = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    temp_db.execute(
        f"INSERT INTO fx_ml_features (datetime_utc, {feature_cols_sql}) "
        f"VALUES (?, {placeholders})",
        [bar_dt] + [0.0] * len(FEATURE_COLS),
    )

    # Price row (so price comes back non-None)
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
        "gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [bar_dt, bar_dt.date(), bar_dt.strftime("%A"), bar_dt.hour,
         1.18, 1.181, 1.179, 1.180, 100, "OK"],
    )

    # Mock joblib.load → bundle with fake model + Platt + medians
    fake_model = MagicMock()
    fake_model.predict_proba = lambda X: np.array([[0.3, 0.7]])
    fake_platt = MagicMock()
    fake_platt.predict_proba = lambda X: np.array([[0.25, 0.75]])
    monkeypatch.setattr(
        predict_mod.joblib, "load",
        lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}},
    )

    out = predict_mod.score_bar(temp_db, bar_dt)
    assert "up_24h" in out["predictions"]
    assert out["predictions"]["up_24h"]["probability"] == pytest.approx(0.75)
    assert out["price"] == pytest.approx(1.180, rel=1e-6)

    rows = temp_db.execute(
        "SELECT predicted_probability FROM fx_ml_predictions WHERE datetime_utc = ?",
        [bar_dt],
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(0.75)


# ──────────────────────────────────────────────────────────────────────
# fill_outcomes
# ──────────────────────────────────────────────────────────────────────


def _seed_prices_with_known_move(conn, anchor: datetime, peak_pips: int):
    """Seed 60h of prices: flat at 1.18 except a peak at +60 pips at hour 5
    after anchor. Used to give fill_outcomes deterministic max_up_pips."""
    base = 1.18
    for h in range(60):
        dt = anchor + timedelta(hours=h)
        if h == 5:
            high = base + (peak_pips * PIP_SIZE)
            low = base - 0.0001
        else:
            high = base + 0.0001
            low = base - 0.0001
        conn.execute(
            "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
            "gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [dt, dt.date(), dt.strftime("%A"), dt.hour,
             base, high, low, base, 100, "OK"],
        )


def test_fill_outcomes_24h_horizon_up_direction(temp_db):
    anchor = datetime(2026, 5, 1, 12, 0, 0)
    _seed_prices_with_known_move(temp_db, anchor, peak_pips=60)

    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        [anchor, "m_up_24h", "up", "24h", 0.7, 20],
    )

    predict_mod.fill_outcomes(temp_db)

    row = temp_db.execute(
        "SELECT actual_max_pips, actual_hit FROM fx_ml_predictions "
        "WHERE datetime_utc = ?", [anchor]
    ).fetchone()
    assert row[0] == pytest.approx(60.0, rel=1e-3)
    assert row[1] is True  # 60 pips >= 20 threshold


def test_fill_outcomes_leaves_recent_predictions_unfilled(temp_db, synthetic_prices_fx):
    """A prediction made within the last 24h has no full forward window;
    fill_outcomes must leave outcome_filled_at NULL."""
    rows = synthetic_prices_fx(num_hours=10)
    temp_db.executemany(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
        "gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["datetime_utc"], r["date"], r["weekday"], r["hour_utc"],
          r["gbpeur_open"], r["gbpeur_high"], r["gbpeur_low"], r["gbpeur_close"],
          r["tick_count"], r["data_quality"]) for r in rows],
    )

    pred_dt = rows[-1]["datetime_utc"]  # latest bar — no 24h forward
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        [pred_dt, "m1", "up", "24h", 0.6, 20],
    )

    predict_mod.fill_outcomes(temp_db)

    row = temp_db.execute(
        "SELECT outcome_filled_at FROM fx_ml_predictions WHERE datetime_utc = ?",
        [pred_dt],
    ).fetchone()
    assert row[0] is None


def test_fill_outcomes_idempotent(temp_db):
    """Running fill_outcomes twice gives the same result."""
    anchor = datetime(2026, 5, 1, 12, 0, 0)
    _seed_prices_with_known_move(temp_db, anchor, peak_pips=40)
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        [anchor, "m1", "up", "24h", 0.6, 20],
    )

    predict_mod.fill_outcomes(temp_db)
    first = temp_db.execute(
        "SELECT actual_max_pips, actual_hit FROM fx_ml_predictions WHERE datetime_utc = ?",
        [anchor],
    ).fetchone()
    predict_mod.fill_outcomes(temp_db)
    second = temp_db.execute(
        "SELECT actual_max_pips, actual_hit FROM fx_ml_predictions WHERE datetime_utc = ?",
        [anchor],
    ).fetchone()
    assert first[0] == pytest.approx(second[0])
    assert first[1] == second[1]
