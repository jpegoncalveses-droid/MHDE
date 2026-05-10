"""Walkfold OOS trade-Sharpe simulation for the crypto ML validation gate.

Public API
----------
* :func:`compute_walkfold_trade_sharpe` — annualised Sharpe from walkfold
  OOS predictions stored in ``crypto_ml_predictions``.

Design notes
------------
* The function is pure: same DB state + model_id + horizon → same float.
* No file I/O, no global state, no side effects.
* Walkfold predictions are written by ``crypto/ml/backfill_walkforward.py``
  under model_id patterns like ``crypto_{horizon}_walkfold_{YYYY_MM}``.
  This module reads them; it never writes.
* The Sharpe is computed on a per-prediction-date equity curve, using a
  simple daily-contribution model: each prediction date contributes
  ``sum(actual_max_return * SIZE_FRAC)`` where SIZE_FRAC =
  ``deploy_fraction / max_positions = 0.8 / 6 ≈ 0.1333``.  Rows with a
  NULL ``actual_max_return`` or NULL ``actual_hit`` are excluded (outcome
  not yet filled, per Phase 1A policy).  The top-N coins per date are
  selected by ``predicted_probability`` DESC, tie-broken by ``symbol``
  ASC (same as the harness's selection rule).
"""
from __future__ import annotations

import math

import duckdb
import numpy as np
import pandas as pd

# Sizing constants — mirror the values in strategy_edge_analysis.py (Part 2
# comment) and active_spec.json: deploy_fraction=0.8, max_positions=6.
_DEPLOY_FRACTION: float = 0.8
_MAX_POSITIONS: int = 6
_SIZE_FRAC: float = _DEPLOY_FRACTION / _MAX_POSITIONS

# Annualisation: daily Sharpe → annual Sharpe; 252 trading-day convention
# matches crypto/execution/backtest/report.py SHARPE_PERIODS_PER_YEAR.
_SHARPE_PERIODS_PER_YEAR: int = 252

# Top-N picks per prediction date — matches active_spec.json selection_n=6.
_TOP_N: int = 6


def compute_walkfold_trade_sharpe(
    conn: duckdb.DuckDBPyConnection,
    model_id: str,
    horizon: str,
) -> float:
    """Walkfold OOS trade Sharpe (annualised) — gross returns (no fees/slippage),
    sized at deploy_fraction/max_positions = 0.8/6 per position, top-6 daily picks.

    Parameters
    ----------
    conn:
        Open DuckDB connection.  Read-only access is sufficient; the
        function never writes.
    model_id:
        Exact model_id to filter on (e.g. ``"crypto_10d_walkfold_2025_04"``).
        For an aggregate across all folds of a given horizon, the caller
        should union the folds externally or pass a pattern-matched view.
    horizon:
        Horizon string (e.g. ``"10d"`` or ``"5d"``).

    Returns
    -------
    float
        Annualised Sharpe ratio computed from per-date portfolio returns.
        Returns ``float("nan")`` when there are fewer than 2 prediction
        dates with non-null outcomes (cannot compute a meaningful standard
        deviation).
    """
    rows = conn.execute(
        """
        SELECT prediction_date,
               symbol,
               predicted_probability,
               actual_max_return
        FROM crypto_ml_predictions
        WHERE model_id = ?
          AND horizon  = ?
          AND actual_max_return IS NOT NULL
          AND actual_hit        IS NOT NULL
        ORDER BY prediction_date ASC,
                 predicted_probability DESC,
                 symbol ASC
        """,
        [model_id, horizon],
    ).fetchdf()

    if rows.empty:
        return float("nan")

    rows["prediction_date"] = pd.to_datetime(rows["prediction_date"]).dt.date

    # Select top-N per date by predicted_probability (already ordered above;
    # use cumcount to number picks within each date).
    rows["_rank"] = rows.groupby("prediction_date").cumcount() + 1
    picks = rows[rows["_rank"] <= _TOP_N].copy()

    if picks.empty:
        return float("nan")

    # Per-date contribution = sum(actual_max_return * SIZE_FRAC) over picks.
    daily_contrib = (
        picks.groupby("prediction_date")["actual_max_return"]
        .apply(lambda s: (s * _SIZE_FRAC).sum())
    )

    if len(daily_contrib) < 2:
        return float("nan")

    mu = float(daily_contrib.mean())
    sigma = float(daily_contrib.std(ddof=1))

    if sigma == 0.0 or math.isnan(sigma):
        return float("nan")

    return float((mu / sigma) * math.sqrt(_SHARPE_PERIODS_PER_YEAR))
