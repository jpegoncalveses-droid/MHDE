from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.features.macro")

_MACRO_SERIES = ["FEDFUNDS", "DGS10", "CPIAUCSL", "UNRATE"]


def compute_macro(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    as_of: date,
) -> list[dict]:
    """Macro features are not stock-specific. ticker=None."""
    features = []

    for series_id in _MACRO_SERIES:
        rows = conn.execute(
            """
            SELECT value, as_of_date FROM macro_series
            WHERE series_id = ? ORDER BY as_of_date DESC LIMIT 2
            """,
            [series_id],
        ).fetchall()

        if not rows:
            features.append({
                "feature_group": "macro",
                "feature_name": series_id.lower(),
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "fred",
                "ticker": None,
            })
            continue

        latest_val = rows[0][0]
        prior_val = rows[1][0] if len(rows) > 1 else None
        trend = (
            "rising" if prior_val and latest_val > prior_val
            else "falling" if prior_val and latest_val < prior_val
            else "flat"
        )

        # Simple contextual score (not used in per-stock scoring formula)
        score = 50.0  # neutral — macro is context not signal

        features.append({
            "feature_group": "macro",
            "feature_name": series_id.lower(),
            "feature_value": float(latest_val) if latest_val is not None else None,
            "feature_score": score,
            "confidence": "high",
            "source": "fred",
            "ticker": None,
            "metadata": {"trend": trend},
        })

    return features
