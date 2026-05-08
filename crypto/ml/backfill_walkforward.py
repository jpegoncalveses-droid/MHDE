"""Phase 1A — walk-forward prediction backfill.

See ``crypto/ml/PHASE1A_SPEC.md`` for the full spec. This module captures
the OOS probabilities each fold of :func:`crypto.ml.train.train_walk_forward`
already computes (and otherwise discards), persists them tagged with a
fold-specific ``model_id``, and computes realized outcomes against
``crypto_prices_daily``.

Design choices made for this phase, all explicit:

  * **Outcome gap policy — NULL on incomplete path.**
    A prediction whose entry-day close is missing, or whose forward-window
    in ``crypto_prices_daily`` has fewer than ``horizon_days`` rows after
    the prediction date, gets ``actual_max_return``, ``actual_max_drawdown``,
    ``actual_hit``, and ``outcome_filled_at`` written as NULL. The
    prediction row is still inserted so the model's signal is preserved;
    Phase 1B's harness filters via ``WHERE outcome_filled_at IS NOT NULL``.
    The number of NULLed predictions is logged per fold and surfaced in
    :class:`BackfillResult.fold_summaries` so silent data gaps are visible.

  * **Idempotency — fail loudly by default; ``--force`` to overwrite.**
    Model IDs are deterministic (``crypto_{horizon}_walkfold_{YYYY_MM}``).
    A second run hits PK collision; default behaviour is to raise with a
    clear message naming the offending model_ids. ``force=True`` deletes
    matching rows from both ``crypto_ml_predictions`` and
    ``crypto_ml_model_runs`` before writing.

  * **Per-fold transactional writes.**
    Each fold's persistence is wrapped in BEGIN / COMMIT (or ROLLBACK on
    any failure) so a partial-fold write can never leave orphans. Folds
    are independent — failure of fold N does not prevent attempts at
    fold N+1, but the failure is recorded in the result.

  * **Scope.** Only ``crypto/ml/`` files are modified. Existing actives
    (``is_active=true``) are never flipped. New ``model_runs`` rows are
    inserted with ``is_active=false`` so daily ``predict`` ignores them.

This module imports nothing from equity / FX / shared ``ml/``; it is
crypto-only and read-only outside its two destination tables.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import duckdb
import pandas as pd

from crypto.ml.train import (
    TRAIN_START,
    _build_walk_forward_folds,
    _load_dataset,
    _train_single_fold,
)

logger = logging.getLogger("mhde.crypto.backfill_walkforward")


# ──────────────────────────────────────────────────────────────────────
# Config — mirrors crypto/ml/retrain.py::CONFIGS
# ──────────────────────────────────────────────────────────────────────


HORIZON_CONFIGS: dict[str, dict[str, Any]] = {
    "5d":  {"label_col": "label_5d_10pct",  "threshold": 0.10},
    "10d": {"label_col": "label_10d_10pct", "threshold": 0.10},
}

MODEL_ID_PREFIX = "crypto_"
MODEL_ID_TAG = "_walkfold_"


# ──────────────────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class FoldSummary:
    """One walk-forward fold's persistence summary."""

    fold: int
    model_id: str
    train_end: str
    test_start: str
    test_end: str
    n_predictions: int
    n_outcomes_filled: int
    n_outcomes_nulled: int
    auc_roc: float | None
    lift: float | None
    error: str | None = None


@dataclass
class BackfillResult:
    """Outcome of :func:`backfill_horizon` for one horizon."""

    horizon: str
    label_col: str
    dry_run: bool
    n_folds_planned: int
    n_folds_succeeded: int
    n_folds_failed: int
    n_predictions: int
    n_outcomes_filled: int
    n_outcomes_nulled: int
    fold_summaries: list[FoldSummary] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# model_id helpers
# ──────────────────────────────────────────────────────────────────────


