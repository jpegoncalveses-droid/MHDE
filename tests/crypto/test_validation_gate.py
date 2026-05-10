"""Tests for crypto/ml/validation_gate.py.

Seven required test cases (see task spec):

1. test_first_model_skip
2. test_gate_passes_when_new_model_at_parity
3. test_gate_fails_on_hit_rate
4. test_gate_fails_on_sharpe
5. test_gate_fails_both_arms
6. test_gate_timeout
7. test_gate_zero_old_sharpe_permissive

Test infrastructure
-------------------
* Uses the ``temp_db`` fixture from tests/conftest.py (in-memory DuckDB
  with all production schemas).
* The backfill entrypoint (``crypto.ml.validation_gate._run_backfill``)
  is monkeypatched to a no-op in every test except test_gate_timeout,
  where it sleeps to trigger the timeout path.
* Predictions are seeded directly under the model_id so that
  ``compute_walkfold_trade_sharpe`` and ``_get_hit_rate`` return
  controlled values.
"""
from __future__ import annotations

import math
import time
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


def _seed_sharpe_predictions(
    conn,
    model_id: str,
    horizon: str,
    *,
    ret: float = 0.10,
    hit: bool = True,
) -> None:
    """Seed 3 prediction dates × 3 symbols with scaled ``actual_max_return``.

    Uses a fixed multiplier pattern so that the mean/std ratio (Sharpe)
    scales with ``ret``.  The per-date returns are:
      date 1: [ret, ret*0.8, ret*0.6]   → mean=0.8*ret
      date 2: [ret*1.2, ret*0.9, ret*0.7] → mean=0.933*ret
      date 3: [ret*1.5, ret*0.5, ret*1.1] → mean=1.033*ret

    Scaling all returns by a constant k multiplies both mean and std by k,
    leaving Sharpe invariant.  To produce DIFFERENT Sharpe values across
    models use ``_seed_high_sharpe_predictions`` and
    ``_seed_low_sharpe_predictions`` which have different mean/std ratios.
    """
    rows = [
        (date(2025, 1, 10), "SYM_A", 0.90, ret,        hit),
        (date(2025, 1, 10), "SYM_B", 0.80, ret * 0.8,  hit),
        (date(2025, 1, 10), "SYM_C", 0.70, ret * 0.6,  hit),
        (date(2025, 1, 20), "SYM_A", 0.85, ret * 1.2,  hit),
        (date(2025, 1, 20), "SYM_B", 0.75, ret * 0.9,  hit),
        (date(2025, 1, 20), "SYM_C", 0.65, ret * 0.7,  hit),
        (date(2025, 1, 30), "SYM_A", 0.95, ret * 1.5,  hit),
        (date(2025, 1, 30), "SYM_B", 0.60, ret * 0.5,  not hit),
        (date(2025, 1, 30), "SYM_C", 0.50, ret * 1.1,  hit),
    ]
    for pred_date, symbol, prob, r, h in rows:
        _insert_prediction(
            conn,
            model_id=model_id,
            horizon=horizon,
            symbol=symbol,
            prediction_date=pred_date,
            predicted_probability=prob,
            actual_max_return=r,
            actual_hit=h,
        )


def _seed_high_sharpe_predictions(conn, model_id: str, horizon: str) -> None:
    """Seed predictions with a high mean/std ratio (smooth uptrend).

    Per-date portfolio contributions (SIZE_FRAC = 0.8/6 ≈ 0.1333):
      date 1: top-3 returns → [0.12, 0.11, 0.10] → sum=0.33 → contrib=0.044
      date 2: top-3 returns → [0.13, 0.12, 0.11] → sum=0.36 → contrib=0.048
      date 3: top-3 returns → [0.14, 0.13, 0.12] → sum=0.39 → contrib=0.052

    mu ≈ 0.0480, std is small (monotone dates) → high Sharpe.
    """
    rows = [
        (date(2025, 1, 10), "SYM_A", 0.90, 0.12, True),
        (date(2025, 1, 10), "SYM_B", 0.80, 0.11, True),
        (date(2025, 1, 10), "SYM_C", 0.70, 0.10, True),
        (date(2025, 1, 20), "SYM_A", 0.85, 0.13, True),
        (date(2025, 1, 20), "SYM_B", 0.75, 0.12, True),
        (date(2025, 1, 20), "SYM_C", 0.65, 0.11, True),
        (date(2025, 1, 30), "SYM_A", 0.95, 0.14, True),
        (date(2025, 1, 30), "SYM_B", 0.60, 0.13, True),
        (date(2025, 1, 30), "SYM_C", 0.50, 0.12, True),
    ]
    for pred_date, symbol, prob, ret, hit in rows:
        _insert_prediction(
            conn,
            model_id=model_id,
            horizon=horizon,
            symbol=symbol,
            prediction_date=pred_date,
            predicted_probability=prob,
            actual_max_return=ret,
            actual_hit=hit,
        )


