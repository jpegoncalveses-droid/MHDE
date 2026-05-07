"""XGBoost walk-forward training with probability calibration.

Trains one model per horizon/threshold combo using expanding-window cross-validation.
Stores results in ml_model_runs.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from ml.schema import create_all_tables

logger = logging.getLogger("mhde.ml.train")

MODELS_DIR = "models/saved"

WALK_FORWARD_FOLDS = [
    {"train_end": "2025-10-31", "test_start": "2025-11-01", "test_end": "2025-11-30"},
    {"train_end": "2025-11-30", "test_start": "2025-12-01", "test_end": "2025-12-31"},
    {"train_end": "2025-12-31", "test_start": "2026-01-01", "test_end": "2026-01-31"},
    {"train_end": "2026-01-31", "test_start": "2026-02-01", "test_end": "2026-02-28"},
    {"train_end": "2026-02-28", "test_start": "2026-03-01", "test_end": "2026-03-31"},
    {"train_end": "2026-03-31", "test_start": "2026-04-01", "test_end": "2026-04-30"},
]

FEATURE_COLS = [
    "return_5d", "return_10d", "return_20d", "return_60d",
    "rsi_14d", "drawdown_from_52w_high", "price_vs_50d_ma", "price_vs_200d_ma",
    "bollinger_position", "close_in_range", "gap_from_prev_close",
    "realized_vol_20d", "realized_vol_60d", "vol_ratio", "atr_pct_20d",
    "relative_volume_20d", "volume_trend_5d",
    "return_vs_spy_5d", "return_vs_spy_20d",
    "return_vs_sector_5d", "return_vs_sector_20d",
    "beta_60d",
    "vix_level", "vix_change_5d", "yield_curve_10y_2y",
    "filing_8k_count_7d", "filing_8k_count_30d",
    "filing_form4_count_7d", "filing_form4_count_14d",
    "days_since_last_10q",
    "market_cap_log", "pb_ratio",
]

DEFAULT_PARAMS = {
    "n_estimators": 200,
    "max_depth": 4,
    "learning_rate": 0.05,
    "min_child_weight": 20,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "eval_metric": "logloss",
    "random_state": 42,
    "verbosity": 0,
    "n_jobs": -1,
}


def _load_dataset(conn: duckdb.DuckDBPyConnection, start_date: str, end_date: str,
                  label_col: str) -> tuple[pd.DataFrame, pd.Series]:
    """Load features + label for a date range. Returns (X, y) with NaN-filled features."""
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    query = f"""
        SELECT f.ticker, f.trade_date, {feature_select}, l.{label_col} AS label
        FROM ml_features f
        JOIN ml_labels l ON f.ticker = l.ticker AND f.trade_date = l.trade_date
        WHERE f.trade_date >= '{start_date}'
          AND f.trade_date <= '{end_date}'
          AND l.{label_col} IS NOT NULL
    """
    df = conn.execute(query).fetchdf()
    X = df[FEATURE_COLS].copy()
    y = df["label"].astype(int)
    return X, y


def _fill_nulls(X_train: pd.DataFrame, X_test: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Fill NaN with training set median. Returns filled frames and the median dict."""
    medians = X_train.median()
    X_train_filled = X_train.fillna(medians)
    X_test_filled = X_test.fillna(medians)
    return X_train_filled, X_test_filled, medians.to_dict()


