from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

logger = logging.getLogger("mhde.features.catalyst")

_8K_MATERIAL_KEYWORDS = [
    "earn", "acqui", "merger", "divestiture", "agreement",
    "guidance", "revenue", "settlement", "dividend", "buyback", "restate",
]


def _8k_is_material(description: str | None) -> bool:
    if not description:
        return False
    desc_lower = description.lower()
    return any(kw in desc_lower for kw in _8K_MATERIAL_KEYWORDS)


def compute_catalyst(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []
    points = 0.0
    signals = []
    has_routine_filing = False

    # Recent 8-K (material or routine)
    row_8k = conn.execute(
        """
        SELECT COUNT(*), MAX(description) FROM filings
        WHERE ticker = ? AND form_type = '8-K'
          AND filing_date >= CAST(? AS DATE) - INTERVAL '30 days'
        """,
        [ticker, as_of],
    ).fetchone()
    if row_8k and row_8k[0] > 0:
        description_8k = row_8k[1]
        if _8k_is_material(description_8k):
            points += 30.0
            signals.append(f"8-K (material) filed in last 30d (count={row_8k[0]})")
        else:
            points += 15.0
            signals.append(f"8-K (routine) filed in last 30d (count={row_8k[0]})")

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
        points += 5.0
        signals.append("10-Q/10-K filed recently (routine)")
        has_routine_filing = True

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

    # 6-K disclosures (foreign issuer filing — recorded but not auto-scored as catalyst)
    disclosure_evidence: list[str] = []
    row_6k = conn.execute(
        """
        SELECT COUNT(*) FROM filings
        WHERE ticker = ? AND form_type = '6-K'
          AND filing_date >= CAST(? AS DATE) - INTERVAL '30 days'
        """,
        [ticker, as_of],
    ).fetchone()
    if row_6k and row_6k[0] > 0:
        disclosure_evidence.append(
            f"6-K filed in last 30d (count={row_6k[0]}) — foreign issuer disclosure, not auto-scored"
        )

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
    meta: dict = {"signals": signals}
    if has_routine_filing:
        meta["routine_filing"] = True
    if disclosure_evidence:
        meta["disclosure_evidence"] = disclosure_evidence
    features.append({
        "feature_group": "catalyst",
        "feature_name": "catalyst_score",
        "feature_value": float(len(signals)),
        "feature_score": score,
        "confidence": "medium" if signals else "low",
        "source": "sec_edgar+events+finra",
        "metadata": meta,
    })
    return features
