"""Enrich companies table with Polygon ticker details (market_cap, exchange, SIC).

Polygon is optional. If no API key is provided, returns None and logs a warning.
No data fetched in tests — all external calls are mockable via _fetch_polygon_details.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TickerDetail:
    ticker: str
    market_cap: Optional[float]
    exchange: Optional[str]
    sic_code: Optional[str]
    sic_description: Optional[str]


def _fetch_polygon_details(ticker: str, api_key: str) -> dict:
    import json
    import urllib.request

    url = f"https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={api_key}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def enrich_ticker_details(ticker: str, api_key: Optional[str]) -> Optional[TickerDetail]:
    """Fetch ticker details from Polygon. Returns None if no key or on error."""
    if not api_key:
        return None
    try:
        data = _fetch_polygon_details(ticker, api_key)
        results = data.get("results", {})
        return TickerDetail(
            ticker=ticker,
            market_cap=results.get("market_cap"),
            exchange=results.get("primary_exchange"),
            sic_code=str(results["sic_code"]) if results.get("sic_code") else None,
            sic_description=results.get("sic_description"),
        )
    except Exception as exc:
        logger.warning("ticker_details: %s — %s", ticker, exc)
        return None


def run_enrichment(db_path: str, api_key: Optional[str], delay: float = 0.25) -> dict:
    """Enrich all active companies with Polygon ticker details.

    Returns a summary dict with keys: updated, errors, skipped, reason.
    If no API key, returns immediately with reason='no_api_key'.
    """
    import duckdb

    if not api_key:
        logger.warning("POLYGON_API_KEY not set — skipping ticker details enrichment")
        return {"updated": 0, "errors": 0, "skipped": 0, "reason": "no_api_key"}

    conn = duckdb.connect(db_path)
    try:
        tickers = [
            r[0]
            for r in conn.execute(
                "SELECT ticker FROM companies WHERE is_active = true ORDER BY ticker"
            ).fetchall()
        ]
        updated = errors = 0
        for ticker in tickers:
            detail = enrich_ticker_details(ticker, api_key)
            if detail:
                conn.execute(
                    "UPDATE companies SET market_cap = ? WHERE ticker = ?",
                    [detail.market_cap, ticker],
                )
                updated += 1
            else:
                errors += 1
            if delay > 0:
                time.sleep(delay)
        return {"updated": updated, "errors": errors, "skipped": 0, "reason": "ok"}
    finally:
        conn.close()
