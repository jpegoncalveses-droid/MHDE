"""Build a priority refresh queue: tickers ordered by data staleness."""
from __future__ import annotations
import csv
import datetime
import os
from typing import Optional


def build_priority_queue(
    conn,
    as_of_date: Optional[str] = None,
    max_tickers: int = 100,
    price_only_tickers: Optional[set[str]] = None,
    price_only_p1_tickers: Optional[set[str]] = None,
    price_only_p2_tickers: Optional[set[str]] = None,
    polygon_missing_tickers: Optional[set[str]] = None,
) -> list[dict]:
    """Return tickers that need data refresh, sorted by urgency (1 = most urgent).

    Priority levels:
      1 -- no prices ever
      1 -- price_only_scored_miss true_miss tickers
      2 -- stale prices (> 10 days old)
      2 -- price_only_scored_miss near_threshold / scored_missed tickers
      2 -- polygon_fundamentals_missing_miss (has filing+prices, no market_cap)
      3 -- no fundamentals (last_financial_filing_date IS NULL)
      4 -- no market_cap (generic)
    Tickers with complete data are excluded unless flagged via a miss set.

    price_only_p1_tickers: true_miss tickers (→ P1)
    price_only_p2_tickers: near_threshold / scored_missed tickers (→ P2)
    price_only_tickers: legacy — treated as P1 (backward compat)
    polygon_missing_tickers: tickers with polygon_fundamentals_missing root cause (→ P2)
    """
    if as_of_date is None:
        as_of_date = str(datetime.date.today())

    rows = conn.execute("""
        SELECT
            c.ticker,
            c.last_financial_filing_date,
            c.market_cap,
            c.sector,
            p.last_price_date,
            CAST(? AS DATE) - p.last_price_date AS price_age_days
        FROM companies c
        LEFT JOIN (
            SELECT ticker, MAX(trade_date) AS last_price_date
            FROM prices_daily
            GROUP BY ticker
        ) p ON p.ticker = c.ticker
        WHERE c.is_active = true
        ORDER BY c.ticker
    """, [as_of_date]).fetchall()

    p1_set = (price_only_p1_tickers or set()) | (price_only_tickers or set())
    p2_set = price_only_p2_tickers or set()
    poly_set = polygon_missing_tickers or set()

    queue: list[dict] = []
    for ticker, last_filing, market_cap, sector, last_price_date, price_age in rows:
        reasons: list[str] = []
        priority = 99

        if last_price_date is None:
            reasons.append("no_prices")
            priority = min(priority, 1)
        elif price_age is not None and int(price_age) > 10:
            reasons.append(f"stale_prices_{int(price_age)}d")
            priority = min(priority, 2)

        if last_filing is None:
            reasons.append("no_fundamentals")
            priority = min(priority, 3)

        if market_cap is None:
            reasons.append("no_market_cap")
            priority = min(priority, 4)

        if ticker in p1_set:
            reasons.append("price_only_scored_miss")
            priority = min(priority, 1)
        elif ticker in p2_set:
            reasons.append("price_only_scored_miss")
            priority = min(priority, 2)

        if ticker in poly_set:
            reasons.append("polygon_fundamentals_missing_miss")
            priority = min(priority, 2)

        if reasons:
            queue.append({
                "ticker": ticker,
                "priority": priority,
                "reason": "|".join(reasons),
                "last_price_date": str(last_price_date) if last_price_date else "",
                "last_filing_date": str(last_filing) if last_filing else "",
                "market_cap": market_cap,
                "sector": sector or "",
            })

    queue.sort(key=lambda x: x["priority"])
    return queue[:max_tickers]


def save_priority_queue(queue: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fieldnames = ["ticker", "priority", "reason", "last_price_date", "last_filing_date", "market_cap", "sector"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(queue)