def train_walk_forward(
    conn: duckdb.DuckDBPyConnection,
    label_col: str = "label_20d_5pct",
    horizon: str = "20d",
    threshold: float = 0.05,
) -> list[dict]:
    """Run walk-forward cross-validation. Returns list of fold results."""
    create_all_tables(conn)
    os.makedirs(MODELS_DIR, exist_ok=True)

    results = []
    train_start = "2025-05-01"

    for fold_idx, fold in enumerate(WALK_FORWARD_FOLDS):
        logger.info("Fold %d: train [%s -> %s], test [%s -> %s]",
                    fold_idx + 1, train_start, fold["train_end"],
                    fold["test_start"], fold["test_end"])

        X_train_raw, y_train = _load_dataset(conn, train_start, fold["train_end"], label_col)
        X_test_raw, y_test = _load_dataset(conn, fold["test_start"], fold["test_end"], label_col)

        if len(X_train_raw) < 100 or len(X_test_raw) < 50:
            logger.warning("  Skipping fold %d: insufficient data (train=%d, test=%d)",
                           fold_idx + 1, len(X_train_raw), len(X_test_raw))
            continue

        X_train, X_test, medians = _fill_nulls(X_train_raw, X_test_raw)

        n_pos = y_train.sum()
        n_neg = len(y_train) - n_pos
        scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0

        params = {**DEFAULT_PARAMS, "scale_pos_weight": scale_pos_weight}

        # Split training into fit + calibration sets
        X_fit, X_cal, y_fit, y_cal = train_test_split(
            X_train, y_train, test_size=0.2, random_state=42, stratify=y_train
        )

        # Train with early stopping
        model = XGBClassifier(**params)
        model.fit(
            X_fit, y_fit,
            eval_set=[(X_cal, y_cal)],
            verbose=False,
        )

        # Platt scaling calibration on calibration set
        raw_cal_probs = model.predict_proba(X_cal)[:, 1].reshape(-1, 1)
        platt = LogisticRegression(C=1e10, solver="lbfgs", max_iter=1000)
        platt.fit(raw_cal_probs, y_cal)

        # Predict on test set
        raw_test_probs = model.predict_proba(X_test)[:, 1].reshape(-1, 1)
        probs = platt.predict_proba(raw_test_probs)[:, 1]

        # Evaluate
        fold_result = _evaluate_fold(y_test, probs, fold_idx + 1, fold)

        # Feature importance
        importance = dict(zip(FEATURE_COLS, model.feature_importances_.tolist()))
        fold_result["feature_importance"] = importance
        fold_result["n_train"] = len(X_train)
        fold_result["n_test"] = len(X_test)
        fold_result["n_pos_train"] = int(n_pos)
        fold_result["n_pos_test"] = int(y_test.sum())
        fold_result["medians"] = medians

        results.append(fold_result)

        logger.info("  Fold %d: precision@top20=%.3f, lift=%.2f, AUC=%.3f",
                    fold_idx + 1, fold_result["precision_top_20"],
                    fold_result["lift_over_base"], fold_result["auc_roc"])

    # Train final model on all data up to last fold's train_end
    if results:
        logger.info("Training final production model...")
        final_result = _train_final_model(conn, label_col, horizon, threshold, results)
        results.append({"final_model": final_result})

    return results


def _evaluate_fold(y_test: pd.Series, probs: np.ndarray, fold_num: int, fold: dict) -> dict:
    """Compute evaluation metrics for a single fold."""
    from sklearn.metrics import roc_auc_score, precision_score, recall_score, f1_score

    base_rate = y_test.mean()

    # Top 20 predictions
    top_20_idx = np.argsort(probs)[-20:]
    precision_top_20 = y_test.iloc[top_20_idx].mean()

    # Optimal threshold: find threshold that gives best precision with recall > 0.15
    best_threshold = 0.5
    best_precision = 0.0
    for t in np.arange(0.3, 0.8, 0.02):
        preds = (probs >= t).astype(int)
        if preds.sum() < 10:
            continue
        prec = y_test[preds == 1].mean()
        rec = y_test[preds == 1].sum() / y_test.sum() if y_test.sum() > 0 else 0
        if rec >= 0.10 and prec > best_precision:
            best_precision = prec
            best_threshold = t

    preds_at_thresh = (probs >= best_threshold).astype(int)
    n_flagged = preds_at_thresh.sum()

    if n_flagged > 0:
        precision_at_thresh = y_test[preds_at_thresh == 1].mean()
        recall_at_thresh = y_test[preds_at_thresh == 1].sum() / y_test.sum() if y_test.sum() > 0 else 0
    else:
        precision_at_thresh = 0.0
        recall_at_thresh = 0.0

    try:
        auc = roc_auc_score(y_test, probs)
    except ValueError:
        auc = 0.5

    lift = precision_at_thresh / base_rate if base_rate > 0 else 0

    return {
        "fold": fold_num,
        "test_start": fold["test_start"],
        "test_end": fold["test_end"],
        "base_rate": float(base_rate),
        "precision_top_20": float(precision_top_20),
        "precision_at_threshold": float(precision_at_thresh),
        "recall_at_threshold": float(recall_at_thresh),
        "optimal_threshold": float(best_threshold),
        "n_flagged": int(n_flagged),
        "lift_over_base": float(lift),
        "auc_roc": float(auc),
    }