def _seed_low_sharpe_predictions(conn, model_id: str, horizon: str) -> None:
    """Seed predictions with a low mean/std ratio (volatile, low mean returns).

    Per-date portfolio contributions:
      date 1: returns → [0.01, -0.05, 0.03] → sum=-0.01 → contrib=-0.00133
      date 2: returns → [0.15, -0.12, 0.01] → sum=0.04  → contrib=0.00533
      date 3: returns → [0.02,  0.02, 0.02] → sum=0.06  → contrib=0.00800

    Highly variable contributions → low Sharpe (high std relative to mean).
    """
    rows = [
        (date(2025, 1, 10), "SYM_A", 0.90,  0.01, True),
        (date(2025, 1, 10), "SYM_B", 0.80, -0.05, False),
        (date(2025, 1, 10), "SYM_C", 0.70,  0.03, True),
        (date(2025, 1, 20), "SYM_A", 0.85,  0.15, True),
        (date(2025, 1, 20), "SYM_B", 0.75, -0.12, False),
        (date(2025, 1, 20), "SYM_C", 0.65,  0.01, True),
        (date(2025, 1, 30), "SYM_A", 0.95,  0.02, True),
        (date(2025, 1, 30), "SYM_B", 0.60,  0.02, True),
        (date(2025, 1, 30), "SYM_C", 0.50,  0.02, True),
    ]
    for pred_date, symbol, prob, ret, hit in rows:
        _insert_prediction(
            conn,
            model_id=model_id,
            horizon=horizon,
            symbol=symbol,
            prediction_date=pred_date,
            predicted_probability=prob,
            actual_max_return=ret,
            actual_hit=hit,
        )


# ──────────────────────────────────────────────────────────────────────
# 1. First model skip
# ──────────────────────────────────────────────────────────────────────


def test_first_model_skip(temp_db, monkeypatch):
    """No prior active model → passed=True, reason='first_model_skip'."""
    monkeypatch.setattr(
        "crypto.ml.validation_gate._run_backfill",
        lambda conn, horizon, new_model_id: None,
    )

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
# 2. Gate passes when new model is at parity
# ──────────────────────────────────────────────────────────────────────


def test_gate_passes_when_new_model_at_parity(temp_db, monkeypatch):
    """Old hit_rate=0.50 Sharpe≈X; new hit_rate=0.50 Sharpe≈X → PASS."""
    monkeypatch.setattr(
        "crypto.ml.validation_gate._run_backfill",
        lambda conn, horizon, new_model_id: None,
    )

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

    # Seed predictions so Sharpe is finite and identical for both models.
    _seed_sharpe_predictions(temp_db, old_id, "10d", ret=0.10)
    _seed_sharpe_predictions(temp_db, new_id, "10d", ret=0.10)

    result = validate_promotion(temp_db, new_id, "10d")

    assert result.passed is True
    assert result.reason is None
    assert result.comparison["passed_hit_rate"] is True
    assert result.comparison["passed_sharpe"] is True


# ──────────────────────────────────────────────────────────────────────
# 3. Gate fails on hit rate
# ──────────────────────────────────────────────────────────────────────


def test_gate_fails_on_hit_rate(temp_db, monkeypatch):
    """New hit_rate=0.40 < 0.9 * 0.50 = 0.45 → FAIL, reason='hit_rate_below_threshold'.

    Sharpe is identical in both models to isolate the hit-rate arm.
    """
    monkeypatch.setattr(
        "crypto.ml.validation_gate._run_backfill",
        lambda conn, horizon, new_model_id: None,
    )

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

    # Same Sharpe baseline so only hit rate fails.
    _seed_sharpe_predictions(temp_db, old_id, "10d", ret=0.10)
    _seed_sharpe_predictions(temp_db, new_id, "10d", ret=0.10)

    result = validate_promotion(temp_db, new_id, "10d")

    assert result.passed is False
    assert result.reason == "hit_rate_below_threshold"
    assert result.comparison["passed_hit_rate"] is False
    assert result.comparison["passed_sharpe"] is True


# ──────────────────────────────────────────────────────────────────────
# 4. Gate fails on Sharpe
# ──────────────────────────────────────────────────────────────────────


def test_gate_fails_on_sharpe(temp_db, monkeypatch):
    """New Sharpe well below 0.9 * old Sharpe → FAIL, reason='sharpe_below_threshold'.

    Hit rate is identical in both models to isolate the Sharpe arm.

    Note: scaling all returns by a constant k multiplies both mean and std
    by k, leaving Sharpe unchanged.  To produce a LOWER Sharpe we change
    the return *structure* — the new model uses volatile, low-mean returns
    (_seed_low_sharpe_predictions) vs the old model's smooth, high-mean
    returns (_seed_high_sharpe_predictions).  Both helpers insert 3 dates
    × 3 symbols.
    """
    monkeypatch.setattr(
        "crypto.ml.validation_gate._run_backfill",
        lambda conn, horizon, new_model_id: None,
    )

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

    # Old model: smooth monotone returns → high Sharpe (≈190).
    # New model: volatile, low-mean returns → low Sharpe (≈13).
    # Ratio ≈ 0.07 < 0.9 → Sharpe arm fails.
    _seed_high_sharpe_predictions(temp_db, old_id, "10d")
    _seed_low_sharpe_predictions(temp_db, new_id, "10d")

    result = validate_promotion(temp_db, new_id, "10d")

    assert result.passed is False
    assert result.reason == "sharpe_below_threshold"
    assert result.comparison["passed_hit_rate"] is True
    assert result.comparison["passed_sharpe"] is False


