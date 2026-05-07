"""Integration test: equity ML pipeline end-to-end with synthetic data.

The pipeline orchestration we exercise here:
    backfill labels -> backfill features -> register tiny model
                  -> run_prediction_pipeline -> dashboard query

We assert structural completeness — predictions written, outcomes
filled where the window has elapsed, schema parity with what the
dashboard reads. Precision metrics are not asserted because a model
trained on synthetic random-walk data has no predictive signal by
construction.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from ml.features import compute_features
from ml.labels import compute_labels
from ml.predict import score_universe, fill_outcomes
from ml.train import FEATURE_COLS

from tests.integration._helpers import (
    insert_equity_prices,
    register_active_equity_model,
    seed_active_company,
    train_tiny_model,
)


@pytest.fixture
def equity_pipeline_state(temp_db, synthetic_prices_equity, tmp_path):
    """50 tickers × 220 days of synthetic prices + a tiny active model."""
    tickers = [f"TKR{i:03d}" for i in range(50)]
    for t in tickers:
        seed_active_company(temp_db, t, market_cap=100e9)
    for t in tickers:
        rows = synthetic_prices_equity(t, num_days=220, seed=hash(t) % 10000)
        insert_equity_prices(temp_db, rows)

    model_path = train_tiny_model(FEATURE_COLS, tmp_path / "equity_model.joblib")
    register_active_equity_model(temp_db, model_path, horizon="20d",
                                 label_col="label_20d_10pct", threshold=0.10)
    return temp_db


def test_equity_pipeline_end_to_end(equity_pipeline_state):
    conn = equity_pipeline_state

    # 1. Labels
    n_labels = compute_labels(conn)
    assert n_labels > 0

    # 2. Features
    n_features = compute_features(conn)
    assert n_features > 0

    # 3. Predict — score the latest feature date
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM ml_features"
    ).fetchone()[0]
    out = score_universe(conn, latest)
    assert out.get("status") != "error", out
    pred_count = conn.execute(
        "SELECT COUNT(*) FROM ml_predictions WHERE prediction_date = ?", [latest]
    ).fetchone()[0]
    assert pred_count > 0

    # 4. fill_outcomes — for predictions made deep in the past where the
    #    20d window has elapsed.
    earliest_label = conn.execute(
        "SELECT MIN(trade_date) FROM ml_labels"
    ).fetchone()[0]
    # Insert a synthetic past prediction at the earliest label date.
    conn.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        ["TKR000", earliest_label, "test_old", "20d", 0.7, 0.10],
    )
    fill_outcomes(conn)

    # The old prediction should be filled (>20 trading days elapsed).
    row = conn.execute(
        "SELECT outcome_filled_at FROM ml_predictions "
        "WHERE ticker = 'TKR000' AND prediction_date = ?", [earliest_label]
    ).fetchone()
    assert row[0] is not None


def test_equity_predictions_match_ml_predictions_schema(equity_pipeline_state):
    """The dashboard reads ml_predictions with a fixed column set; assert
    the pipeline wrote rows that match."""
    conn = equity_pipeline_state
    compute_labels(conn)
    compute_features(conn)
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM ml_features"
    ).fetchone()[0]
    score_universe(conn, latest)

    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'ml_predictions'"
    ).fetchall()}
    required = {"ticker", "prediction_date", "model_id", "horizon",
                "predicted_probability", "prediction_threshold",
                "actual_max_return", "actual_max_drawdown", "actual_hit",
                "outcome_filled_at"}
    assert required.issubset(cols), f"missing: {required - cols}"


def test_equity_dashboard_query_returns_rows(equity_pipeline_state):
    """The dashboard query for ML predictions must return rows after
    a successful pipeline run. Stand-in for assert_dashboard_renders
    until that helper is fully implemented (Session 4 deliverable)."""
    conn = equity_pipeline_state
    compute_labels(conn)
    compute_features(conn)
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM ml_features"
    ).fetchone()[0]
    score_universe(conn, latest)

    # Query mimicking what dashboard/services/queries.py does for the
    # ML predictions tab.
    rows = conn.execute("""
        SELECT p.ticker, p.prediction_date, p.predicted_probability,
               p.sector, p.market_cap_bucket
        FROM ml_predictions p
        WHERE p.prediction_date = ?
        ORDER BY p.predicted_probability DESC
    """, [latest]).fetchall()
    assert len(rows) > 0
    # All rows have real probabilities
    for r in rows:
        assert r[2] is not None
        assert 0 <= r[2] <= 1
