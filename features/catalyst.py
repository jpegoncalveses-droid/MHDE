from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

logger = logging.getLogger("mhde.features.catalyst")


def compute_catalyst(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []
    points = 0.0
    signals = []

    # Recent 8-K (material event)
    row_8k = conn.execute(
        """
        SELECT COUNT(*) FROM filings
        WHERE ticker = ? AND form_type = '8-K'
          AND filing_date >= CAST(? AS DATE) - INTERVAL '30 days'
        """,
        [ticker, as_of],
    ).fetchone()
    if row_8k and row_8k[0] > 0:
        points += 30.0
        signals.append(f"8-K filed in last 30d (count={row_8k[0]})")

    # Recent 10-Q or 10-K
    row_qk = conn.execute(
        """
        SELECT COUNT(*) FROM filings
        WHERE ticker = ?
          AND form_type IN ('10-Q', '10-K')
          AND filing_date >= CAST(? AS DATE) - INTERVAL '45 days'
        """,
        [ticker, as_of],
    ).fetchone()
    if row_qk and row_qk[0] > 0:
        points += 20.0
        signals.append("10-Q/10-K filed recently")

    # Upcoming earnings
    row_earnings = conn.execute(
        """
        SELECT COUNT(*) FROM events
        WHERE ticker = ? AND event_type = 'earnings'
          AND is_upcoming = true
          AND event_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) + INTERVAL '14 days'
        """,
        [ticker, as_of, as_of],
    ).fetchone()
    if row_earnings and row_earnings[0] > 0:
        points += 25.0
        signals.append("Earnings in next 14d")

    # Short interest change (contrarian: rising short interest + low price = potential catalyst)
    si_rows = conn.execute(
        """
        SELECT short_interest, settlement_date FROM short_interest
        WHERE ticker = ? ORDER BY settlement_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()
    if len(si_rows) >= 2 and si_rows[1][0] and si_rows[1][0] > 0:
        si_change_pct = (si_rows[0][0] - si_rows[1][0]) / si_rows[1][0] * 100
        if abs(si_change_pct) > 10:
            points += 15.0
            signals.append(f"Short interest changed {si_change_pct:+.1f}%")

    score = min(100.0, points)
    features.append({
        "feature_group": "catalyst",
        "feature_name": "catalyst_score",
        "feature_value": float(len(signals)),
        "feature_score": score,
        "confidence": "medium" if signals else "low",
        "source": "sec_edgar+events+finra",
        "metadata": {"signals": signals},
    })
    return features
