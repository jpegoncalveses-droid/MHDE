"""Tests for crypto/ml/sharpe_sim.py.

Coverage:
  A.  Deterministic Sharpe on a known fixture                  1
  B.  Determinism — same inputs twice → same output            1
  C.  Edge cases: empty result, single date, NULL outcomes      3
  D.  model_id / horizon isolation                             1
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from crypto.ml.sharpe_sim import compute_walkfold_trade_sharpe


# ──────────────────────────────────────────────────────────────────────
# Fixture helpers
# ──────────────────────────────────────────────────────────────────────


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
        VALUES (?, ?, ?, ?, ?, 0.10, ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            model_id, horizon, symbol, prediction_date,
            predicted_probability,
            actual_max_return,
            actual_hit,
        ],
    )


# ──────────────────────────────────────────────────────────────────────
# Shared fixture — three prediction dates, three coins each
#
# SIZE_FRAC = 0.8 / 6 ≈ 0.13333
#
# date 2025-01-10: SYM_A (0.9, ret=0.10) SYM_B (0.8, ret=0.05) SYM_C (0.7, ret=-0.02)
#   contrib = (0.10 + 0.05 + (-0.02)) * SIZE_FRAC = 0.13 * 0.1333… = 0.01733…
# date 2025-01-20: SYM_A (0.85, ret=0.08) SYM_B (0.75, ret=0.03) SYM_C (0.65, ret=0.01)
#   contrib = (0.08 + 0.03 + 0.01) * SIZE_FRAC = 0.12 * 0.1333… = 0.01600
# date 2025-01-30: SYM_A (0.95, ret=0.15) SYM_B (0.60, ret=-0.05) SYM_C (0.50, ret=0.02)
#   contrib = (0.15 + (-0.05) + 0.02) * SIZE_FRAC = 0.12 * 0.1333… = 0.01600
#
# mu    = (0.01733… + 0.01600 + 0.01600) / 3 = 0.016444…
# sigma = std(ddof=1) = 0.000769800…
# Sharpe = (mu / sigma) * sqrt(252) ≈ 339.1106014267…
# ──────────────────────────────────────────────────────────────────────

_FIXTURE_MODEL_ID = "crypto_10d_walkfold_2025_01"
_FIXTURE_HORIZON  = "10d"
_EXPECTED_SHARPE  = 339.11060142673165


def _seed_fixture(conn, model_id: str = _FIXTURE_MODEL_ID,
                  horizon: str = _FIXTURE_HORIZON) -> None:
    rows = [
        (date(2025, 1, 10), "SYM_A", 0.90,  0.10, True),
        (date(2025, 1, 10), "SYM_B", 0.80,  0.05, True),
        (date(2025, 1, 10), "SYM_C", 0.70, -0.02, False),
        (date(2025, 1, 20), "SYM_A", 0.85,  0.08, True),
        (date(2025, 1, 20), "SYM_B", 0.75,  0.03, True),
        (date(2025, 1, 20), "SYM_C", 0.65,  0.01, True),
        (date(2025, 1, 30), "SYM_A", 0.95,  0.15, True),
        (date(2025, 1, 30), "SYM_B", 0.60, -0.05, False),
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
# A. Deterministic Sharpe on a known fixture
# ──────────────────────────────────────────────────────────────────────


def test_sharpe_known_value(temp_db):
    """Function returns the expected Sharpe for a hand-computed fixture."""
    _seed_fixture(temp_db)
    result = compute_walkfold_trade_sharpe(temp_db, _FIXTURE_MODEL_ID, _FIXTURE_HORIZON)
    assert abs(result - _EXPECTED_SHARPE) < 1e-6, (
        f"Expected Sharpe ≈ {_EXPECTED_SHARPE}, got {result}"
    )


# ──────────────────────────────────────────────────────────────────────
# B. Determinism — same inputs twice → same output
# ──────────────────────────────────────────────────────────────────────


def test_sharpe_deterministic(temp_db):
    """Two calls with identical DB state return identical float."""
    _seed_fixture(temp_db)
    first  = compute_walkfold_trade_sharpe(temp_db, _FIXTURE_MODEL_ID, _FIXTURE_HORIZON)
    second = compute_walkfold_trade_sharpe(temp_db, _FIXTURE_MODEL_ID, _FIXTURE_HORIZON)
    assert first == second, f"Calls diverged: {first} vs {second}"


# ──────────────────────────────────────────────────────────────────────
# C. Edge cases
# ──────────────────────────────────────────────────────────────────────


def test_sharpe_empty_table(temp_db):
    """No matching rows → nan."""
    result = compute_walkfold_trade_sharpe(temp_db, "nonexistent_model", "10d")
    assert math.isnan(result), f"Expected nan, got {result}"


def test_sharpe_single_date(temp_db):
    """A single prediction date has no variance → nan (cannot compute std)."""
    _insert_prediction(
        temp_db,
        model_id="crypto_10d_walkfold_single",
        horizon="10d",
        symbol="SYM_A",
        prediction_date=date(2025, 3, 1),
        predicted_probability=0.9,
        actual_max_return=0.10,
        actual_hit=True,
    )
    result = compute_walkfold_trade_sharpe(temp_db, "crypto_10d_walkfold_single", "10d")
    assert math.isnan(result), f"Expected nan for single date, got {result}"


def test_sharpe_null_outcomes_excluded(temp_db):
    """Rows with NULL actual_max_return or actual_hit are excluded; result
    still computes correctly from the remaining non-null rows."""
    _seed_fixture(temp_db, model_id="crypto_10d_walkfold_nulltest")

    _insert_prediction(
        temp_db,
        model_id="crypto_10d_walkfold_nulltest",
        horizon="10d",
        symbol="SYM_D",
        prediction_date=date(2025, 1, 10),
        predicted_probability=0.99,
        actual_max_return=None,
        actual_hit=None,
    )

    result = compute_walkfold_trade_sharpe(temp_db, "crypto_10d_walkfold_nulltest", "10d")
    assert abs(result - _EXPECTED_SHARPE) < 1e-6, (
        f"NULL row should be excluded; expected {_EXPECTED_SHARPE}, got {result}"
    )


# ──────────────────────────────────────────────────────────────────────
# D. model_id / horizon isolation
# ──────────────────────────────────────────────────────────────────────


def test_sharpe_model_id_isolation(temp_db):
    """A different model_id with the same horizon returns nan (no rows)."""
    _seed_fixture(temp_db, model_id="crypto_10d_walkfold_2025_01")
    result = compute_walkfold_trade_sharpe(
        temp_db, "crypto_10d_walkfold_2025_02", "10d"
    )
    assert math.isnan(result), (
        f"Different model_id should return nan, got {result}"
    )
