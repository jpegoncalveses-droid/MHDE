"""Integration test: crypto ML pipeline end-to-end with synthetic data."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from crypto.config import FEATURE_COLS
from crypto.ml.features import compute_features
from crypto.ml.labels import compute_labels
from crypto.ml.predict import score_universe, fill_outcomes

from tests.integration._helpers import (
    insert_crypto_prices,
    register_active_crypto_model,
    seed_crypto_universe,
    train_tiny_model,
)


@pytest.fixture
def crypto_pipeline_state(temp_db, synthetic_prices_crypto, tmp_path):
    """Synthetic crypto state: 5 symbols × 80 days, BTC for cross-features."""
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT"]
    seed_crypto_universe(temp_db, symbols)
    for sym in symbols:
        rows = synthetic_prices_crypto(sym, num_days=80,
                                        start_price={"BTCUSDT": 50000, "ETHUSDT": 3000,
                                                     "BNBUSDT": 400, "SOLUSDT": 100,
                                                     "ADAUSDT": 0.5}.get(sym, 1000),
                                        seed=hash(sym) % 10000)
        insert_crypto_prices(temp_db, rows)

    model_path = train_tiny_model(FEATURE_COLS, tmp_path / "crypto_model.joblib")
    register_active_crypto_model(temp_db, model_path, horizon="5d", threshold=0.10)
    return temp_db


def test_crypto_pipeline_end_to_end(crypto_pipeline_state):
    conn = crypto_pipeline_state

    # Labels + Features
    n_labels = compute_labels(conn)
    assert n_labels > 0
    n_features = compute_features(conn)
    assert n_features > 0

    # Predict
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM crypto_ml_features"
    ).fetchone()[0]
    out = score_universe(conn, latest)
    assert out.get("predictions") is not None
    pred_count = conn.execute(
        "SELECT COUNT(*) FROM crypto_ml_predictions WHERE prediction_date = ?",
        [latest],
    ).fetchone()[0]
    assert pred_count > 0

    # fill_outcomes — KI-103 regression: window must match label horizon
    earliest = conn.execute(
        "SELECT MIN(trade_date) FROM crypto_prices_daily"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ["BTCUSDT", earliest, "test_old", "5d", 0.7, 0.10],
    )
    fill_outcomes(conn)
    row = conn.execute(
        "SELECT outcome_filled_at, actual_max_return FROM crypto_ml_predictions "
        "WHERE symbol = 'BTCUSDT' AND prediction_date = ?", [earliest]
    ).fetchone()
    assert row[0] is not None  # filled
    assert row[1] is not None  # actual_max_return computed


def test_crypto_predictions_schema_parity(crypto_pipeline_state):
    """crypto_ml_predictions parallels ml_predictions (no `sector`)."""
    cols = {r[0] for r in crypto_pipeline_state.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'crypto_ml_predictions'"
    ).fetchall()}
    required = {"symbol", "prediction_date", "model_id", "horizon",
                "predicted_probability", "prediction_threshold",
                "actual_max_return", "actual_max_drawdown", "actual_hit",
                "outcome_filled_at", "market_cap_bucket"}
    assert required.issubset(cols)


def test_crypto_dashboard_query_returns_rows(crypto_pipeline_state):
    conn = crypto_pipeline_state
    compute_labels(conn)
    compute_features(conn)
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM crypto_ml_features"
    ).fetchone()[0]
    score_universe(conn, latest)

    rows = conn.execute("""
        SELECT symbol, prediction_date, predicted_probability, market_cap_bucket
        FROM crypto_ml_predictions
        WHERE prediction_date = ?
        ORDER BY predicted_probability DESC
    """, [latest]).fetchall()
    assert len(rows) > 0