# ──────────────────────────────────────────────────────────────────────
# 5. Gate fails both arms
# ──────────────────────────────────────────────────────────────────────


def test_gate_fails_both_arms(temp_db, monkeypatch):
    """Both hit rate and Sharpe below threshold → reason='both_below_threshold'."""
    monkeypatch.setattr(
        "crypto.ml.validation_gate._run_backfill",
        lambda conn, horizon, new_model_id: None,
    )

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
        precision_at_threshold=0.40,  # hit-rate arm fails (0.40 < 0.9 * 0.50)
    )

    # Sharpe arm also fails: old model uses high-Sharpe fixture,
    # new model uses low-Sharpe fixture.  Ratio ≈ 0.07 < 0.9.
    _seed_high_sharpe_predictions(temp_db, old_id, "10d")
    _seed_low_sharpe_predictions(temp_db, new_id, "10d")

    result = validate_promotion(temp_db, new_id, "10d")

    assert result.passed is False
    assert result.reason == "both_below_threshold"
    assert result.comparison["passed_hit_rate"] is False
    assert result.comparison["passed_sharpe"] is False


# ──────────────────────────────────────────────────────────────────────
# 6. Gate timeout
# ──────────────────────────────────────────────────────────────────────


def test_gate_timeout(temp_db, monkeypatch):
    """Backfill that takes longer than the timeout → passed=False, reason='validation_timeout'.

    Uses MHDE_RETRAIN_VALIDATION_TIMEOUT_SEC=1 and a backfill stub that
    sleeps for 3 seconds to guarantee the timeout fires.
    """
    monkeypatch.setenv("MHDE_RETRAIN_VALIDATION_TIMEOUT_SEC", "1")

    def _slow_backfill(conn, horizon, new_model_id):
        time.sleep(3)

    monkeypatch.setattr(
        "crypto.ml.validation_gate._run_backfill",
        _slow_backfill,
    )

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

    assert result.passed is False
    assert result.reason == "validation_timeout"
    # Gate should return quickly (well under 10 s with 1 s timeout).
    assert result.duration_sec < 10


# ──────────────────────────────────────────────────────────────────────
# 7. Zero old Sharpe — permissive edge case
# ──────────────────────────────────────────────────────────────────────


def test_gate_zero_old_sharpe_permissive(temp_db, monkeypatch):
    """When old Sharpe is zero (degenerate baseline), the Sharpe arm passes.

    Policy rationale (documented here AND in validation_gate.py):
    If the previously-active model had old_sharpe <= 0, a multiplicative
    floor of 0.9 * 0.0 = 0.0 would trivially pass anything >= 0, and
    0.9 * negative is even more misleading.  Rather than applying a
    meaningless floor, the Sharpe arm is treated as *passing* — there is
    no positive baseline to defend.  This is intentionally more permissive
    and applies only in bootstrap-like edge cases where the previous model
    had no useful return track record.
    """
    monkeypatch.setattr(
        "crypto.ml.validation_gate._run_backfill",
        lambda conn, horizon, new_model_id: None,
    )

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

    # Old model: seed predictions with all-negative returns so that
    # compute_walkfold_trade_sharpe returns a negative value.
    # Negative return rows seeded with alternating -0.05/-0.10 across
    # dates to produce non-zero std (avoiding NaN Sharpe).
    rows_old = [
        (date(2025, 1, 10), "SYM_A", 0.90, -0.05, False),
        (date(2025, 1, 10), "SYM_B", 0.80, -0.08, False),
        (date(2025, 1, 10), "SYM_C", 0.70, -0.03, False),
        (date(2025, 1, 20), "SYM_A", 0.85, -0.10, False),
        (date(2025, 1, 20), "SYM_B", 0.75, -0.07, False),
        (date(2025, 1, 20), "SYM_C", 0.65, -0.06, False),
        (date(2025, 1, 30), "SYM_A", 0.95, -0.12, False),
        (date(2025, 1, 30), "SYM_B", 0.60, -0.04, False),
        (date(2025, 1, 30), "SYM_C", 0.50, -0.09, False),
    ]
    for pred_date, symbol, prob, ret, hit in rows_old:
        _insert_prediction(
            temp_db,
            model_id=old_id,
            horizon="10d",
            symbol=symbol,
            prediction_date=pred_date,
            predicted_probability=prob,
            actual_max_return=ret,
            actual_hit=hit,
        )

    # New model: seed with positive returns.
    _seed_sharpe_predictions(temp_db, new_id, "10d", ret=0.05)

    result = validate_promotion(temp_db, new_id, "10d")

    # old_sharpe < 0 → Sharpe arm should pass (permissive rule).
    assert result.comparison["passed_sharpe"] is True
    # hit rate is at parity, so overall should pass.
    assert result.passed is True
    assert result.comparison["thresholds"]["sharpe_floor"] is None
