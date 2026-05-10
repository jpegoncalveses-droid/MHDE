"""Retrain validation gate — decides whether a newly-trained crypto model
is good enough to be promoted to ``is_active=true``.

Public API
----------
* :class:`ValidationResult` — structured outcome of the gate.
* :func:`validate_promotion` — entry point called by the train pipeline
  (Task 1.4) after a new model is inserted but before ``is_active`` is
  flipped.

Gate logic (single-arm, 0.9× multiplicative threshold)
-------------------------------------------------------
The gate compares the new model against the previously-active model on
one metric: **label hit rate** — ``precision_at_threshold`` stored on
``crypto_ml_model_runs`` at training time.  If the stored value is NULL
(shouldn't happen in practice, but defensive), the gate falls back to
computing the live precision from ``crypto_ml_predictions``.

The gate passes when ``new_hit_rate >= 0.9 * old_hit_rate``.

No backfill step is required; the hit-rate check is a single SELECT
query and is essentially instant.

Originally specified with a second Sharpe arm; dropped after we
discovered walkfold predictions are tagged per-fold (e.g.
``crypto_10d_walkfold_2024_03_a3b1``), not per-production-model, so
``compute_walkfold_trade_sharpe(conn, new_model_id, horizon)`` returned
zero rows and the degenerate-baseline rule caused the Sharpe arm to
trivially pass.  ADR-019 (Task 1.5) captures the full rationale and
escape valve (AUC arm if hit-rate-only proves too forgiving).

Edge cases
----------
* **First model (no prior active):** returns ``passed=True`` with
  ``reason="first_model_skip"``.  There is no baseline to defend.

* **Degenerate baseline (old_hit_rate <= 0):** the multiplicative
  threshold is meaningless when the baseline is zero or negative.  The
  hit-rate arm is treated as *passing* — we cannot defend a baseline
  that has no value.  This is more permissive than the strict rule and
  is only reachable in bootstrap-like edge cases.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import duckdb

logger = logging.getLogger("mhde.crypto.validation_gate")


# ──────────────────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Outcome of :func:`validate_promotion`.

    ``reason`` valid values: ``None`` (pass), ``"first_model_skip"``,
    ``"hit_rate_below_threshold"``.
    """

    passed: bool
    comparison: dict
    duration_sec: float
    reason: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────