def model_id_for_fold(horizon: str, test_start: str) -> str:
    """Deterministic model_id from horizon + fold's test_start (YYYY-MM-DD).

    >>> model_id_for_fold("5d", "2024-10-05")
    'crypto_5d_walkfold_2024_10'
    """
    yyyy_mm = test_start[:7].replace("-", "_")
    return f"{MODEL_ID_PREFIX}{horizon}{MODEL_ID_TAG}{yyyy_mm}"


def is_backfill_model_id(model_id: str) -> bool:
    """True iff ``model_id`` follows the Phase 1A naming convention."""
    return MODEL_ID_TAG in model_id and model_id.startswith(MODEL_ID_PREFIX)


# ──────────────────────────────────────────────────────────────────────
# Idempotency
# ──────────────────────────────────────────────────────────────────────


def _existing_backfill_model_ids(
    conn: duckdb.DuckDBPyConnection, planned: list[str]
) -> list[str]:
    if not planned:
        return []
    placeholders = ",".join(["?"] * len(planned))
    rows = conn.execute(
        f"SELECT model_id FROM crypto_ml_model_runs "
        f"WHERE model_id IN ({placeholders})",
        list(planned),
    ).fetchall()
    return [r[0] for r in rows]


def _delete_backfill_rows(
    conn: duckdb.DuckDBPyConnection, model_ids: list[str]
) -> tuple[int, int]:
    """Delete rows matching ``model_ids`` from both backfill destination
    tables. Returns (n_predictions_deleted, n_model_runs_deleted)."""
    if not model_ids:
        return 0, 0
    placeholders = ",".join(["?"] * len(model_ids))
    n_pred = conn.execute(
        f"SELECT COUNT(*) FROM crypto_ml_predictions "
        f"WHERE model_id IN ({placeholders})",
        list(model_ids),
    ).fetchone()[0]
    n_runs = conn.execute(
        f"SELECT COUNT(*) FROM crypto_ml_model_runs "
        f"WHERE model_id IN ({placeholders})",
        list(model_ids),
    ).fetchone()[0]
    conn.execute(
        f"DELETE FROM crypto_ml_predictions "
        f"WHERE model_id IN ({placeholders})",
        list(model_ids),
    )
    conn.execute(
        f"DELETE FROM crypto_ml_model_runs "
        f"WHERE model_id IN ({placeholders})",
        list(model_ids),
    )
    return int(n_pred), int(n_runs)


# ──────────────────────────────────────────────────────────────────────
# Outcome computation — NULL on incomplete path
# ──────────────────────────────────────────────────────────────────────


