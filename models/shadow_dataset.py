"""Shadow ML dataset builder.

Extends the base dataset with missed_opportunity_label and false_positive_label.
"""
from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger("mhde.models.shadow_dataset")

_MIN_ROWS = 30


def build_shadow_dataset(conn: duckdb.DuckDBPyConnection):
    """
    Build shadow training dataset joining scores + outcomes + missed_opportunity labels.
    Returns pandas DataFrame or None if insufficient data.
    """
    try:
        import pandas as pd
    except ImportError:
        logger.warning("pandas not installed — cannot build shadow dataset")
        return None

    rows = conn.execute(
        """
        SELECT
            s.ticker,
            s.cheap_score, s.quality_score, s.catalyst_score,
            s.momentum_score, s.sentiment_score, s.risk_penalty, s.total_score,
            co.forward_return_5d,
            co.forward_return_20d,
            co.forward_return_60d,
            co.hit_10pct_before_down_10pct,
            co.hit_20pct_before_down_10pct,
            CASE WHEN co.forward_return_20d >= 0.20 THEN 1 ELSE 0 END AS missed_opportunity_label,
            CASE WHEN r.false_positive_reason IS NOT NULL THEN 1 ELSE 0 END AS false_positive_label
        FROM scores s
        JOIN candidate_outcomes co ON (s.ticker = co.ticker AND s.run_id = co.run_id)
        LEFT JOIN candidate_reviews r ON (s.ticker = r.ticker AND s.run_id = r.run_id)
        WHERE co.forward_return_20d IS NOT NULL
        ORDER BY s.created_at DESC
        """
    ).fetchall()

    if len(rows) < _MIN_ROWS:
        logger.warning(
            "Insufficient shadow training data: %d rows (need %d).",
            len(rows), _MIN_ROWS,
        )
        return None

    cols = [
        "ticker", "cheap_score", "quality_score", "catalyst_score",
        "momentum_score", "sentiment_score", "risk_penalty", "total_score",
        "forward_return_5d", "forward_return_20d", "forward_return_60d",
        "hit_10pct_before_down_10pct", "hit_20pct_before_down_10pct",
        "missed_opportunity_label", "false_positive_label",
    ]
    return pd.DataFrame(rows, columns=cols)
