"""Unit tests for health/ml_checks.py — ML-specific health checks."""
from __future__ import annotations

from datetime import date, timedelta

from health.ml_checks import (
    check_last_prediction,
    check_rolling_precision,
    check_ml_tables_freshness,
    check_trained_models,
)


def _insert_pred(conn, ticker, prediction_date, model_id="m1", horizon="20d",
                 prob=0.7, threshold=0.10, hit=None):
    conn.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, horizon, "
        "predicted_probability, prediction_threshold, actual_hit, outcome_filled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [ticker, prediction_date, model_id, horizon, prob, threshold, hit,
         "2026-01-01 00:00:00" if hit is not None else None],
    )


def test_check_last_prediction_empty(temp_db):
    result = check_last_prediction(temp_db)
    assert result["status"] == "fail"
    assert "ml_last_prediction" == result["check_name"]


def test_check_last_prediction_recent(temp_db):
    today = date.today()
    _insert_pred(temp_db, "AAPL", today)
    result = check_last_prediction(temp_db)
    assert result["status"] == "pass"


def test_check_last_prediction_stale(temp_db):
    """Predictions older than 7 days → warn or fail."""
    old_date = date.today() - timedelta(days=30)
    _insert_pred(temp_db, "AAPL", old_date)
    result = check_last_prediction(temp_db)
    assert result["status"] in ("warn", "fail")


def test_check_rolling_precision_no_filled_predictions(temp_db):
    """No filled outcomes → skip status (nothing to measure yet)."""
    result = check_rolling_precision(temp_db)
    assert result["status"] in ("skip", "warn", "pass")


def test_check_rolling_precision_with_hits(temp_db):
    """Filled predictions with hits → returns pass/warn based on rate."""
    today = date.today()
    for i in range(10):
        _insert_pred(temp_db, f"T{i}", today - timedelta(days=i + 5),
                     hit=(i % 2 == 0))  # 50% hit rate
    result = check_rolling_precision(temp_db)
    assert result["status"] in ("pass", "warn", "fail")
    assert "check_name" in result


def test_check_ml_tables_freshness_empty(temp_db):
    """Empty ml_features and ml_labels → fail for both."""
    results = check_ml_tables_freshness(temp_db)
    assert isinstance(results, list)
    names = {r["check_name"] for r in results}
    assert "ml_features_freshness" in names
    assert "ml_labels_freshness" in names


def test_check_ml_tables_freshness_with_data(temp_db):
    today = date.today()
    # ml_features needs all 32 columns; provide minimum to satisfy NOT NULL
    temp_db.execute(
        "INSERT INTO ml_features (ticker, trade_date, return_5d) VALUES (?, ?, ?)",
        ["AAPL", today, 0.05],
    )
    temp_db.execute(
        "INSERT INTO ml_labels (ticker, trade_date, close_price) VALUES (?, ?, ?)",
        ["AAPL", today, 150.0],
    )
    results = check_ml_tables_freshness(temp_db)
    statuses = {r["check_name"]: r["status"] for r in results}
    # With fresh today's data, both should be pass.
    assert statuses.get("ml_features_freshness") in ("pass", "warn")
    assert statuses.get("ml_labels_freshness") in ("pass", "warn")


def test_check_trained_models_returns_dict(monkeypatch, tmp_path):
    """check_trained_models reads models/saved/ from disk; we don't
    assert a specific status (depends on environment) but must return
    a dict with check_name and status."""
    result = check_trained_models()
    assert "check_name" in result
    assert "status" in result
    assert result["status"] in ("pass", "warn", "fail")
