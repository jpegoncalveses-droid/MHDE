"""Enrich companies table with market_cap derived from SEC EDGAR XBRL.

Source: data.sec.gov/api/xbrl/companyfacts/{CIK}.json
Formula: market_cap = CommonStockSharesOutstanding × latest_close_price

No API key required. SEC EDGAR rate limit is ≤10 req/sec; default delay is
0.12s (~8 req/sec). Only processes tickers with a non-null CIK and
active_sec_reporter != false.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_SEC_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
_SEC_USER_AGENT = "MHDE/1.0 jpegoncalves.es@gmail.com"
_PREFERRED_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F"})


@dataclass
class TickerDetail:
    ticker: str
    market_cap: Optional[float]
    shares_outstanding: Optional[int]
    exchange: Optional[str] = None
    sic_code: Optional[str] = None
    sic_description: Optional[str] = None


def _cik_url(cik: str) -> str:
    """Build the SEC EDGAR company facts URL from a CIK string."""
    numeric = str(cik).lstrip("CIK").lstrip("0") or "0"
    padded = str(int(numeric)).zfill(10)
    return f"{_SEC_BASE}/CIK{padded}.json"


def _fetch_sec_companyfacts(cik: str) -> dict:
    import json
    import urllib.request

    req = urllib.request.Request(
        _cik_url(cik), headers={"User-Agent": _SEC_USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _extract_shares_outstanding(facts: dict) -> Optional[int]:
    """Return most recent shares outstanding from XBRL company facts.

    Tries us-gaap.CommonStockSharesOutstanding first, then
    dei.EntityCommonStockSharesOutstanding as a fallback.
    Prefers 10-K/10-Q/20-F/40-F filings; falls back to any filing.
    """
    def _from_namespace(ns_key: str, concept_key: str) -> Optional[int]:
        units = (
            facts.get("facts", {})
            .get(ns_key, {})
            .get(concept_key, {})
            .get("units", {})
            .get("shares", [])
        )
        if not units:
            return None
        preferred = [u for u in units if u.get("form") in _PREFERRED_FORMS and u.get("val") is not None]
        pool = preferred if preferred else [u for u in units if u.get("val") is not None]
        if not pool:
            return None
        return int(max(pool, key=lambda u: u.get("end", ""))["val"])

    return (
        _from_namespace("us-gaap", "CommonStockSharesOutstanding")
        or _from_namespace("dei", "EntityCommonStockSharesOutstanding")
    )


def enrich_ticker_details(
    ticker: str, cik: str, latest_price: Optional[float]
) -> Optional[TickerDetail]:
    """Fetch shares from SEC EDGAR XBRL and compute market_cap = shares × price.

    Returns None on any network or parsing error.
    Returns a TickerDetail with market_cap=None when shares exist but price is absent.
    """
    try:
        facts = _fetch_sec_companyfacts(cik)
        shares = _extract_shares_outstanding(facts)
        market_cap: Optional[float] = None
        if shares is not None and latest_price:
            market_cap = float(shares) * float(latest_price)
        return TickerDetail(
            ticker=ticker,
            market_cap=market_cap,
            shares_outstanding=shares,
        )
    except Exception as exc:
        logger.warning("ticker_details: %s — %s", ticker, exc)
        return None


def run_enrichment(db_path: str, delay: float = 0.12) -> dict:
    """Enrich companies.market_cap for all active tickers that have a CIK.

    Uses SEC EDGAR XBRL for shares outstanding × latest price from prices_daily.
    Returns dict with keys: updated, skipped, errors, reason.
    """
    import duckdb

    conn = duckdb.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT c.ticker, c.cik,
                   COALESCE(p.adjusted_close, p.close) AS latest_price
            FROM companies c
            LEFT JOIN (
                SELECT ticker,
                       COALESCE(adjusted_close, close) AS adjusted_close,
                       close,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn
                FROM prices_daily
            ) p ON p.ticker = c.ticker AND p.rn = 1
            WHERE c.is_active = true
              AND c.cik IS NOT NULL
              AND c.active_sec_reporter IS NOT false
            ORDER BY c.ticker
        """).fetchall()

        updated = skipped = errors = 0
        total = len(rows)
        logger.info("ticker_details: enriching %d tickers via SEC EDGAR", total)

        for i, (ticker, cik, latest_price) in enumerate(rows, 1):
            detail = enrich_ticker_details(ticker, str(cik), latest_price)
            if detail is None:
                errors += 1
            elif detail.market_cap is not None:
                conn.execute(
                    "UPDATE companies SET market_cap = ? WHERE ticker = ?",
                    [detail.market_cap, ticker],
                )
                updated += 1
                logger.debug("ticker_details: %s market_cap=%.0f", ticker, detail.market_cap)
            else:
                skipped += 1
                logger.debug("ticker_details: %s — no shares or price", ticker)

            if i % 50 == 0:
                logger.info(
                    "ticker_details: %d/%d — updated=%d skipped=%d errors=%d",
                    i, total, updated, skipped, errors,
                )

            if delay > 0:
                time.sleep(delay)

        return {"updated": updated, "skipped": skipped, "errors": errors, "reason": "ok"}
    finally:
        conn.close()
