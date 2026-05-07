"""Shadow ML ranker.

Trains in shadow mode only. Output (shadow_score, shadow_rank) is stored in model_runs
but NEVER used for production alerts or tier assignment.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

import duckdb

from models.shadow_dataset import build_shadow_dataset

logger = logging.getLogger("mhde.models.shadow_ranker")

_WARNING = (
    "Shadow ranker: experimental only. "
    "shadow_score is NOT used for production alerts or rankings."
)


class ShadowRanker:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def train(self) -> dict | None:
        """
        Train shadow XGBoost model on accumulated outcome data.
        Returns result dict or None if insufficient data / xgboost unavailable.
        Does NOT modify any row in the scores table.
        """
        logger.warning(_WARNING)

        try:
            import xgboost as xgb
        except ImportError:
            logger.warning("xgboost not installed — shadow ranker unavailable.")
            return None

        df = build_shadow_dataset(self.conn)
        if df is None:
            return None

        feature_cols = [
            "cheap_score", "quality_score", "catalyst_score",
            "momentum_score", "sentiment_score", "risk_penalty", "total_score",
        ]
        target_col = "missed_opportunity_label"

        X = df[feature_cols].fillna(0).values
        y = df[target_col].values

        split = max(1, int(len(X) * 0.8))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        model = xgb.XGBClassifier(
            n_estimators=50,
            max_depth=3,
            learning_rate=0.1,
            eval_metric="logloss",
            verbosity=0,
        )
        model.fit(X_train, y_train)

        importance = dict(zip(feature_cols, model.feature_importances_.tolist()))
        model_run_id = uuid.uuid4().hex[:16]

        metrics: dict = {"train_rows": int(split), "test_rows": int(len(X) - split)}
        if len(X_test) > 0:
            try:
                from models.evaluation import evaluate
                metrics.update(evaluate(model, X_test, y_test))
            except Exception as exc:
                logger.warning("Shadow evaluation failed: %s", exc)

        self._store_model_run(model_run_id, metrics, importance)
        print(f"Shadow ranker trained: model_run_id={model_run_id}")
        return {"model_run_id": model_run_id, "metrics": metrics}

    def _store_model_run(self, model_run_id: str, metrics: dict, importance: dict) -> None:
        now = datetime.utcnow()
        self.conn.execute(
            """INSERT INTO model_runs
               (model_run_id, model_type, target, metrics_json, feature_importance_json,
                status, warning, created_at)
               VALUES (?, 'xgboost_shadow', 'missed_opportunity_label', ?, ?, 'complete', ?, ?)""",
            [model_run_id, json.dumps(metrics), json.dumps(importance), _WARNING, now],
        )
