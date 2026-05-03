"""Ingest earnings estimates and EPS surprises from Alpha Vantage.

Alpha Vantage is optional. If no API key, returns 0 and logs a warning.
All external calls go through _fetch_alpha_vantage_earnings() for easy mocking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EarningsSurprise:
    ticker: str
    fiscal_date: str
    reported_eps: Optional[float]
    estimated_eps: Optional[float]
    surprise_pct: Optional[float]


def compute_surprise_pct(reported: float, estimated: float) -> Optional[float]:
    """Compute EPS surprise as a percentage. Returns None if estimated is zero."""
    if not estimated:
        return None
    return round((reported - estimated) / abs(estimated) * 100, 2)


def _fetch_alpha_vantage_earnings(ticker: str, api_key: str) -> dict:
    import json
    import urllib.request

    url = (
        f"https://www.alphavantage.co/query"
        f"?function=EARNINGS&symbol={ticker}&apikey={api_key}"
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def parse_alpha_vantage_earnings(ticker: str, raw: dict) -> list[EarningsSurprise]:
    """Parse Alpha Vantage EARNINGS response into EarningsSurprise records."""
    results: list[EarningsSurprise] = []
    for q in raw.get("quarterlyEarnings", []):
        try:
            def _f(v: object) -> Optional[float]:
                if v is None or str(v) in ("None", "", "N/A"):
                    return None
                return float(v)

            reported = _f(q.get("reportedEPS"))
            estimated = _f(q.get("estimatedEPS"))
            surprise_pct_raw = _f(q.get("surprisePercentage"))

            if surprise_pct_raw is None and reported is not None and estimated is not None:
                surprise_pct_raw = compute_surprise_pct(reported, estimated)

            results.append(
                EarningsSurprise(
                    ticker=ticker,
                    fiscal_date=q["fiscalDateEnding"],
                    reported_eps=reported,
                    estimated_eps=estimated,
                    surprise_pct=surprise_pct_raw,
                )
            )
        except Exception as exc:
            logger.warning("parse_earnings: %s %s — %s", ticker, q.get("fiscalDateEnding"), exc)
    return results


def ingest_earnings_for_ticker(ticker: str, api_key: Optional[str], db_path: str) -> int:
    """Fetch and store earnings surprises for one ticker. Returns rows upserted."""
    if not api_key:
        logger.warning("ALPHA_VANTAGE_API_KEY not set — earnings estimates unavailable for %s", ticker)
        return 0
    import duckdb

    try:
        raw = _fetch_alpha_vantage_earnings(ticker, api_key)
        surprises = parse_alpha_vantage_earnings(ticker, raw)
        if not surprises:
            return 0
        conn = duckdb.connect(db_path)
        try:
            count = 0
            for s in surprises:
                conn.execute(
                    """
                    INSERT INTO earnings_estimates
                        (ticker, fiscal_date, reported_eps, estimated_eps, surprise_pct, source)
                    VALUES (?, ?, ?, ?, ?, 'alpha_vantage')
                    ON CONFLICT (ticker, fiscal_date, source) DO UPDATE SET
                        reported_eps = excluded.reported_eps,
                        estimated_eps = excluded.estimated_eps,
                        surprise_pct = excluded.surprise_pct
                    """,
                    [s.ticker, s.fiscal_date, s.reported_eps, s.estimated_eps, s.surprise_pct],
                )
                count += 1
            return count
        finally:
            conn.close()
    except Exception as exc:
        logger.error("ingest_earnings: %s — %s", ticker, exc)
        return 0
