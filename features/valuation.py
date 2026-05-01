from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.features.valuation")


def _clamp(v: float | None) -> float | None:
    if v is None:
        return None
    return max(0.0, min(100.0, v))


def compute_valuation(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []

    # Latest price
    price_row = conn.execute(
        "SELECT close FROM prices_daily WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
        [ticker],
    ).fetchone()
    price = price_row[0] if price_row else None

    # 52-week high
    high_row = conn.execute(
        """
        SELECT MAX(high) FROM prices_daily
        WHERE ticker = ? AND trade_date >= CAST(? AS DATE) - INTERVAL '52 weeks'
        """,
        [ticker, as_of],
    ).fetchone()
    week52_high = high_row[0] if high_row else None

    # Price vs 52-week high
    if price and week52_high and week52_high > 0:
        pct_from_high = (price / week52_high) * 100
        score = _clamp(100 - pct_from_high)  # lower = cheaper relative to high
        features.append({
            "feature_group": "valuation",
            "feature_name": "price_vs_52w_high",
            "feature_value": pct_from_high,
            "feature_score": score,
            "confidence": "high",
            "source": "polygon",
        })
    else:
        features.append({
            "feature_group": "valuation",
            "feature_name": "price_vs_52w_high",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "polygon",
        })

    # Revenue-based P/S proxy (price / revenue_per_share)
    rev_row = conn.execute(
        """
        SELECT value FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%Revenues%'
        ORDER BY as_of_date DESC LIMIT 1
        """,
        [ticker],
    ).fetchone()
    shares_row = conn.execute(
        """
        SELECT value FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%CommonStockSharesOutstanding%'
        ORDER BY as_of_date DESC LIMIT 1
        """,
        [ticker],
    ).fetchone()

    if price and rev_row and shares_row and shares_row[0] and shares_row[0] > 0:
        rev_per_share = rev_row[0] / shares_row[0]
        ps = price / rev_per_share if rev_per_share > 0 else None
        if ps is not None:
            # Score: P/S < 1 → 90, P/S 1-3 → 70, P/S 3-10 → 50, P/S > 10 → 20
            if ps < 1:
                score = 90.0
            elif ps < 3:
                score = 70.0
            elif ps < 10:
                score = 50.0
            else:
                score = 20.0
            features.append({
                "feature_group": "valuation",
                "feature_name": "ps_proxy",
                "feature_value": round(ps, 2),
                "feature_score": score,
                "confidence": "medium",
                "source": "sec_edgar+polygon",
            })
    else:
        features.append({
            "feature_group": "valuation",
            "feature_name": "ps_proxy",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar+polygon",
        })

    return features
