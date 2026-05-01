from __future__ import annotations

import json
import logging
import uuid

import duckdb

from models.dataset_builder import build_dataset

logger = logging.getLogger("mhde.models.xgboost")

_WARNING = (
    "Experimental only. Not used for alerts or rankings. "
    "Requires weeks of accumulated daily runs for meaningful results."
)


def train_smoke(conn: duckdb.DuckDBPyConnection, cfg: dict) -> dict | None:
    logger.warning(_WARNING)
    print(f"\n{_WARNING}\n")

    try:
        import xgboost as xgb
    except ImportError:
        logger.warning("xgboost not installed — skipping. pip install xgboost to enable.")
        print("xgboost not installed. pip install xgboost to enable.")
        return None

    X, y, feature_names = build_dataset(conn)
    if X is None:
        print("Insufficient training data — skipping XGBoost smoke.")
        return None

    model_run_id = uuid.uuid4().hex[:16]

    # Simple train/test split (last 20% = test)
    split = max(1, int(len(X) * 0.8))
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    model = xgb.XGBClassifier(
        n_estimators=50,
        max_depth=3,
        learning_rate=0.1,
        use_label_encoder=False,
        eval_metric="logloss",
        verbosity=0,
    )
    model.fit(X_train, y_train)

    from models.evaluation import evaluate
    metrics = evaluate(model, X_test, y_test)

    importance = dict(zip(feature_names, model.feature_importances_.tolist()))
    importance_sorted = dict(sorted(importance.items(), key=lambda x: -x[1]))

    print("XGBoost smoke results:")
    for k, v in metrics.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print("\nFeature importance:")
    for feat, imp in importance_sorted.items():
        print(f"  {feat}: {imp:.4f}")
    print(f"\n  {_WARNING}")

    _persist(conn, model_run_id, metrics, importance_sorted)
    return {"model_run_id": model_run_id, "metrics": metrics, "warning": _WARNING}


def _persist(conn: duckdb.DuckDBPyConnection, model_run_id: str, metrics: dict, importance: dict) -> None:
    try:
        conn.execute(
            """
            INSERT INTO model_runs
                (model_run_id, model_type, target, metrics_json, feature_importance_json,
                 status, warning)
            VALUES (?, 'xgboost', 'forward_return_20d_positive', ?, ?, 'complete', ?)
            """,
            [model_run_id, json.dumps(metrics), json.dumps(importance), _WARNING],
        )
    except Exception as exc:
        logger.debug("Could not persist model run: %s", exc)
