from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.features.risk")


def compute_risk(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
    feature_rows: list[dict],
) -> list[dict]:
    """Compute risk penalty 0-100. Higher = riskier."""
    penalty = 0.0
    flags = []

    # Missing data rate
    scored = [f for f in feature_rows if f.get("feature_score") is not None]
    total = len(feature_rows)
    null_rate = 1.0 - (len(scored) / total) if total > 0 else 1.0
    if null_rate > 0.7:
        penalty += 35.0
        flags.append(f"missing_data_rate={null_rate:.0%}")
    elif null_rate > 0.4:
        penalty += 15.0
        flags.append(f"partial_data={null_rate:.0%}")

    # Negative income
    ni_feature = next(
        (f for f in feature_rows if f.get("feature_name") == "net_income_positive"), None
    )
    if ni_feature and ni_feature.get("feature_value") is not None:
        if ni_feature["feature_value"] <= 0:
            penalty += 20.0
            flags.append("negative_net_income")

    # Low price
    price_row = conn.execute(
        "SELECT close FROM prices_daily WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
        [ticker],
    ).fetchone()
    if price_row and price_row[0] is not None and price_row[0] < 2.0:
        penalty += 20.0
        flags.append(f"low_price={price_row[0]:.2f}")

    # Insufficient price history
    count_row = conn.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker = ?", [ticker]
    ).fetchone()
    if not count_row or count_row[0] < 20:
        penalty += 10.0
        flags.append("insufficient_price_history")

    # Stale fundamentals (no filings in 180 days)
    filing_row = conn.execute(
        """
        SELECT MAX(filing_date) FROM filings WHERE ticker = ?
        """,
        [ticker],
    ).fetchone()
    if not filing_row or not filing_row[0]:
        penalty += 15.0
        flags.append("no_filings_found")

    penalty = min(100.0, penalty)

    return [{
        "feature_group": "risk",
        "feature_name": "risk_penalty",
        "feature_value": penalty,
        "feature_score": penalty,
        "confidence": "medium",
        "source": "computed",
        "metadata": {"flags": flags},
    }]
