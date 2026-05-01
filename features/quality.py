from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.features.quality")


def compute_quality(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []

    # Net income positive/negative
    ni_rows = conn.execute(
        """
        SELECT value, as_of_date FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%NetIncomeLoss%'
        ORDER BY as_of_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()

    if ni_rows:
        latest_ni = ni_rows[0][0]
        ni_positive = latest_ni is not None and latest_ni > 0
        score = 70.0 if ni_positive else 30.0
        features.append({
            "feature_group": "quality",
            "feature_name": "net_income_positive",
            "feature_value": float(latest_ni) if latest_ni is not None else None,
            "feature_score": score,
            "confidence": "high",
            "source": "sec_edgar",
        })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "net_income_positive",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    # Revenue growth YoY
    rev_rows = conn.execute(
        """
        SELECT value, as_of_date FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%Revenues%'
        ORDER BY as_of_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()

    if len(rev_rows) >= 2 and rev_rows[1][0] and rev_rows[1][0] != 0:
        growth = (rev_rows[0][0] - rev_rows[1][0]) / abs(rev_rows[1][0]) * 100
        # Score: > 20% growth → 90, 10-20% → 75, 0-10% → 60, negative → 30
        if growth > 20:
            score = 90.0
        elif growth > 10:
            score = 75.0
        elif growth >= 0:
            score = 60.0
        else:
            score = 30.0
        features.append({
            "feature_group": "quality",
            "feature_name": "revenue_growth_yoy",
            "feature_value": round(growth, 2),
            "feature_score": score,
            "confidence": "medium",
            "source": "sec_edgar",
        })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "revenue_growth_yoy",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    # Net margin
    ni_val = ni_rows[0][0] if ni_rows else None
    rev_val = rev_rows[0][0] if rev_rows else None
    if ni_val is not None and rev_val and rev_val != 0:
        margin = ni_val / rev_val * 100
        if margin > 20:
            score = 90.0
        elif margin > 10:
            score = 75.0
        elif margin > 0:
            score = 55.0
        else:
            score = 20.0
        features.append({
            "feature_group": "quality",
            "feature_name": "net_margin",
            "feature_value": round(margin, 2),
            "feature_score": score,
            "confidence": "medium",
            "source": "sec_edgar",
        })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "net_margin",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    # Dilution proxy (shares change)
    shares_rows = conn.execute(
        """
        SELECT value FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%CommonStockSharesOutstanding%'
        ORDER BY as_of_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()

    if len(shares_rows) >= 2 and shares_rows[1][0] and shares_rows[1][0] > 0:
        dilution_pct = (shares_rows[0][0] - shares_rows[1][0]) / shares_rows[1][0] * 100
        # Low dilution is good: < 2% → 85, 2-5% → 65, > 5% → 30
        if dilution_pct < 2:
            score = 85.0
        elif dilution_pct < 5:
            score = 65.0
        else:
            score = 30.0
        features.append({
            "feature_group": "quality",
            "feature_name": "dilution_rate",
            "feature_value": round(dilution_pct, 2),
            "feature_score": score,
            "confidence": "medium",
            "source": "sec_edgar",
        })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "dilution_rate",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    return features
