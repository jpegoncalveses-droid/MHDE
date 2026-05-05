"""Weekly retrain for FX models."""
from __future__ import annotations

import logging

import duckdb

from fx.ml.labels import compute_labels
from fx.ml.features import compute_features
from fx.ml.train import train_all_models
from fx.ml.evaluate import print_training_results

logger = logging.getLogger("mhde.fx.retrain")


def retrain_all(conn: duckdb.DuckDBPyConnection):
    logger.info("Starting FX weekly retrain")
    logger.info("Recomputing labels...")
    compute_labels(conn)
    logger.info("Recomputing features...")
    compute_features(conn)
    logger.info("Training all models...")
    results = train_all_models(conn)
    print_training_results(results)
    logger.info("FX retrain complete")