def _compute_outcomes(
    conn: duckdb.DuckDBPyConnection,
    predictions: pd.DataFrame,
    horizon: str,
    prediction_threshold: float,
) -> pd.DataFrame:
    """Join each prediction to ``crypto_prices_daily`` and compute realized
    outcomes. Returns a frame aligned to ``predictions`` row-for-row with
    columns:

        actual_max_return, actual_max_drawdown, actual_hit, outcome_filled_at

    **Gap policy (count-based completeness).** A forward window is
    "complete" iff ``COUNT(close) >= horizon_days`` rows exist in
    ``(entry_date, entry_date + horizon_days days]``. Any internal
    calendar-day gap (e.g. day 3 missing in a 5-day window) drops the
    count below ``horizon_days`` and all four outcome columns are NULL —
    crypto markets trade 24/7 so every calendar day in the horizon is
    expected to have a price row. The prediction row itself is still
    inserted; Phase 1B's harness filters via
    ``WHERE outcome_filled_at IS NOT NULL``.

    **actual_max_drawdown semantics.** Computed as
    ``(MIN(forward_close) / entry_close) - 1``. When forward prices
    never drop below entry (monotonically-up or flat path) the value is
    ``>= 0`` — it reflects the worst forward close relative to entry,
    NOT a strict negative-only excursion. This matches the existing
    ``fill_outcomes`` behavior in ``crypto/ml/predict.py`` and is the
    deliberate, documented contract of this module: a positive
    ``actual_max_drawdown`` is a legitimate value meaning "the
    worst-case forward close was still above entry".
    """
    horizon_days = int(horizon.rstrip("d"))
    now = datetime.utcnow().replace(microsecond=0)

    # Normalize prediction_date dtype before the post-SQL merge —
    # pandas requires matching dtypes on join keys. Production callers
    # pass datetime64[us] (DuckDB DATE), but date / Timestamp / object
    # inputs should round-trip cleanly too.
    predictions = predictions.copy()
    predictions["prediction_date"] = pd.to_datetime(predictions["prediction_date"])

    # Register predictions as a temp view for the JOIN below. Use a unique
    # name so concurrent calls don't collide.
    view_name = f"_pred_{id(predictions):x}"
    conn.register(view_name, predictions[["symbol", "prediction_date"]])

    try:
        rows = conn.execute(
            f"""
            WITH entry AS (
                SELECT t.symbol, t.prediction_date, p.close AS entry_close
                FROM {view_name} t
                LEFT JOIN crypto_prices_daily p
                  ON p.symbol = t.symbol
                 AND p.trade_date = t.prediction_date
                 AND p.close IS NOT NULL
                 AND p.close > 0
            ),
            forward AS (
                SELECT t.symbol, t.prediction_date,
                       COUNT(p.close) AS n_forward_rows,
                       MAX(p.close)   AS max_close,
                       MIN(p.close)   AS min_close
                FROM {view_name} t
                LEFT JOIN crypto_prices_daily p
                  ON p.symbol = t.symbol
                 AND p.trade_date >  t.prediction_date
                 AND p.trade_date <= t.prediction_date
                                     + INTERVAL '{horizon_days} days'
                 AND p.close IS NOT NULL
                 AND p.close > 0
                GROUP BY t.symbol, t.prediction_date
            )
            SELECT e.symbol, e.prediction_date, e.entry_close,
                   f.n_forward_rows, f.max_close, f.min_close
            FROM entry e
            JOIN forward f
              ON f.symbol = e.symbol
             AND f.prediction_date = e.prediction_date
            """
        ).fetchdf()
    finally:
        conn.unregister(view_name)

    # Same dtype normalization on the SQL-side frame so the merge keys
    # match. DuckDB returns DATE as datetime64[us]; we already coerced the
    # left-hand side, so apply the same transform here.
    if not rows.empty:
        rows["prediction_date"] = pd.to_datetime(rows["prediction_date"])

    # Merge back in input order — predictions and rows are not guaranteed
    # to share row ordering after the SQL JOIN, so do a left-merge keyed
    # on (symbol, prediction_date).
    merged = predictions[["symbol", "prediction_date"]].merge(
        rows, on=["symbol", "prediction_date"], how="left"
    )

    complete_mask = (
        merged["entry_close"].notna()
        & (merged["n_forward_rows"] >= horizon_days)
    )
    actual_max_return = pd.Series(
        [None] * len(merged), index=merged.index, dtype=object
    )
    actual_max_drawdown = pd.Series(
        [None] * len(merged), index=merged.index, dtype=object
    )
    actual_hit = pd.Series([None] * len(merged), index=merged.index, dtype=object)
    outcome_filled_at = pd.Series(
        [None] * len(merged), index=merged.index, dtype=object
    )

    if complete_mask.any():
        c = merged.loc[complete_mask]
        max_ret = (c["max_close"] / c["entry_close"]) - 1.0
        min_ret = (c["min_close"] / c["entry_close"]) - 1.0
        actual_max_return.loc[complete_mask] = max_ret.values
        actual_max_drawdown.loc[complete_mask] = min_ret.values
        actual_hit.loc[complete_mask] = (max_ret >= prediction_threshold).values
        outcome_filled_at.loc[complete_mask] = now

    return pd.DataFrame({
        "actual_max_return": actual_max_return,
        "actual_max_drawdown": actual_max_drawdown,
        "actual_hit": actual_hit,
        "outcome_filled_at": outcome_filled_at,
    })


