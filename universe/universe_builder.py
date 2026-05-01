from __future__ import annotations

import logging
import uuid
from datetime import datetime

import duckdb

from universe.sec_company_tickers import fetch_sec_company_tickers
from universe.filters import filter_non_equities, classify_company

logger = logging.getLogger("mhde.universe")

_WARNING = (
    "WARNING: Universe is built from SEC company list with name-based filters only. "
    "No market cap, liquidity, or price filters applied. May include micro-caps. "
    "Review candidates carefully."
)


def build_universe(conn: duckdb.DuckDBPyConnection, cfg: dict) -> int:
    """Fetch SEC company tickers, filter, and upsert into companies table.

    Returns number of companies in universe after build.
    """
    universe_cfg = cfg.get("universe", {})
    max_symbols = universe_cfg.get("max_symbols", 500)
    fallback_tickers = [t.upper() for t in universe_cfg.get("fallback_tickers", [])]

    logger.warning(_WARNING)

    # Fetch from SEC
    raw = fetch_sec_company_tickers()

    if not raw:
        logger.warning("SEC fetch failed — falling back to config tickers only")
        raw = []

    # Filter non-equities
    filtered = filter_non_equities(raw, universe_cfg)

    # Build a lookup for fast dedup
    seen: set[str] = set()
    ordered: list[dict] = []

    # Fallback tickers always go first (highest priority)
    fallback_lookup = {co["ticker"]: co for co in filtered}
    for ticker in fallback_tickers:
        if ticker not in seen:
            if ticker in fallback_lookup:
                co = fallback_lookup[ticker].copy()
            else:
                co = {
                    "ticker": ticker,
                    "cik": None,
                    "company_name": ticker,
                    "is_etf": False,
                    "is_fund": False,
                    "is_adr": False,
                    "is_active": True,
                }
                co = classify_company(co)
            co["universe_tier"] = "primary"
            ordered.append(co)
            seen.add(ticker)

    # Fill remaining up to max_symbols
    for co in filtered:
        if len(ordered) >= max_symbols:
            break
        if co["ticker"] not in seen:
            co["universe_tier"] = "extended"
            ordered.append(co)
            seen.add(co["ticker"])

    now = datetime.utcnow()
    inserted = 0
    for co in ordered:
        try:
            conn.execute(
                """
                INSERT INTO companies
                    (ticker, cik, company_name, is_etf, is_fund, is_adr,
                     is_active, universe_tier, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticker) DO UPDATE SET
                    cik = excluded.cik,
                    company_name = excluded.company_name,
                    is_etf = excluded.is_etf,
                    is_fund = excluded.is_fund,
                    is_adr = excluded.is_adr,
                    is_active = excluded.is_active,
                    universe_tier = excluded.universe_tier,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                [
                    co["ticker"],
                    co.get("cik"),
                    co.get("company_name", co["ticker"]),
                    co.get("is_etf", False),
                    co.get("is_fund", False),
                    co.get("is_adr", False),
                    co.get("is_active", True),
                    co.get("universe_tier", "extended"),
                    now,
                    now,
                ],
            )
            inserted += 1
        except Exception as exc:
            logger.warning("Failed to upsert %s: %s", co.get("ticker"), exc)

    count = conn.execute("SELECT COUNT(*) FROM companies WHERE is_active = true").fetchone()[0]
    logger.info("Universe built: %d companies (primary: %d)", count, len(fallback_tickers))
    return count
