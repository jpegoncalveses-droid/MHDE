"""Weekly retrain orchestration for crypto ML models.

Retrains all configured horizons on latest data, promotes if performance is acceptable.
"""
from __future__ import annotations

import logging

import duckdb

from crypto.ml.labels import compute_labels
from crypto.ml.features import compute_features
from crypto.ml.train import train_walk_forward
from crypto.ml.evaluate import print_walk_forward_results

logger = logging.getLogger("mhde.crypto.retrain")

CONFIGS = [
    {"label_col": "label_10d_10pct", "horizon": "10d", "threshold": 0.10},
    {"label_col": "label_5d_10pct", "horizon": "5d", "threshold": 0.10},
]


def retrain_all(conn: duckdb.DuckDBPyConnection):
    logger.info("Starting crypto weekly retrain")

    logger.info("Recomputing labels...")
    compute_labels(conn)

    logger.info("Recomputing features...")
    compute_features(conn)

    for cfg in CONFIGS:
        logger.info("Training %s (%s, threshold=%.2f)", cfg["horizon"], cfg["label_col"], cfg["threshold"])
        results = train_walk_forward(
            conn,
            label_col=cfg["label_col"],
            horizon=cfg["horizon"],
            threshold=cfg["threshold"],
        )
        print_walk_forward_results(results, cfg["label_col"], cfg["horizon"])

    logger.info("Crypto retrain complete")
