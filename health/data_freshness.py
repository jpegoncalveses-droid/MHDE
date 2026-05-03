"""Per-ticker data freshness metrics computed from companies + prices_daily."""
from __future__ import annotations
import datetime
from dataclasses import dataclass
from typing import Optional


@dataclass
class TickerFreshness:
    ticker: str
    has_prices: bool
    price_age_days: Optional[int]
    has_fundamentals: bool
    filing_age_days: Optional[int]
    has_market_cap: bool
    freshness_label: str  # "fresh" | "stale" | "missing"


def compute_freshness(conn, as_of_date: Optional[str] = None) -> list[TickerFreshness]:
    """Compute freshness for all active tickers using an open DuckDB connection."""
    if as_of_date is None:
        as_of_date = str(datetime.date.today())
    rows = conn.execute("""
        SELECT
            c.ticker,
            c.last_financial_filing_date,
            c.market_cap,
            p.last_price_date,
            CAST(? AS DATE) - p.last_price_date AS price_age_days,
            CAST(? AS DATE) - c.last_financial_filing_date AS filing_age_days
        FROM companies c
        LEFT JOIN (
            SELECT ticker, MAX(trade_date) AS last_price_date
            FROM prices_daily
            GROUP BY ticker
        ) p ON p.ticker = c.ticker
        WHERE c.is_active = true
        ORDER BY c.ticker
    """, [as_of_date, as_of_date]).fetchall()

    results: list[TickerFreshness] = []
    for ticker, last_filing, market_cap, last_price_date, price_age, filing_age in rows:
        has_prices = last_price_date is not None
        has_fundamentals = last_filing is not None
        has_market_cap = market_cap is not None

        if not has_prices:
            label = "missing"
        elif price_age is not None and int(price_age) > 10:
            label = "stale"
        else:
            label = "fresh"

        results.append(TickerFreshness(
            ticker=ticker,
            has_prices=has_prices,
            price_age_days=int(price_age) if price_age is not None else None,
            has_fundamentals=has_fundamentals,
            filing_age_days=int(filing_age) if filing_age is not None else None,
            has_market_cap=has_market_cap,
            freshness_label=label,
        ))
    return results


def freshness_summary(results: list[TickerFreshness]) -> dict:
    total = len(results)
    return {
        "total": total,
        "has_prices": sum(1 for r in results if r.has_prices),
        "has_fundamentals": sum(1 for r in results if r.has_fundamentals),
        "has_market_cap": sum(1 for r in results if r.has_market_cap),
        "fresh": sum(1 for r in results if r.freshness_label == "fresh"),
        "stale": sum(1 for r in results if r.freshness_label == "stale"),
        "missing": sum(1 for r in results if r.freshness_label == "missing"),
    }