def _read_stored_hit_rate(
    conn: duckdb.DuckDBPyConnection,
    model_id: str,
) -> Optional[float]:
    """Return ``precision_at_threshold`` stored in ``crypto_ml_model_runs``.

    Returns ``None`` if the row does not exist or the column value is NULL.
    """
    row = conn.execute(
        "SELECT precision_at_threshold FROM crypto_ml_model_runs WHERE model_id = ?",
        [model_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _compute_live_hit_rate(
    conn: duckdb.DuckDBPyConnection,
    model_id: str,
) -> Optional[float]:
    """Compute live precision from ``crypto_ml_predictions`` as a fallback
    when ``precision_at_threshold`` is not stored.

    Returns the fraction of filled predictions where ``actual_hit=TRUE``,
    or ``None`` if there are no filled outcomes.
    """
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN actual_hit IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN actual_hit = TRUE  THEN 1 ELSE 0 END)
        FROM crypto_ml_predictions
        WHERE model_id = ? AND outcome_filled_at IS NOT NULL
        """,
        [model_id],
    ).fetchone()
    n_filled = int(row[0] or 0)
    n_hits = int(row[1] or 0)
    if n_filled == 0:
        return None
    return n_hits / n_filled


def _get_hit_rate(
    conn: duckdb.DuckDBPyConnection,
    model_id: str,
) -> Optional[float]:
    """Return the label hit rate for ``model_id``.

    Prefers the stored ``precision_at_threshold`` from training-time CV.
    Falls back to computing live precision from predictions when the
    stored value is NULL or absent.
    """
    stored = _read_stored_hit_rate(conn, model_id)
    if stored is not None:
        return stored
    logger.warning(
        "precision_at_threshold is NULL for %s; falling back to live precision",
        model_id,
    )
    return _compute_live_hit_rate(conn, model_id)


# ──────────────────────────────────────────────────────────────────────
# Gate entry point
# ──────────────────────────────────────────────────────────────────────


def validate_promotion(
    conn: duckdb.DuckDBPyConnection,
    new_model_id: str,
    horizon: str,
) -> ValidationResult:
    """Decide whether ``new_model_id`` is good enough to be promoted.

    Parameters
    ----------
    conn:
        Open DuckDB connection (read access is sufficient; no backfill
        step is performed).
    new_model_id:
        The ``model_id`` of the newly-trained model.  It must already
        exist in ``crypto_ml_model_runs`` (INSERTed by the train pipeline)
        but ``is_active`` must NOT yet have been flipped to ``true``.
    horizon:
        Horizon string (e.g. ``"5d"`` or ``"10d"``).

    Returns
    -------
    ValidationResult
        ``passed=True`` → caller may promote.
        ``passed=False`` → caller must block promotion.
    """
    gate_start = time.perf_counter()

    # ── 1. Find previous active model for the same horizon ──────────────
    row = conn.execute(
        """
        SELECT model_id
        FROM crypto_ml_model_runs
        WHERE horizon = ?
          AND is_active = true
          AND model_id != ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [horizon, new_model_id],
    ).fetchone()

    old_model_id: Optional[str] = row[0] if row else None

    if old_model_id is None:
        # Bootstrap case: no prior active model — allow promotion.
        duration = time.perf_counter() - gate_start
        logger.info(
            "validate_promotion(%s): no prior active model for horizon=%s "
            "— first_model_skip, returning PASS",
            new_model_id, horizon,
        )
        return ValidationResult(
            passed=True,
            comparison={"reason": "first_model_skip"},
            duration_sec=duration,
            reason="first_model_skip",
        )

    # ── 2. Compute label hit rate for both models ─────────────────────
    old_hit_rate = _get_hit_rate(conn, old_model_id)
    new_hit_rate = _get_hit_rate(conn, new_model_id)

    # ── 3. Apply hit-rate gate (0.9× threshold) ───────────────────────
    #
    # Edge case: if the old baseline is zero or negative the multiplicative
    # threshold is meaningless (0.9 * 0 = 0 would trivially pass anything,
    # and 0.9 * negative_value would invert the direction of the gate).
    # Policy: treat the arm as *passing* — there is no positive baseline
    # to defend.  This applies only in bootstrap-like situations where the
    # previously-active model had no useful track record.

    if old_hit_rate is None or old_hit_rate <= 0:
        # No meaningful hit-rate baseline; pass this arm.
        passed_hit_rate = True
        hit_rate_floor = None
        logger.info(
            "validate_promotion(%s): old_hit_rate=%s (degenerate) — "
            "hit-rate arm treated as PASS",
            new_model_id, old_hit_rate,
        )
    else:
        hit_rate_floor = 0.9 * old_hit_rate
        passed_hit_rate = (new_hit_rate is not None) and (new_hit_rate >= hit_rate_floor)

    overall_passed = passed_hit_rate

    reason = None if overall_passed else "hit_rate_below_threshold"

    duration = time.perf_counter() - gate_start

    comparison = {
        "old": {
            "model_id": old_model_id,
            "label_hit_rate": old_hit_rate,
        },
        "new": {
            "model_id": new_model_id,
            "label_hit_rate": new_hit_rate,
        },
        "thresholds": {
            "hit_rate_floor": hit_rate_floor,
        },
        "passed_hit_rate": passed_hit_rate,
    }

    log_level = logging.INFO if overall_passed else logging.WARNING
    logger.log(
        log_level,
        "validate_promotion(%s, horizon=%s): passed=%s reason=%s "
        "hit_rate(old=%.3f new=%.3f floor=%.3f ok=%s) duration=%.1fs",
        new_model_id, horizon, overall_passed, reason,
        old_hit_rate or 0, new_hit_rate or 0, hit_rate_floor or 0, passed_hit_rate,
        duration,
    )

    return ValidationResult(
        passed=overall_passed,
        comparison=comparison,
        duration_sec=duration,
        reason=reason,
    )
