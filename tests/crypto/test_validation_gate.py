"""Tests for crypto/ml/validation_gate.py.

Four test cases after dropping the Sharpe arm:

1. test_first_model_skip
2. test_gate_passes_at_hit_rate_parity
3. test_gate_fails_on_hit_rate
4. test_gate_zero_old_hit_rate_permissive

Test infrastructure
-------------------
* Uses the ``temp_db`` fixture from tests/conftest.py (in-memory DuckDB
  with all production schemas).
* Hit rates are seeded via ``precision_at_threshold`` in
  ``crypto_ml_model_runs`` rows, which ``_get_hit_rate`` returns
  directly (no fallback query needed).
"""
from __future__ import annotations

from datetime import date

import pytest

from crypto.ml.validation_gate import ValidationResult, validate_promotion


# ──────────────────────────────────────────────────────────────────────
# Shared seed helpers
# ──────────────────────────────────────────────────────────────────────


def _insert_model_run(
    conn,
    *,
    model_id: str,
    horizon: str = "10d",
    is_active: bool = False,
    precision_at_threshold: float | None = None,
) -> None:
    """Insert a minimal crypto_ml_model_runs row."""
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs (
            model_id, horizon, target_threshold,
            train_start, train_end, test_start, test_end,
            n_train_samples, n_test_samples, n_positive_train, n_positive_test,
            precision_at_threshold, recall_at_threshold, f1_score, auc_roc,
            base_rate, lift_over_base, feature_importance_json,
            model_path, is_active
        ) VALUES (?, ?, 0.10,
                  '2024-01-01', '2025-01-01', '2025-01-02', '2025-01-31',
                  1000, 100, 100, 10,
                  ?, 0.5, 0.4, 0.75,
                  0.15, 2.0, '{}',
                  NULL, ?)
        """,
        [model_id, horizon, precision_at_threshold, is_active],
    )


def _insert_prediction(
    conn,
    *,
    model_id: str,
    horizon: str,
    symbol: str,
    prediction_date: date,
    predicted_probability: float,
    actual_max_return: float | None,
    actual_hit: bool | None,
) -> None:
    conn.execute(
        """
        INSERT INTO crypto_ml_predictions
            (model_id, horizon, symbol, prediction_date,
             predicted_probability, prediction_threshold,
             actual_max_return, actual_hit, outcome_filled_at)
        VALUES (?, ?, ?, ?, ?, 0.10, ?, ?,
                CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE NULL END)
        """,
        [
            model_id, horizon, symbol, prediction_date,
            predicted_probability,
            actual_max_return, actual_hit,
            actual_max_return,  # used for CASE condition
        ],
    )


# ──────────────────────────────────────────────────────────────────────
# 1. First model skip
# ──────────────────────────────────────────────────────────────────────


def test_first_model_skip(temp_db):
    """No prior active model → passed=True, reason='first_model_skip'."""
    _insert_model_run(
        temp_db,
        model_id="crypto_10d_newmodel",
        horizon="10d",
        is_active=False,
        precision_at_threshold=0.50,
    )

    result = validate_promotion(temp_db, "crypto_10d_newmodel", "10d")

    assert isinstance(result, ValidationResult)
    assert result.passed is True
    assert result.reason == "first_model_skip"
    assert result.duration_sec > 0


# ──────────────────────────────────────────────────────────────────────
# 2. Gate passes when new model is at hit-rate parity
# ──────────────────────────────────────────────────────────────────────


def test_gate_passes_at_hit_rate_parity(temp_db):
    """Old hit_rate=0.50; new hit_rate=0.50 → PASS (0.50 >= 0.9 * 0.50)."""
    old_id = "crypto_10d_oldmodel"
    new_id = "crypto_10d_newmodel"

    _insert_model_run(
        temp_db,
        model_id=old_id,
        horizon="10d",
        is_active=True,
        precision_at_threshold=0.50,
    )
    _insert_model_run(
        temp_db,
        model_id=new_id,
        horizon="10d",
        is_active=False,
        precision_at_threshold=0.50,
    )

    result = validate_promotion(temp_db, new_id, "10d")

    assert result.passed is True
    assert result.reason is None
    assert result.comparison["passed_hit_rate"] is True


# ──────────────────────────────────────────────────────────────────────
# 3. Gate fails on hit rate
# ──────────────────────────────────────────────────────────────────────


def test_gate_fails_on_hit_rate(temp_db):
    """New hit_rate=0.40 < 0.9 * 0.50 = 0.45 → FAIL, reason='hit_rate_below_threshold'."""
    old_id = "crypto_10d_oldmodel"
    new_id = "crypto_10d_newmodel"

    _insert_model_run(
        temp_db,
        model_id=old_id,
        horizon="10d",
        is_active=True,
        precision_at_threshold=0.50,
    )
    _insert_model_run(
        temp_db,
        model_id=new_id,
        horizon="10d",
        is_active=False,
        precision_at_threshold=0.40,  # 0.80 * old → below 0.9 floor
    )

    result = validate_promotion(temp_db, new_id, "10d")

    assert result.passed is False
    assert result.reason == "hit_rate_below_threshold"
    assert result.comparison["passed_hit_rate"] is False


# ──────────────────────────────────────────────────────────────────────
# 4. Zero old hit rate — permissive edge case
# ──────────────────────────────────────────────────────────────────────


def test_gate_zero_old_hit_rate_permissive(temp_db):
    """When old_hit_rate=0.0 (degenerate baseline), the hit-rate arm passes.

    Policy rationale: if the previously-active model had old_hit_rate <= 0,
    a multiplicative floor of 0.9 * 0.0 = 0.0 would trivially pass anything
    >= 0, and a negative baseline is even more meaningless.  Rather than
    applying a meaningless floor the arm is treated as *passing* — there is
    no positive baseline to defend.  This is intentionally more permissive
    and applies only in bootstrap-like edge cases.
    """
    old_id = "crypto_10d_oldmodel"
    new_id = "crypto_10d_newmodel"

    _insert_model_run(
        temp_db,
        model_id=old_id,
        horizon="10d",
        is_active=True,
        precision_at_threshold=0.0,  # degenerate baseline
    )
    _insert_model_run(
        temp_db,
        model_id=new_id,
        horizon="10d",
        is_active=False,
        precision_at_threshold=0.40,
    )

    result = validate_promotion(temp_db, new_id, "10d")

    # old_hit_rate = 0.0 → arm is treated as PASS (degenerate-baseline rule).
    assert result.comparison["passed_hit_rate"] is True
    # No positive baseline to defend → overall gate passes.
    assert result.passed is True
    assert result.reason is None
    # hit_rate_floor should be None (not computed when baseline is degenerate).
    assert result.comparison["thresholds"]["hit_rate_floor"] is None
