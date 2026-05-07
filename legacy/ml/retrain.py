"""Weekly retrain orchestration.

Per architecture doc section 10:
1. Recompute labels to include latest forward returns
2. Recompute features for new dates
3. Train new model on all available data (expanding window)
4. Evaluate on most recent month as holdout
5. Compare against current active model
6. Promote if not significantly worse (precision >= 0.95 * current)
7. Store model run
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.schema import create_all_tables
from ml.train import FEATURE_COLS, DEFAULT_PARAMS, _load_dataset, _fill_nulls, MODELS_DIR

logger = logging.getLogger("mhde.ml.retrain")

CONFIGS = [
    {"label_col": "label_20d_5pct", "horizon": "20d", "threshold": 0.05},
    {"label_col": "label_10d_5pct", "horizon": "10d", "threshold": 0.05},
    {"label_col": "label_5d_3pct", "horizon": "5d", "threshold": 0.03},
]


def retrain_all(conn: duckdb.DuckDBPyConnection) -> dict:
    """Run the full weekly retrain for all horizons."""
    create_all_tables(conn)
    os.makedirs(MODELS_DIR, exist_ok=True)

    logger.info("=== WEEKLY RETRAIN START ===")

    # Step 1: Recompute labels
    logger.info("Step 1: Recomputing labels...")
    from ml.labels import compute_labels
    n_labels = compute_labels(conn)
    logger.info("  Labels: %d rows", n_labels)

    # Step 2: Recompute features
    logger.info("Step 2: Recomputing features...")
    from ml.features import compute_features
    n_features = compute_features(conn)
    logger.info("  Features: %d rows", n_features)

    # Step 3-7: Train and evaluate each horizon
    results = {}
    for cfg in CONFIGS:
        result = _retrain_single(conn, cfg["label_col"], cfg["horizon"], cfg["threshold"])
        results[cfg["horizon"]] = result

    logger.info("=== WEEKLY RETRAIN COMPLETE ===")
    for horizon, res in results.items():
        logger.info("  %s: %s (lift=%.2f)", horizon, res["action"], res.get("new_lift", 0))

    return results


def _retrain_single(
    conn: duckdb.DuckDBPyConnection,
    label_col: str,
    horizon: str,
    threshold: float,
) -> dict:
    """Retrain a single horizon model and decide whether to promote."""
    logger.info("Retraining %s (label=%s, threshold=%.2f)", horizon, label_col, threshold)

    # Get current active model metrics
    current = conn.execute("""
        SELECT model_id, precision_at_threshold, auc_roc, lift_over_base
        FROM ml_model_runs
        WHERE is_active = true AND horizon = ?
    """, [horizon]).fetchone()

    current_precision = current[1] if current else 0
    current_lift = current[3] if current else 0
    current_model_id = current[0] if current else None

    # Determine date ranges
    max_date = conn.execute("""
        SELECT MAX(trade_date) FROM ml_features f
        JOIN ml_labels l ON f.ticker = l.ticker AND f.trade_date = l.trade_date
        WHERE l.{} IS NOT NULL
    """.format(label_col)).fetchone()[0]

    if max_date is None:
        logger.error("  No data available for %s", label_col)
        return {"action": "skip", "reason": "no data"}

    # Use last month as holdout test set
    test_start = max_date.replace(day=1)
    train_end_raw = test_start - pd.Timedelta(days=1)
    train_end = train_end_raw.date() if hasattr(train_end_raw, 'date') else train_end_raw

    train_start = "2025-05-01"

    logger.info("  Train: %s → %s, Test: %s → %s", train_start, train_end, test_start, max_date)

    # Load data
    X_train_raw, y_train = _load_dataset(conn, train_start, str(train_end), label_col)
    X_test_raw, y_test = _load_dataset(conn, str(test_start), str(max_date), label_col)

    if len(X_train_raw) < 500 or len(X_test_raw) < 50:
        logger.warning("  Insufficient data (train=%d, test=%d)", len(X_train_raw), len(X_test_raw))
        return {"action": "skip", "reason": "insufficient data"}

    # Train
    X_fit, X_cal, y_fit, y_cal = train_test_split(
        X_train_raw, y_train, test_size=0.15, random_state=42, stratify=y_train
    )

    medians = X_fit.median()
    X_fit = X_fit.fillna(medians)
    X_cal = X_cal.fillna(medians)
    X_test = X_test_raw.fillna(medians)

    n_pos = y_fit.sum()
    n_neg = len(y_fit) - n_pos
    scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0

    params = {**DEFAULT_PARAMS, "scale_pos_weight": scale_pos_weight}
    model = XGBClassifier(**params)
    model.fit(X_fit, y_fit, eval_set=[(X_cal, y_cal)], verbose=False)

    # Calibrate
    raw_cal_probs = model.predict_proba(X_cal)[:, 1].reshape(-1, 1)
    platt = LogisticRegression(C=1e10, solver="lbfgs", max_iter=1000)
    platt.fit(raw_cal_probs, y_cal)

    # Evaluate on test set
    raw_test_probs = model.predict_proba(X_test)[:, 1].reshape(-1, 1)
    probs = platt.predict_proba(raw_test_probs)[:, 1]

    base_rate = y_test.mean()

    # Find optimal threshold
    best_precision = 0.0
    best_thresh = 0.5
    for t in np.arange(0.3, 0.8, 0.02):
        preds = (probs >= t).astype(int)
        if preds.sum() < 10:
            continue
        prec = y_test[preds == 1].mean()
        rec = y_test[preds == 1].sum() / y_test.sum() if y_test.sum() > 0 else 0
        if rec >= 0.10 and prec > best_precision:
            best_precision = prec
            best_thresh = t

    preds_at_thresh = (probs >= best_thresh).astype(int)
    n_flagged = preds_at_thresh.sum()
    precision = y_test[preds_at_thresh == 1].mean() if n_flagged > 0 else 0
    recall = (y_test[preds_at_thresh == 1].sum() / y_test.sum()) if (y_test.sum() > 0 and n_flagged > 0) else 0
    lift = precision / base_rate if base_rate > 0 else 0

    try:
        auc = roc_auc_score(y_test, probs)
    except ValueError:
        auc = 0.5

    logger.info("  New model: precision=%.3f, lift=%.2f, AUC=%.3f (base_rate=%.3f)",
                precision, lift, auc, base_rate)
    logger.info("  Current model: precision=%.3f, lift=%.2f",
                current_precision, current_lift)

    # Decision: promote if not significantly worse
    promote = precision >= current_precision * 0.95 if current_precision > 0 else True

    if promote:
        # Save new model
        model_id = f"{horizon}_{label_col}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        model_path = os.path.join(MODELS_DIR, f"{model_id}.joblib")
        joblib.dump({
            "model": model,
            "platt": platt,
            "medians": medians.to_dict(),
            "feature_cols": FEATURE_COLS,
            "threshold": threshold,
            "horizon": horizon,
            "label_col": label_col,
        }, model_path)

        # Deactivate current model
        if current_model_id:
            conn.execute("UPDATE ml_model_runs SET is_active = false WHERE model_id = ?",
                         [current_model_id])

        # Feature importance
        importance = dict(zip(FEATURE_COLS, model.feature_importances_.tolist()))

        # Insert new model run
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        conn.execute("""
            INSERT INTO ml_model_runs (
                model_id, horizon, target_threshold,
                train_start, train_end, test_start, test_end,
                n_train_samples, n_test_samples, n_positive_train, n_positive_test,
                precision_at_threshold, recall_at_threshold, f1_score, auc_roc,
                base_rate, lift_over_base, feature_importance_json, model_path, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            model_id, horizon, threshold,
            date.fromisoformat(train_start), train_end,
            test_start, max_date,
            len(X_train_raw), len(X_test_raw),
            int(y_train.sum()), int(y_test.sum()),
            float(precision), float(recall), float(f1), float(auc),
            float(base_rate), float(lift),
            json.dumps(importance), model_path, True,
        ])

        logger.info("  PROMOTED: %s (precision %.3f >= %.3f * 0.95 = %.3f)",
                    model_id, precision, current_precision, current_precision * 0.95)
        return {
            "action": "promoted",
            "model_id": model_id,
            "new_precision": precision,
            "new_lift": lift,
            "new_auc": auc,
        }
    else:
        logger.warning("  KEPT CURRENT: new precision %.3f < %.3f (threshold %.3f)",
                       precision, current_precision * 0.95, current_precision)
        return {
            "action": "kept_current",
            "reason": f"new precision {precision:.3f} < {current_precision * 0.95:.3f}",
            "new_precision": precision,
            "new_lift": lift,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = duckdb.connect("data/mhde.duckdb")
    try:
        retrain_all(conn)
    finally:
        conn.close()