def _train_final_model(
    conn: duckdb.DuckDBPyConnection,
    label_col: str,
    horizon: str,
    threshold: float,
    fold_results: list[dict],
) -> dict:
    """Train final model on all available data and save."""
    last_fold = WALK_FORWARD_FOLDS[-1]
    X_all, y_all = _load_dataset(conn, "2025-05-01", last_fold["train_end"], label_col)

    X_train, X_cal, y_train, y_cal = train_test_split(
        X_all, y_all, test_size=0.15, random_state=42, stratify=y_all
    )

    medians = X_train.median()
    X_train = X_train.fillna(medians)
    X_cal = X_cal.fillna(medians)

    n_pos = y_train.sum()
    n_neg = len(y_train) - n_pos
    scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0

    params = {**DEFAULT_PARAMS, "scale_pos_weight": scale_pos_weight}
    model = XGBClassifier(**params)
    model.fit(X_train, y_train, eval_set=[(X_cal, y_cal)], verbose=False)

    # Platt scaling calibration
    raw_cal_probs = model.predict_proba(X_cal)[:, 1].reshape(-1, 1)
    platt = LogisticRegression(C=1e10, solver="lbfgs", max_iter=1000)
    platt.fit(raw_cal_probs, y_cal)

    # Save model
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

    # Average metrics across folds
    avg_precision = np.mean([r["precision_at_threshold"] for r in fold_results])
    avg_recall = np.mean([r["recall_at_threshold"] for r in fold_results])
    avg_auc = np.mean([r["auc_roc"] for r in fold_results])
    avg_lift = np.mean([r["lift_over_base"] for r in fold_results])
    avg_base = np.mean([r["base_rate"] for r in fold_results])

    # Feature importance from final model
    importance = dict(zip(FEATURE_COLS, model.feature_importances_.tolist()))

    # KI-003 promotion: deactivate any prior is_active row for the same
    # (horizon, target_threshold) tuple before inserting the new one.
    # crypto/ml/train.py and fx/ml/train.py do this; equity didn't,
    # which is what caused KI-009's "old + new both active" state during
    # the manual retrain on 2026-05-07.
    conn.execute(
        "UPDATE ml_model_runs SET is_active = false "
        "WHERE horizon = ? AND target_threshold = ? AND is_active = true",
        [horizon, threshold],
    )

    # Store in ml_model_runs
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
        date.fromisoformat("2025-05-01"),
        date.fromisoformat(last_fold["train_end"]),
        date.fromisoformat(WALK_FORWARD_FOLDS[0]["test_start"]),
        date.fromisoformat(last_fold["test_end"]),
        len(X_all), sum(r["n_test"] for r in fold_results),
        int(y_all.sum()), sum(r["n_pos_test"] for r in fold_results),
        avg_precision, avg_recall,
        2 * avg_precision * avg_recall / (avg_precision + avg_recall) if (avg_precision + avg_recall) > 0 else 0,
        avg_auc, avg_base, avg_lift,
        json.dumps(importance), model_path, True,
    ])

    logger.info("Final model saved: %s (avg lift=%.2f, avg AUC=%.3f)", model_id, avg_lift, avg_auc)
    return {
        "model_id": model_id,
        "model_path": model_path,
        "avg_precision": avg_precision,
        "avg_recall": avg_recall,
        "avg_auc": avg_auc,
        "avg_lift": avg_lift,
        "feature_importance": importance,
    }
