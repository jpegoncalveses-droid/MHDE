"""Retrain validation gate — decides whether a newly-trained crypto model
is good enough to be promoted to ``is_active=true``.

Public API
----------
* :class:`ValidationResult` — structured outcome of the gate.
* :func:`validate_promotion` — entry point called by the train pipeline
  (Task 1.4) after a new model is inserted but before ``is_active`` is
  flipped.

Gate logic (0.9× multiplicative threshold)
-------------------------------------------
The gate compares the new model against the previously-active model on
two metrics:

1. **Label hit rate** — ``precision_at_threshold`` stored on
   ``crypto_ml_model_runs`` at training time.  If the stored value is
   NULL (shouldn't happen in practice, but defensive), the gate falls
   back to computing the live precision from ``crypto_ml_predictions``.

2. **Trade Sharpe** — annualised gross Sharpe from walkfold OOS
   predictions via :func:`~crypto.ml.sharpe_sim.compute_walkfold_trade_sharpe`.

Both metrics must satisfy ``new >= 0.9 * old`` for the gate to pass.

Edge cases
----------
* **First model (no prior active):** returns ``passed=True`` with
  ``reason="first_model_skip"``.  There is no baseline to defend.

* **Degenerate baseline (old_sharpe <= 0 or old_hit_rate <= 0):** the
  multiplicative threshold is meaningless when the baseline is zero or
  negative.  Each degenerate arm is treated as *passing* — we cannot
  defend a baseline that has no value.  This is more permissive than the
  strict rule and is only reachable in bootstrap-like edge cases.

Timeout enforcement
-------------------
:func:`validate_promotion` uses :class:`concurrent.futures.ThreadPoolExecutor`
with ``future.result(timeout=...)`` to bound the time spent waiting for
the backfill step.  The timeout is read from
``MHDE_RETRAIN_VALIDATION_TIMEOUT_SEC`` (default 600 seconds).  If the
backfill does not complete within the timeout, the gate returns
``passed=False, reason="validation_timeout"`` immediately — timeout
never counts as PASS.  The backfill thread continues running in the
background (Python threads cannot be killed); this is acceptable because
the train pipeline discards the result and moves on.
"""
from __future__ import annotations

import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Optional

import duckdb

from crypto.ml.backfill_walkforward import backfill_horizon
from crypto.ml.sharpe_sim import compute_walkfold_trade_sharpe

logger = logging.getLogger("mhde.crypto.validation_gate")

# Default timeout (seconds) for the full validation run (backfill + metrics).
_DEFAULT_TIMEOUT_SEC: int = 600


# ──────────────────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Outcome of :func:`validate_promotion`."""

    passed: bool
    comparison: dict
    duration_sec: float
    reason: Optional[str] = None  # e.g. "first_model_skip", "validation_timeout",
    #   "hit_rate_below_threshold", "sharpe_below_threshold", "both_below_threshold"


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


