"""Walk-forward training for FX directional models.

Trains 4 models: up_24h, down_24h, up_48h, down_48h
using expanding-window walk-forward CV with yearly test folds.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from fx.config import FEATURE_COLS, DEFAULT_PARAMS, MODELS_DIR, CV_TEST_YEARS, TRAIN_START_YEAR
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.train")

MODEL_CONFIGS = [
    {"direction": "up", "horizon": "24h", "label_col": "label_up_20pip_24h", "target_pips": 20},
    {"direction": "down", "horizon": "24h", "label_col": "label_down_20pip_24h", "target_pips": 20},
    {"direction": "up", "horizon": "48h", "label_col": "label_up_20pip_48h", "target_pips": 20},
    {"direction": "down", "horizon": "48h", "label_col": "label_down_20pip_48h", "target_pips": 20},
]


def _load_dataset(conn: duckdb.DuckDBPyConnection, start_year: int, end_year: int,
                  label_col: str) -> tuple[pd.DataFrame, pd.Series]:
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    query = f"""
        SELECT f.datetime_utc, {feature_select}, l.{label_col} AS label
        FROM fx_ml_features f
        JOIN fx_ml_labels l ON f.datetime_utc = l.datetime_utc
        WHERE EXTRACT(YEAR FROM f.datetime_utc) >= {start_year}
          AND EXTRACT(YEAR FROM f.datetime_utc) <= {end_year}
          AND l.{label_col} IS NOT NULL
    """
    df = conn.execute(query).fetchdf()
    if df.empty:
        return pd.DataFrame(columns=FEATURE_COLS), pd.Series(dtype=int)
    X = df[FEATURE_COLS].copy()
    y = df["label"].astype(int)
    return X, y


def _train_single_fold(X_train, y_train, X_test, y_test) -> dict:
    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    if n_pos < 50:
        return {"error": "too few positive samples", "n_pos": n_pos}

    params = DEFAULT_PARAMS.copy()
    params["scale_pos_weight"] = n_neg / max(n_pos, 1)

    X_fit, X_cal, y_fit, y_cal = train_test_split(
        X_train, y_train, test_size=0.15, random_state=42, stratify=y_train
    )

    medians = X_fit.median().to_dict()
    for col in FEATURE_COLS:
        X_fit[col] = X_fit[col].fillna(medians.get(col, 0))
        X_cal[col] = X_cal[col].fillna(medians.get(col, 0))
        X_test[col] = X_test[col].fillna(medians.get(col, 0))

    model = XGBClassifier(**params)
    model.fit(X_fit, y_fit, eval_set=[(X_cal, y_cal)], verbose=False)

    raw_probs = model.predict_proba(X_cal)[:, 1].reshape(-1, 1)
    platt = LogisticRegression(max_iter=1000)
    platt.fit(raw_probs, y_cal)

    test_raw = model.predict_proba(X_test)[:, 1].reshape(-1, 1)
    test_probs = platt.predict_proba(test_raw)[:, 1]

    from sklearn.metrics import roc_auc_score

    base_rate = float(y_test.mean())
    top_n = max(1, int(len(y_test) * 0.10))
    top_idx = np.argsort(test_probs)[-top_n:]
    precision_top = float(y_test.iloc[top_idx].mean())

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
        "precision_top10": precision_top,
        "auc_roc": auc,
        "lift": precision_top / base_rate if base_rate > 0 else 0,
        "feature_importance": importance,
    }


def train_model(conn: duckdb.DuckDBPyConnection, direction: str, horizon: str,
                label_col: str, target_pips: float) -> list[dict]:
    create_all_tables(conn)
    logger.info("Training %s_%s (label: %s, target: %d pips)", direction, horizon, label_col, target_pips)

    results = []
    for test_year in CV_TEST_YEARS:
        train_end_year = test_year - 1
        logger.info("  Fold: train %d-%d, test %d", TRAIN_START_YEAR, train_end_year, test_year)

        X_train, y_train = _load_dataset(conn, TRAIN_START_YEAR, train_end_year, label_col)
        X_test, y_test = _load_dataset(conn, test_year, test_year, label_col)

        if len(X_test) < 100:
            logger.warning("    Too few test samples (%d), skipping", len(X_test))
            continue

        fold_result = _train_single_fold(X_train, y_train, X_test, y_test)
        fold_result["test_year"] = test_year
        results.append(fold_result)

        if "error" not in fold_result:
            logger.info("    AUC=%.3f  Lift=%.2fx  Prec@Top10=%.1f%%  Base=%.1f%%",
                        fold_result["auc_roc"], fold_result["lift"],
                        fold_result["precision_top10"] * 100, fold_result["base_rate"] * 100)

    valid = [r for r in results if "error" not in r]
    if not valid:
        logger.error("No successful folds for %s_%s", direction, horizon)
        return results

    # Train final model on all data through last test year minus 1
    last_train_year = CV_TEST_YEARS[-1] - 1
    X_all, y_all = _load_dataset(conn, TRAIN_START_YEAR, last_train_year, label_col)
    X_test_final, y_test_final = _load_dataset(conn, CV_TEST_YEARS[-1], CV_TEST_YEARS[-1], label_col)

    final = _train_single_fold(X_all, y_all, X_test_final, y_test_final)
    if "error" in final:
        logger.error("Final model failed: %s", final["error"])
        return results

    os.makedirs(MODELS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_path = os.path.join(MODELS_DIR, f"fx_{direction}_{horizon}_{timestamp}.joblib")
    model_id = f"fx_{direction}_{horizon}_{uuid.uuid4().hex[:8]}"

    joblib.dump({
        "model": final["model"],
        "platt": final["platt"],
        "medians": final["medians"],
        "feature_cols": FEATURE_COLS,
        "direction": direction,
        "horizon": horizon,
        "target_pips": target_pips,
    }, model_path)

    avg_auc = float(np.mean([r["auc_roc"] for r in valid]))
    avg_lift = float(np.mean([r["lift"] for r in valid]))
    avg_prec = float(np.mean([r["precision_top10"] for r in valid]))
    avg_base = float(np.mean([r["base_rate"] for r in valid]))

    conn.execute(
        "UPDATE fx_ml_model_runs SET is_active = false WHERE direction = ? AND horizon = ?",
        [direction, horizon]
    )

    conn.execute("""
        INSERT INTO fx_ml_model_runs (
            model_id, direction, horizon, target_pips,
            train_start, train_end, test_start, test_end,
            n_train_samples, n_test_samples, n_positive_train, n_positive_test,
            precision_at_threshold, auc_roc, base_rate, lift_over_base,
            feature_importance_json, model_path, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true)
    """, [
        model_id, direction, horizon, target_pips,
        f"{TRAIN_START_YEAR}-01-01", f"{last_train_year}-12-31",
        f"{CV_TEST_YEARS[-1]}-01-01", f"{CV_TEST_YEARS[-1]}-12-31",
        final["n_train"], final["n_test"], final["n_pos_train"], final["n_pos_test"],
        avg_prec, avg_auc, avg_base, avg_lift,
        json.dumps(final["feature_importance"]), model_path,
    ])

    logger.info("Saved: %s (avg AUC=%.3f, avg Lift=%.2fx)", model_id, avg_auc, avg_lift)
    return results


def train_all_models(conn: duckdb.DuckDBPyConnection) -> dict:
    all_results = {}
    for cfg in MODEL_CONFIGS:
        key = f"{cfg['direction']}_{cfg['horizon']}"
        all_results[key] = train_model(conn, **cfg)
    return all_results