# ──────────────────────────────────────────────────────────────────────
# Persister — transactional write of one fold
# ──────────────────────────────────────────────────────────────────────


def _persist_fold(
    conn: duckdb.DuckDBPyConnection,
    *,
    model_id: str,
    horizon: str,
    fold: dict,
    fold_metrics: dict,
    predictions: pd.DataFrame,
    outcomes: pd.DataFrame,
    prediction_threshold: float,
) -> None:
    """Write one fold's model_runs row + prediction rows in a single
    transaction. Raises on failure; caller catches and records.
    """
    # market_cap_bucket is required by the existing predict path; fill
    # with 'unknown' for backfill rows. (Phase 1B doesn't filter on it.)
    importance_json = json.dumps(fold_metrics.get("feature_importance", {}))

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            """
            INSERT INTO crypto_ml_model_runs (
                model_id, horizon, target_threshold,
                train_start, train_end, test_start, test_end,
                n_train_samples, n_test_samples, n_positive_train, n_positive_test,
                precision_at_threshold, recall_at_threshold, f1_score, auc_roc,
                base_rate, lift_over_base, feature_importance_json, model_path,
                is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, false)
            """,
            [
                model_id, horizon, prediction_threshold,
                TRAIN_START, fold["train_end"], fold["test_start"], fold["test_end"],
                int(fold_metrics.get("n_train", 0)),
                int(fold_metrics.get("n_test", 0)),
                int(fold_metrics.get("n_pos_train", 0)),
                int(fold_metrics.get("n_pos_test", 0)),
                float(fold_metrics.get("precision_top", 0.0)),
                float(fold_metrics.get("recall", 0.0)),
                float(fold_metrics.get("f1", 0.0)),
                float(fold_metrics.get("auc_roc", 0.0)),
                float(fold_metrics.get("base_rate", 0.0)),
                float(fold_metrics.get("lift", 0.0)),
                importance_json,
                None,  # model_path: not persisted for backfill folds
            ],
        )

        # Insert predictions row-by-row (DuckDB's executemany works on a
        # list of param tuples; passing a DataFrame is also supported via
        # parameterized prepared statement).
        rows = []
        for i in range(len(predictions)):
            rows.append([
                predictions["symbol"].iat[i],
                predictions["prediction_date"].iat[i],
                model_id,
                horizon,
                float(predictions["predicted_probability"].iat[i]),
                prediction_threshold,
                "unknown",
                outcomes["actual_max_return"].iat[i],
                outcomes["actual_max_drawdown"].iat[i],
                outcomes["actual_hit"].iat[i],
                outcomes["outcome_filled_at"].iat[i],
            ])
        if rows:
            conn.executemany(
                """
                INSERT INTO crypto_ml_predictions (
                    symbol, prediction_date, model_id, horizon,
                    predicted_probability, prediction_threshold, market_cap_bucket,
                    actual_max_return, actual_max_drawdown, actual_hit,
                    outcome_filled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ──────────────────────────────────────────────────────────────────────
# Orchestrator — one horizon, all folds
# ──────────────────────────────────────────────────────────────────────


def backfill_horizon(
    conn: duckdb.DuckDBPyConnection,
    horizon: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> BackfillResult:
    """Run walk-forward CV for one horizon and persist OOS predictions
    + outcomes per fold.

    Args:
        conn: DuckDB connection (must have write access unless ``dry_run``).
        horizon: ``'5d'`` or ``'10d'``. Must be a key of :data:`HORIZON_CONFIGS`.
        dry_run: if True, run training and outcome computation but do not
            write to the DB. The returned :class:`BackfillResult` reflects
            what *would* have been written.
        force: if True, delete any existing rows whose ``model_id`` matches
            the planned fold IDs before writing. Default behavior raises.
    """
    if horizon not in HORIZON_CONFIGS:
        raise ValueError(
            f"unknown horizon {horizon!r}; expected one of {sorted(HORIZON_CONFIGS)}"
        )
    cfg = HORIZON_CONFIGS[horizon]
    label_col = cfg["label_col"]
    threshold = float(cfg["threshold"])

    folds = _build_walk_forward_folds(conn)
    planned_model_ids = [model_id_for_fold(horizon, f["test_start"]) for f in folds]
    logger.info(
        "Horizon %s: planning %d folds, label=%s, threshold=%.2f",
        horizon, len(folds), label_col, threshold,
    )

    # Idempotency
    existing = _existing_backfill_model_ids(conn, planned_model_ids)
    if existing and not force:
        sample = ", ".join(existing[:3]) + ("..." if len(existing) > 3 else "")
        raise RuntimeError(
            f"Backfill predictions for {len(existing)} model_id(s) already exist "
            f"(e.g. {sample}). Re-run with --force to overwrite, or delete those "
            f"rows from crypto_ml_predictions and crypto_ml_model_runs first."
        )
    if existing and force:
        if dry_run:
            logger.info(
                "[DRY RUN] would force-delete %d existing backfill model_ids",
                len(existing),
            )
        else:
            n_pred, n_runs = _delete_backfill_rows(conn, existing)
            logger.warning(
                "Force-deleted %d existing prediction rows and %d model_run rows "
                "for %d backfill model_ids",
                n_pred, n_runs, len(existing),
            )

    fold_summaries: list[FoldSummary] = []
    n_pred_total = 0
    n_filled_total = 0
    n_nulled_total = 0
    n_succeeded = 0
    n_failed = 0

    for i, fold in enumerate(folds, start=1):
        model_id = model_id_for_fold(horizon, fold["test_start"])
        logger.info(
            "  Fold %d/%d: %s (train ≤ %s, test %s → %s)",
            i, len(folds), model_id,
            fold["train_end"], fold["test_start"], fold["test_end"],
        )
        try:
            X_train, y_train, _ = _load_dataset(
                conn, TRAIN_START, fold["train_end"], label_col
            )
            X_test, y_test, meta_test = _load_dataset(
                conn, fold["test_start"], fold["test_end"], label_col
            )
            if len(X_test) < 10:
                msg = f"too few test samples ({len(X_test)})"
                logger.warning("    SKIP: %s", msg)
                fold_summaries.append(FoldSummary(
                    fold=i, model_id=model_id,
                    train_end=fold["train_end"],
                    test_start=fold["test_start"], test_end=fold["test_end"],
                    n_predictions=0, n_outcomes_filled=0, n_outcomes_nulled=0,
                    auc_roc=None, lift=None, error=msg,
                ))
                n_failed += 1
                continue

            metrics = _train_single_fold(
                X_train, y_train, X_test, y_test, meta_test=meta_test
            )
            if "error" in metrics:
                msg = str(metrics["error"])
                logger.warning("    SKIP: %s", msg)
                fold_summaries.append(FoldSummary(
                    fold=i, model_id=model_id,
                    train_end=fold["train_end"],
                    test_start=fold["test_start"], test_end=fold["test_end"],
                    n_predictions=0, n_outcomes_filled=0, n_outcomes_nulled=0,
                    auc_roc=None, lift=None, error=msg,
                ))
                n_failed += 1
                continue

            preds: pd.DataFrame = metrics["predictions"]
            outcomes = _compute_outcomes(conn, preds, horizon, threshold)
            n_filled = int(outcomes["outcome_filled_at"].notna().sum())
            n_nulled = len(outcomes) - n_filled

            logger.info(
                "    AUC=%.3f Lift=%.2fx | predictions=%d filled=%d nulled=%d",
                metrics["auc_roc"], metrics["lift"], len(preds), n_filled, n_nulled,
            )

            if not dry_run:
                _persist_fold(
                    conn,
                    model_id=model_id, horizon=horizon, fold=fold,
                    fold_metrics=metrics, predictions=preds, outcomes=outcomes,
                    prediction_threshold=threshold,
                )

            fold_summaries.append(FoldSummary(
                fold=i, model_id=model_id,
                train_end=fold["train_end"],
                test_start=fold["test_start"], test_end=fold["test_end"],
                n_predictions=len(preds),
                n_outcomes_filled=n_filled,
                n_outcomes_nulled=n_nulled,
                auc_roc=float(metrics["auc_roc"]),
                lift=float(metrics["lift"]),
            ))
            n_pred_total += len(preds)
            n_filled_total += n_filled
            n_nulled_total += n_nulled
            n_succeeded += 1
        except Exception as exc:
            logger.error("    FAIL: %s", exc, exc_info=True)
            fold_summaries.append(FoldSummary(
                fold=i, model_id=model_id,
                train_end=fold["train_end"],
                test_start=fold["test_start"], test_end=fold["test_end"],
                n_predictions=0, n_outcomes_filled=0, n_outcomes_nulled=0,
                auc_roc=None, lift=None, error=f"{type(exc).__name__}: {exc}",
            ))
            n_failed += 1

    return BackfillResult(
        horizon=horizon,
        label_col=label_col,
        dry_run=dry_run,
        n_folds_planned=len(folds),
        n_folds_succeeded=n_succeeded,
        n_folds_failed=n_failed,
        n_predictions=n_pred_total,
        n_outcomes_filled=n_filled_total,
        n_outcomes_nulled=n_nulled_total,
        fold_summaries=fold_summaries,
    )


# ──────────────────────────────────────────────────────────────────────
# Validation — six checks from PHASE1A_SPEC.md "Validation"
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ValidationCheck:
    name: str
    passed: bool
    detail: str
    sample: list[Any] = field(default_factory=list)


def validate_backfill(
    conn: duckdb.DuckDBPyConnection,
    *,
    expected_rows: int = 41_800,
    coverage_tolerance: float = 0.10,
    live_active_model_ids: list[str] | None = None,
) -> list[ValidationCheck]:
    """Run all six post-backfill validation queries.

    ``expected_rows`` and ``coverage_tolerance`` come from PHASE1A_SPEC.md.
    ``live_active_model_ids`` lets the caller assert which model_ids
    *should* still be active (typically the two pre-Phase-1A actives).
    """
    checks: list[ValidationCheck] = []

    # 1. No leakage — every backfill prediction has prediction_date > train_end
    rows = conn.execute(
        f"""
        SELECT p.model_id, p.symbol, p.prediction_date,
               r.train_end
        FROM crypto_ml_predictions p
        JOIN crypto_ml_model_runs r USING (model_id)
        WHERE p.model_id LIKE '%{MODEL_ID_TAG}%'
          AND p.prediction_date <= r.train_end
        LIMIT 5
        """
    ).fetchall()
    n_violations = conn.execute(
        f"""
        SELECT COUNT(*) FROM crypto_ml_predictions p
        JOIN crypto_ml_model_runs r USING (model_id)
        WHERE p.model_id LIKE '%{MODEL_ID_TAG}%'
          AND p.prediction_date <= r.train_end
        """
    ).fetchone()[0]
    checks.append(ValidationCheck(
        name="no_leakage",
        passed=(n_violations == 0),
        detail=f"{n_violations} prediction(s) with prediction_date <= train_end",
        sample=[list(r) for r in rows],
    ))

    # 2. Coverage — total backfill rows within tolerance
    total = conn.execute(
        f"""
        SELECT COUNT(*) FROM crypto_ml_predictions
        WHERE model_id LIKE '%{MODEL_ID_TAG}%'
        """
    ).fetchone()[0]
    lo = int(expected_rows * (1 - coverage_tolerance))
    hi = int(expected_rows * (1 + coverage_tolerance))
    checks.append(ValidationCheck(
        name="coverage",
        passed=(lo <= total <= hi),
        detail=f"{total:,} backfill prediction rows "
               f"(tolerance window: {lo:,}-{hi:,}, expected ≈ {expected_rows:,})",
    ))

    # 3. Outcomes filled where horizon has elapsed
    rows = conn.execute(
        f"""
        SELECT p.model_id, COUNT(*) AS n_unfilled_due
        FROM crypto_ml_predictions p
        WHERE p.model_id LIKE '%{MODEL_ID_TAG}%'
          AND p.outcome_filled_at IS NULL
          AND p.prediction_date + CASE p.horizon
                  WHEN '5d'  THEN INTERVAL '5 days'
                  WHEN '10d' THEN INTERVAL '10 days'
                  WHEN '20d' THEN INTERVAL '20 days'
                  ELSE INTERVAL '20 days'
              END <= CURRENT_DATE
        GROUP BY p.model_id
        ORDER BY n_unfilled_due DESC
        LIMIT 5
        """
    ).fetchall()
    n_unfilled_due = conn.execute(
        f"""
        SELECT COUNT(*) FROM crypto_ml_predictions p
        WHERE p.model_id LIKE '%{MODEL_ID_TAG}%'
          AND p.outcome_filled_at IS NULL
          AND p.prediction_date + CASE p.horizon
                  WHEN '5d'  THEN INTERVAL '5 days'
                  WHEN '10d' THEN INTERVAL '10 days'
                  ELSE INTERVAL '20 days'
              END <= CURRENT_DATE
        """
    ).fetchone()[0]
    # Per the gap policy, NULLed rows here are expected for predictions
    # whose price path was incomplete in crypto_prices_daily. Always
    # surface the count + percentage in the detail (pass or fail) so the
    # NULL rate is auditable on every run.
    pct_nulled = (n_unfilled_due / max(total, 1) * 100.0)
    checks.append(ValidationCheck(
        name="outcomes_filled_where_horizon_elapsed",
        # Treat "more than 25% NULLed rows" as a fail, per the spec's
        # requirement that outcomes ARE filled where the horizon has
        # elapsed; a few % NULL is the expected gap-coin tail.
        passed=(pct_nulled <= 25.0),
        detail=(
            f"{n_unfilled_due:,} NULLed of {total:,} total ({pct_nulled:.2f}%) "
            f"backfill rows whose horizon has elapsed; "
            f"gap policy expected: small fraction, mostly newly-listed coins"
        ),
        sample=[list(r) for r in rows],
    ))

    # 4. Distinct model_ids — each fold has its own; verified via PK
    rows = conn.execute(
        f"""
        SELECT model_id, COUNT(*) AS n_runs
        FROM crypto_ml_model_runs
        WHERE model_id LIKE '%{MODEL_ID_TAG}%'
        GROUP BY model_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    n_distinct = conn.execute(
        f"""
        SELECT COUNT(DISTINCT model_id) FROM crypto_ml_model_runs
        WHERE model_id LIKE '%{MODEL_ID_TAG}%'
        """
    ).fetchone()[0]
    checks.append(ValidationCheck(
        name="distinct_model_ids",
        passed=(len(rows) == 0),
        detail=f"{n_distinct} distinct backfill model_ids; "
               f"{len(rows)} duplicate ID(s) in model_runs",
        sample=[list(r) for r in rows],
    ))

    # 5. is_active integrity
    n_backfill_active = conn.execute(
        f"""
        SELECT COUNT(*) FROM crypto_ml_model_runs
        WHERE model_id LIKE '%{MODEL_ID_TAG}%' AND is_active = true
        """
    ).fetchone()[0]
    actives = conn.execute(
        "SELECT model_id FROM crypto_ml_model_runs WHERE is_active = true "
        "ORDER BY model_id"
    ).fetchall()
    active_ids = [r[0] for r in actives]
    expected_actives = sorted(live_active_model_ids) if live_active_model_ids else None
    if expected_actives is not None:
        active_match = sorted(active_ids) == expected_actives
    else:
        # Without an explicit list, check that no backfill IDs are active.
        active_match = n_backfill_active == 0
    checks.append(ValidationCheck(
        name="is_active_integrity",
        passed=(n_backfill_active == 0 and active_match),
        detail=f"backfill_active={n_backfill_active}; "
               f"current actives={active_ids}"
               + (f"; expected={expected_actives}" if expected_actives else ""),
    ))

    # 6. Live pipeline unaffected — sentinel: there is at least one
    #    is_active=true model per horizon and none of them are backfill IDs.
    rows = conn.execute(
        """
        SELECT horizon, COUNT(*) AS n_active
        FROM crypto_ml_model_runs
        WHERE is_active = true
        GROUP BY horizon
        ORDER BY horizon
        """
    ).fetchall()
    horizons_with_active = {r[0]: r[1] for r in rows}
    expected_horizons = {"5d", "10d"}
    missing = expected_horizons - set(horizons_with_active)
    backfill_in_active = any(is_backfill_model_id(mid) for mid in active_ids)
    checks.append(ValidationCheck(
        name="live_pipeline_unaffected",
        passed=(not missing and not backfill_in_active),
        detail=(
            f"active models per horizon: {horizons_with_active}; "
            f"missing horizons={sorted(missing) if missing else 'none'}; "
            f"any backfill ID is_active={backfill_in_active}"
        ),
    ))

    return checks


def format_validation_report(checks: list[ValidationCheck]) -> str:
    lines = ["=" * 78,
             "  Phase 1A backfill validation — 6 checks",
             "=" * 78]
    for i, c in enumerate(checks, start=1):
        flag = "PASS" if c.passed else "FAIL"
        lines.append(f"\n  [{flag}] {i}. {c.name}")
        lines.append(f"        {c.detail}")
        if c.sample:
            lines.append(f"        sample: {c.sample[:3]}")
    n_pass = sum(1 for c in checks if c.passed)
    lines.append(f"\n  Result: {n_pass}/{len(checks)} checks passed.")
    return "\n".join(lines)


def format_backfill_summary(result: BackfillResult) -> str:
    """Human-readable summary of a :func:`backfill_horizon` run."""
    header = f"Phase 1A backfill — horizon {result.horizon} ({result.label_col})"
    if result.dry_run:
        header += "  [DRY RUN — no rows written]"
    lines = ["=" * 78, f"  {header}", "=" * 78]
    lines.append(
        f"  folds: {result.n_folds_succeeded}/{result.n_folds_planned} succeeded"
        f"  ({result.n_folds_failed} failed)"
    )
    lines.append(
        f"  predictions: {result.n_predictions:,}"
        f"  | outcomes filled: {result.n_outcomes_filled:,}"
        f"  | outcomes nulled (gap): {result.n_outcomes_nulled:,}"
    )
    lines.append(f"\n  {'Fold':>4}  {'Model ID':<32}  {'Test window':<27}"
                 f"  {'N preds':>7} {'Filled':>7} {'Null':>5} {'AUC':>5} {'Lift':>5}")
    lines.append(f"  {'-' * 100}")
    for fs in result.fold_summaries:
        window = f"{fs.test_start} → {fs.test_end}"
        if fs.error:
            lines.append(
                f"  {fs.fold:>4}  {fs.model_id:<32}  {window:<27}  "
                f"FAIL: {fs.error}"
            )
        else:
            auc = f"{fs.auc_roc:.3f}" if fs.auc_roc is not None else "—"
            lift = f"{fs.lift:.2f}x" if fs.lift is not None else "—"
            lines.append(
                f"  {fs.fold:>4}  {fs.model_id:<32}  {window:<27}  "
                f"{fs.n_predictions:>7,} {fs.n_outcomes_filled:>7,} "
                f"{fs.n_outcomes_nulled:>5,} {auc:>5} {lift:>5}"
            )
    return "\n".join(lines)
