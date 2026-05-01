from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.features.sentiment")


def compute_sentiment(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []

    # Short interest as contrarian sentiment proxy
    si_rows = conn.execute(
        """
        SELECT short_interest, settlement_date FROM short_interest
        WHERE ticker = ? ORDER BY settlement_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()

    if si_rows:
        latest_si = si_rows[0][0]
        prior_si = si_rows[1][0] if len(si_rows) > 1 else None

        # High short interest → bearish sentiment → contrarian upside if thesis holds
        si_change = None
        if prior_si and prior_si > 0:
            si_change = (latest_si - prior_si) / prior_si * 100

        # Score: moderate short interest is interesting; very high is risky
        if latest_si is None:
            score = None
            confidence = "low"
        elif latest_si > 20_000_000:
            score = 40.0  # very high short interest — risky
            confidence = "medium"
        elif latest_si > 5_000_000:
            score = 60.0  # moderate — interesting
            confidence = "medium"
        else:
            score = 70.0  # low short interest — less controversial
            confidence = "medium"

        features.append({
            "feature_group": "sentiment",
            "feature_name": "short_interest_proxy",
            "feature_value": float(latest_si) if latest_si else None,
            "feature_score": score,
            "confidence": confidence,
            "source": "finra",
            "metadata": {"si_change_pct": round(si_change, 2) if si_change else None},
        })
    else:
        logger.debug("No short interest data for %s", ticker)
        features.append({
            "feature_group": "sentiment",
            "feature_name": "short_interest_proxy",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "finra",
            "metadata": {"warning": "No FINRA short interest data available"},
        })

    # Stocktwits — stub
    features.append({
        "feature_group": "sentiment",
        "feature_name": "social_attention",
        "feature_value": None,
        "feature_score": None,
        "confidence": "low",
        "source": "stocktwits",
        "metadata": {"warning": "[STUB] Stocktwits not yet implemented"},
    })

    return features
