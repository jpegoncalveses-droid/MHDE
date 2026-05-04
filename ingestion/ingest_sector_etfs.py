"""Daily sector ETF return ingestion for sympathy/theme attribution.

Fetches 1-day returns for 11 SPDR sector ETFs from Polygon (optional).
If no API key, returns empty dict and logs a warning — no crash.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

SECTOR_ETFS: tuple[str, ...] = (
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC", "XLY"
)

ETF_TO_SECTOR: dict[str, str] = {
    "XLK": "Information Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Health Care",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
}


def _fetch_etf_return(ticker: str, date: str, api_key: str) -> Optional[float]:
    """Fetch 1-day return for a single ETF from Polygon. Returns None on error."""
    import json
    import urllib.request

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
        f"/{date}/{date}?adjusted=true&apiKey={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        bars = data.get("results", [])
        if not bars:
            return None
        bar = bars[0]
        if bar.get("o") and bar["o"] != 0:
            return round((bar["c"] - bar["o"]) / bar["o"], 6)
        return None
    except Exception as exc:
        logger.warning("sector_etf: %s %s — %s", ticker, date, exc)
        return None


def get_sector_returns(date: str, api_key: Optional[str]) -> dict[str, float]:
    """Return {etf_ticker: 1d_return} for all sector ETFs. Empty dict if no key."""
    if not api_key:
        logger.warning("POLYGON_API_KEY not set — sector ETF returns unavailable for %s", date)
        return {}
    results: dict[str, float] = {}
    for etf in SECTOR_ETFS:
        ret = _fetch_etf_return(etf, date, api_key)
        if ret is not None:
            results[etf] = ret
        time.sleep(0.12)
    return results


def ingest_sector_etfs_to_db(db_path: str, date: str, api_key: Optional[str]) -> int:
    """Fetch sector ETF returns and upsert into prices_daily. Returns row count written."""
    import uuid as _uuid
    import duckdb

    returns = get_sector_returns(date, api_key)
    if not returns:
        return 0
    conn = duckdb.connect(db_path)
    try:
        count = 0
        for etf, ret in returns.items():
            conn.execute(
                """
                INSERT INTO prices_daily (id, ticker, trade_date, close, adjusted_close, source)
                VALUES (?, ?, ?, ?, ?, 'polygon_sector_etf')
                ON CONFLICT (ticker, trade_date) DO UPDATE
                    SET adjusted_close = excluded.adjusted_close,
                        source = excluded.source
                """,
                [_uuid.uuid4().hex[:16], etf, date, ret, ret],
            )
            count += 1
        return count
    finally:
        conn.close()
