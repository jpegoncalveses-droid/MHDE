"""XGBoost walk-forward training with probability calibration for crypto.

Trains one model per horizon/threshold combo using expanding-window CV.
Stores results in crypto_ml_model_runs.
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
from dateutil.relativedelta import relativedelta
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from crypto.schema import create_all_tables
from crypto.config import FEATURE_COLS, DEFAULT_PARAMS, MODELS_DIR

logger = logging.getLogger("mhde.crypto.train")

TRAIN_START = "2024-01-01"
MIN_TRAIN_DAYS = 180


def _build_walk_forward_folds(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Build walk-forward folds from available data range."""
    date_range = conn.execute("""
        SELECT MIN(trade_date) AS min_date, MAX(trade_date) AS max_date
        FROM crypto_ml_features
    """).fetchone()
    min_date, max_date = date_range

    folds = []
    train_start = max(min_date, date.fromisoformat(TRAIN_START))
    first_test_start = train_start + relativedelta(months=6)

    current_test_start = first_test_start
    while current_test_start < max_date:
        test_end = current_test_start + relativedelta(months=1) - relativedelta(days=1)
        if test_end > max_date:
            test_end = max_date
        train_end = current_test_start - relativedelta(days=1)
        folds.append({
            "train_end": train_end.isoformat(),
            "test_start": current_test_start.isoformat(),
            "test_end": test_end.isoformat(),
        })
        current_test_start += relativedelta(months=1)

    return folds


def _load_dataset(conn: duckdb.DuckDBPyConnection, start_date: str, end_date: str,
                  label_col: str) -> tuple[pd.DataFrame, pd.Series]:
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    query = f"""
        SELECT f.symbol, f.trade_date, {feature_select}, l.{label_col} AS label
        FROM crypto_ml_features f
        JOIN crypto_ml_labels l ON f.symbol = l.symbol AND f.trade_date = l.trade_date
        WHERE f.trade_date >= '{start_date}'
          AND f.trade_date <= '{end_date}'
          AND l.{label_col} IS NOT NULL
    """
    df = conn.execute(query).fetchdf()
    X = df[FEATURE_COLS].copy()
    y = df["label"].astype(int)
    return X, y


def _train_single_fold(X_train: pd.DataFrame, y_train: pd.Series,
                       X_test: pd.DataFrame, y_test: pd.Series) -> dict:
    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    if n_pos < 10:
        return {"error": "too few positive samples", "n_pos": n_pos}

    params = DEFAULT_PARAMS.copy()
    params["scale_pos_weight"] = n_neg / max(n_pos, 1)

    X_fit, X_cal, y_fit, y_cal = train_test_split(
        X_train, y_train, test_size=0.2, random_state=42, stratify=y_train
    )

    medians = X_fit.median().to_dict()
    for col in FEATURE_COLS:
        X_fit[col] = X_fit[col].fillna(medians.get(col, 0))
        X_cal[col] = X_cal[col].fillna(medians.get(col, 0))
        X_test = X_test.copy()
    for col in FEATURE_COLS:
        X_test[col] = X_test[col].fillna(medians.get(col, 0))

    model = XGBClassifier(**params)
    model.fit(X_fit, y_fit, eval_set=[(X_cal, y_cal)], verbose=False)

    raw_probs = model.predict_proba(X_cal)[:, 1].reshape(-1, 1)
    platt = LogisticRegression(max_iter=1000)
    platt.fit(raw_probs, y_cal)

    test_raw = model.predict_proba(X_test)[:, 1].reshape(-1, 1)
    test_probs = platt.predict_proba(test_raw)[:, 1]

    base_rate = float(y_test.mean())
    top_n = min(10, max(1, int(len(y_test) * 0.05)))
    top_idx = np.argsort(test_probs)[-top_n:]
    precision_top = float(y_test.iloc[top_idx].mean())

    preds_binary = (test_probs >= 0.5).astype(int)
    if preds_binary.sum() > 0:
        precision_val = float(precision_score(y_test, preds_binary, zero_division=0))
        recall_val = float(recall_score(y_test, preds_binary, zero_division=0))
        f1_val = float(f1_score(y_test, preds_binary, zero_division=0))
    else:
        precision_val = recall_val = f1_val = 0.0

    try:
        auc = float(roc_auc_score(y_test, test_probs))
    except ValueError:
        auc = 0.5

    importance = dict(zip(FEATURE_COLS, model.feature_importances_.tolist()))

    return {
        "model": model,
        "platt": platt,
        "medians": medians,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "n_pos_train": n_pos,
        "n_pos_test": int(y_test.sum()),
        "base_rate": base_rate,
        "precision_top": precision_top,
        "precision": precision_val,
        "recall": recall_val,
        "f1": f1_val,
        "auc_roc": auc,
        "lift": precision_top / base_rate if base_rate > 0 else 0,
        "feature_importance": importance,
    }


