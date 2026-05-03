from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import duckdb

from universe.sec_company_tickers import fetch_sec_company_tickers
from universe.filters import filter_non_equities, classify_company
from universe.sp500_loader import load_sp500_yaml
from universe.cik_validator import validate_cik_vs_sec, write_validation_report

logger = logging.getLogger("mhde.universe")

UNIVERSE_MODES = ("sp500", "us_large_cap", "extended")
DEFAULT_UNIVERSE_MODE = "sp500"

_SP500_YAML = Path(__file__).parent / "sp500_tickers.yaml"
_VALIDATION_REPORT = Path("data/processed/sp500_cik_validation_report.csv")

_WARNING = (
    "Universe primary tier sourced from universe/sp500_tickers.yaml + "
    "config fallback_tickers. Extended tier filled from SEC list filtered "
    "by name heuristics. No market-cap or liquidity filter applied."
)


def build_universe(conn: duckdb.DuckDBPyConnection, cfg: dict) -> int:
    """Build the universe: primary tier from S&P 500 YAML + config fallback,
    extended tier from filtered SEC list. Returns active company count.
    """
    universe_cfg = cfg.get("universe", {})
    max_symbols = universe_cfg.get("max_symbols", 500)
    config_fallback = [t.upper() for t in universe_cfg.get("fallback_tickers", [])]

    sp500_entries = load_sp500_yaml(_SP500_YAML)

    logger.warning(_WARNING)

    # Fetch SEC list first so we can validate and correct YAML CIKs
    raw = fetch_sec_company_tickers()
    if not raw:
        logger.warning("SEC fetch failed — using primary list only")
        raw = []

    # Validate YAML CIKs against SEC authoritative list
    sec_map: dict[str, str] = {co["ticker"]: co["cik"] for co in raw if co.get("cik")}
    corrected_sp500, report_rows = validate_cik_vs_sec(sp500_entries, sec_map)
    try:
        write_validation_report(report_rows, _VALIDATION_REPORT)
    except Exception as exc:
        logger.warning("Could not write CIK validation report: %s", exc)

    # Build primary_meta from CIK-corrected YAML entries
    primary_meta: dict[str, dict] = {}
    for e in corrected_sp500:
        t = e.get("ticker", "").upper()
        if t:
            primary_meta[t] = e
    for t in config_fallback:
        if t not in primary_meta:
            primary_meta[t] = {"ticker": t}

    filtered = filter_non_equities(raw, universe_cfg)
    filtered_lookup: dict[str, dict] = {co["ticker"]: co for co in filtered}

    seen: set[str] = set()
    ordered: list[dict] = []

    # Primary tier: never capped by max_symbols
    for ticker, meta in primary_meta.items():
        if ticker in seen:
            continue
        if ticker in filtered_lookup:
            co = filtered_lookup[ticker].copy()
        else:
            co = {
                "ticker": ticker,
                "cik": meta.get("cik"),
                "company_name": meta.get("company_name", ticker),
                "is_etf": False,
                "is_fund": False,
                "is_adr": False,
                "is_active": True,
            }
            co = classify_company(co)
        co["universe_tier"] = "primary"
        co["sector"] = meta.get("sector")
        co["industry"] = meta.get("industry")
        if meta.get("cik"):
            co["cik"] = meta["cik"]
        ordered.append(co)
        seen.add(ticker)

    # Extended tier: fills up to max_symbols
    for co in filtered:
        if len(ordered) >= max_symbols:
            break
        if co["ticker"] not in seen:
            co["universe_tier"] = "extended"
            co["sector"] = None
            co["industry"] = None
            ordered.append(co)
            seen.add(co["ticker"])

    now = datetime.utcnow()
    for co in ordered:
        try:
            conn.execute(
                """
                INSERT INTO companies
                    (ticker, cik, company_name, sector, industry,
                     is_etf, is_fund, is_adr, is_active, universe_tier,
                     last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticker) DO UPDATE SET
                    cik            = COALESCE(excluded.cik, companies.cik),
                    company_name   = excluded.company_name,
                    sector         = COALESCE(excluded.sector, companies.sector),
                    industry       = COALESCE(excluded.industry, companies.industry),
                    is_etf         = excluded.is_etf,
                    is_fund        = excluded.is_fund,
                    is_adr         = excluded.is_adr,
                    is_active      = excluded.is_active,
                    universe_tier  = excluded.universe_tier,
                    last_seen_at   = excluded.last_seen_at,
                    updated_at     = excluded.updated_at
                """,
                [
                    co["ticker"],
                    co.get("cik"),
                    co.get("company_name", co["ticker"]),
                    co.get("sector"),
                    co.get("industry"),
                    co.get("is_etf", False),
                    co.get("is_fund", False),
                    co.get("is_adr", False),
                    co.get("is_active", True),
                    co.get("universe_tier", "extended"),
                    now,
                    now,
                ],
            )
        except Exception as exc:
            logger.warning("Failed to upsert %s: %s", co.get("ticker"), exc)

    # Reconcile: deactivate primary-tier rows no longer in the primary set
    current_primary = list(primary_meta.keys())
    if current_primary:
        placeholders = ", ".join("?" * len(current_primary))
        conn.execute(
            f"""
            UPDATE companies
            SET is_active = false
            WHERE universe_tier = 'primary'
              AND ticker NOT IN ({placeholders})
            """,
            current_primary,
        )
    else:
        conn.execute(
            "UPDATE companies SET is_active = false WHERE universe_tier = 'primary'"
        )

    count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE is_active = true"
    ).fetchone()[0]
    logger.info(
        "Universe built: %d active (%d primary, %d extended slots used)",
        count,
        len(current_primary),
        max(0, count - len(current_primary)),
    )
    return count