def _run_backfill(
    conn: duckdb.DuckDBPyConnection,
    horizon: str,
    new_model_id: str,
) -> None:
    """Run walkfold backfill for the given horizon.

    This is extracted into its own function so tests can monkeypatch it
    without touching ``backfill_walkforward.backfill_horizon`` directly.

    In production, this calls :func:`~crypto.ml.backfill_walkforward.backfill_horizon`
    with ``force=True`` so any existing walkfold predictions for the
    horizon are refreshed with the latest fold data.  The ``new_model_id``
    parameter is accepted for monkeypatching convenience (tests patch
    this function and may need the model_id to seed predictions).
    """
    backfill_horizon(conn, horizon, force=True)


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
        Open DuckDB connection with write access (needed by the backfill
        step).
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

    timeout_sec: int = int(os.environ.get(
        "MHDE_RETRAIN_VALIDATION_TIMEOUT_SEC", str(_DEFAULT_TIMEOUT_SEC)
    ))

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

    # ── 2. Run walkfold backfill for the new model (bounded by timeout) ──
    #
    # We use ThreadPoolExecutor so that future.result(timeout=...) actually
    # bounds our *wait* time.  The backfill thread may continue after we
    # return (Python threads cannot be killed), but the gate will return
    # FAIL/TIMEOUT immediately.

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run_backfill, conn, horizon, new_model_id)
        elapsed_before_backfill = time.perf_counter() - gate_start
        remaining_timeout = max(0.0, timeout_sec - elapsed_before_backfill)
        try:
            future.result(timeout=remaining_timeout)
        except FuturesTimeoutError:
            duration = time.perf_counter() - gate_start
            logger.warning(
                "validate_promotion(%s): backfill timed out after %.1fs (limit=%ds)",
                new_model_id, duration, timeout_sec,
            )
            return ValidationResult(
                passed=False,
                comparison={
                    "old": {"model_id": old_model_id},
                    "new": {"model_id": new_model_id},
                    "reason": "validation_timeout",
                },
                duration_sec=duration,
                reason="validation_timeout",
            )
        except Exception as exc:
            # Backfill raised — treat as timeout/failure (do not promote).
            duration = time.perf_counter() - gate_start
            logger.error(
                "validate_promotion(%s): backfill raised %s — blocking promotion",
                new_model_id, exc, exc_info=True,
            )
            return ValidationResult(
                passed=False,
                comparison={
                    "old": {"model_id": old_model_id},
                    "new": {"model_id": new_model_id},
                    "reason": f"backfill_error: {type(exc).__name__}: {exc}",
                },
                duration_sec=duration,
                reason="validation_timeout",
            )

    # ── 3. Compute label hit rate for both models ─────────────────────
    old_hit_rate = _get_hit_rate(conn, old_model_id)
    new_hit_rate = _get_hit_rate(conn, new_model_id)

    # ── 4. Compute trade Sharpe for both models ───────────────────────
    old_sharpe = compute_walkfold_trade_sharpe(conn, old_model_id, horizon)
    new_sharpe = compute_walkfold_trade_sharpe(conn, new_model_id, horizon)

    # ── 5. Apply gates (0.9× threshold) ──────────────────────────────
    #
    # Edge case: if the old baseline is zero or negative the multiplicative
    # threshold is meaningless (0.9 * 0 = 0 would trivially pass anything,
    # and 0.9 * negative_value would invert the direction of the gate).
    # Policy: treat each degenerate arm as *passing* — there is no positive
    # baseline to defend.  This applies only in bootstrap-like situations
    # where the previously-active model had no useful track record.

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

    if math.isnan(old_sharpe) or old_sharpe <= 0:
        # No meaningful Sharpe baseline; pass this arm.
        passed_sharpe = True
        sharpe_floor = None
        logger.info(
            "validate_promotion(%s): old_sharpe=%s (degenerate/nan) — "
            "Sharpe arm treated as PASS",
            new_model_id, old_sharpe,
        )
    else:
        sharpe_floor = 0.9 * old_sharpe
        new_sharpe_val = new_sharpe if not math.isnan(new_sharpe) else float("-inf")
        passed_sharpe = new_sharpe_val >= sharpe_floor

    overall_passed = passed_hit_rate and passed_sharpe

    if not passed_hit_rate and not passed_sharpe:
        reason = "both_below_threshold"
    elif not passed_hit_rate:
        reason = "hit_rate_below_threshold"
    elif not passed_sharpe:
        reason = "sharpe_below_threshold"
    else:
        reason = None

    duration = time.perf_counter() - gate_start

    comparison = {
        "old": {
            "model_id": old_model_id,
            "label_hit_rate": old_hit_rate,
            "trade_sharpe": old_sharpe if not math.isnan(old_sharpe) else None,
        },
        "new": {
            "model_id": new_model_id,
            "label_hit_rate": new_hit_rate,
            "trade_sharpe": new_sharpe if not math.isnan(new_sharpe) else None,
        },
        "thresholds": {
            "hit_rate_floor": hit_rate_floor,
            "sharpe_floor": sharpe_floor,
        },
        "passed_hit_rate": passed_hit_rate,
        "passed_sharpe": passed_sharpe,
    }

    log_level = logging.INFO if overall_passed else logging.WARNING
    logger.log(
        log_level,
        "validate_promotion(%s, horizon=%s): passed=%s reason=%s "
        "hit_rate(old=%.3f new=%.3f floor=%.3f ok=%s) "
        "sharpe(old=%.3f new=%.3f floor=%.3f ok=%s) duration=%.1fs",
        new_model_id, horizon, overall_passed, reason,
        old_hit_rate or 0, new_hit_rate or 0, hit_rate_floor or 0, passed_hit_rate,
        old_sharpe if not math.isnan(old_sharpe) else 0,
        new_sharpe if not math.isnan(new_sharpe) else 0,
        sharpe_floor or 0, passed_sharpe,
        duration,
    )

    return ValidationResult(
        passed=overall_passed,
        comparison=comparison,
        duration_sec=duration,
        reason=reason,
    )