def train_walk_forward(conn: duckdb.DuckDBPyConnection, label_col: str = "label_10d_10pct",
                       horizon: str = "10d", threshold: float = 0.10) -> list[dict]:
    create_all_tables(conn)
    folds = _build_walk_forward_folds(conn)
    logger.info("Walk-forward CV: %d folds for %s (%s, threshold=%.2f)", len(folds), label_col, horizon, threshold)

    results = []
    for i, fold in enumerate(folds):
        logger.info("  Fold %d: train to %s, test %s -> %s",
                    i + 1, fold["train_end"], fold["test_start"], fold["test_end"])

        X_train, y_train = _load_dataset(conn, TRAIN_START, fold["train_end"], label_col)
        X_test, y_test = _load_dataset(conn, fold["test_start"], fold["test_end"], label_col)

        if len(X_test) < 10:
            logger.warning("    Fold %d: too few test samples (%d), skipping", i + 1, len(X_test))
            continue

        fold_result = _train_single_fold(X_train, y_train, X_test, y_test)
        fold_result["fold"] = i + 1
        fold_result.update(fold)
        results.append(fold_result)

        if "error" not in fold_result:
            logger.info("    AUC=%.3f  Lift=%.2fx  Prec@Top=%.1f%%  Base=%.1f%%",
                        fold_result["auc_roc"], fold_result["lift"],
                        fold_result["precision_top"] * 100, fold_result["base_rate"] * 100)

    if not results or all("error" in r for r in results):
        logger.error("No successful folds. Cannot train final model.")
        return results

    valid_results = [r for r in results if "error" not in r]
    last_fold = folds[-1]
    X_all, y_all = _load_dataset(conn, TRAIN_START, last_fold["train_end"], label_col)
    X_test_final, y_test_final = _load_dataset(conn, last_fold["test_start"], last_fold["test_end"], label_col)

    final = _train_single_fold(X_all, y_all, X_test_final, y_test_final)

    if "error" in final:
        logger.error("Final model training failed: %s", final["error"])
        return results

    os.makedirs(MODELS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_path = os.path.join(MODELS_DIR, f"crypto_{horizon}_{label_col}_{timestamp}.joblib")
    model_id = f"crypto_{horizon}_{uuid.uuid4().hex[:8]}"

    joblib.dump({
        "model": final["model"],
        "platt": final["platt"],
        "medians": final["medians"],
        "feature_cols": FEATURE_COLS,
        "threshold": threshold,
        "horizon": horizon,
        "label_col": label_col,
    }, model_path)

    avg_auc = float(np.mean([r["auc_roc"] for r in valid_results]))
    avg_lift = float(np.mean([r["lift"] for r in valid_results]))
    avg_precision = float(np.mean([r["precision_top"] for r in valid_results]))
    avg_base = float(np.mean([r["base_rate"] for r in valid_results]))

    conn.execute("UPDATE crypto_ml_model_runs SET is_active = false WHERE horizon = ?", [horizon])

    conn.execute("""
        INSERT INTO crypto_ml_model_runs (
            model_id, horizon, target_threshold, train_start, train_end, test_start, test_end,
            n_train_samples, n_test_samples, n_positive_train, n_positive_test,
            precision_at_threshold, recall_at_threshold, f1_score, auc_roc,
            base_rate, lift_over_base, feature_importance_json, model_path, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true)
    """, [
        model_id, horizon, threshold, TRAIN_START, last_fold["train_end"],
        last_fold["test_start"], last_fold["test_end"],
        final["n_train"], final["n_test"], final["n_pos_train"], final["n_pos_test"],
        avg_precision, final["recall"], final["f1"], avg_auc,
        avg_base, avg_lift, json.dumps(final["feature_importance"]), model_path,
    ])

    logger.info("Final model saved: %s (AUC=%.3f, Lift=%.2fx)", model_id, avg_auc, avg_lift)
    return results
