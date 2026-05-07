from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger("mhde.models.dataset")

_MIN_ROWS = 30


def build_dataset(conn: duckdb.DuckDBPyConnection):
    """
    Join features + candidate_outcomes labels to build XGBoost training data.
    Returns (X, y, feature_names) or (None, None, None) if insufficient data.
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        logger.warning("pandas/numpy not installed — cannot build dataset")
        return None, None, None

    rows = conn.execute(
        """
        SELECT
            s.ticker, s.cheap_score, s.quality_score, s.catalyst_score,
            s.momentum_score, s.sentiment_score, s.risk_penalty, s.total_score,
            co.forward_return_20d as label
        FROM scores s
        JOIN candidate_outcomes co ON (s.ticker = co.ticker AND s.run_id = co.run_id)
        WHERE co.forward_return_20d IS NOT NULL
        ORDER BY s.created_at DESC
        """
    ).fetchall()

    if len(rows) < _MIN_ROWS:
        logger.warning(
            "Insufficient training data: %d rows (need %d). "
            "Run the daily pipeline for several weeks to accumulate data.",
            len(rows), _MIN_ROWS,
        )
        return None, None, None

    cols = [
        "ticker", "cheap_score", "quality_score", "catalyst_score",
        "momentum_score", "sentiment_score", "risk_penalty", "total_score", "label",
    ]
    df = pd.DataFrame(rows, columns=cols)
    feature_names = ["cheap_score", "quality_score", "catalyst_score",
                     "momentum_score", "sentiment_score", "risk_penalty"]
    X = df[feature_names].values.astype(float)
    y = (df["label"] > 0).astype(int).values

    logger.info("Dataset built: %d rows, %d features", len(X), len(feature_names))
    return X, y, feature_names
