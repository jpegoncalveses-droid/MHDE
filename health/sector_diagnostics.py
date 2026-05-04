"""Sector cluster diagnostics — classify why a sector_cluster_move row was missed."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Inverse of ETF_TO_SECTOR from ingestion/ingest_sector_etfs.py
SECTOR_TO_ETF: dict[str, str] = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
}


@dataclass
class SectorClusterDiag:
    ticker: str
    event_date: str
    sector: Optional[str]
    etf_ticker: Optional[str]
    etf_price_count: int
    subcause: str


def get_etf_coverage(conn) -> dict[str, int]:
    """Return {etf_ticker: row_count} for all sector ETF tickers in prices_daily."""
    etf_set = set(SECTOR_TO_ETF.values())
    try:
        rows = conn.execute(
            "SELECT ticker, COUNT(*) FROM prices_daily GROUP BY ticker"
        ).fetchall()
        return {ticker: count for ticker, count in rows if ticker in etf_set}
    except Exception as exc:
        logger.warning("sector_diagnostics.get_etf_coverage: %s", exc)
        return {}


def classify_sector_cluster_row(
    ticker: str,
    sector: Optional[str],
    etf_coverage: dict[str, int],
) -> str:
    """Return the most specific subcause for a sector_cluster_move row.

    Subcause hierarchy:
      missing_sector_mapping      — no sector or sector not in SECTOR_TO_ETF
      missing_sector_etf_prices   — ETF mapped but 0 rows in prices_daily
      peer_cluster_only_no_etf_data — ETF has prices but enrichment doesn't use them
    """
    if not sector:
        return "missing_sector_mapping"
    etf = SECTOR_TO_ETF.get(sector)
    if etf is None:
        return "missing_sector_mapping"
    if etf_coverage.get(etf, 0) == 0:
        return "missing_sector_etf_prices"
    return "peer_cluster_only_no_etf_data"


def generate_sector_diagnostics(
    conn,
    enriched_rows: list[dict],
) -> list[SectorClusterDiag]:
    """Return SectorClusterDiag for every sector_cluster_move row in enriched_rows."""
    cluster_rows = [
        r for r in enriched_rows
        if r.get("enriched_root_cause") == "sector_cluster_move"
    ]
    if not cluster_rows:
        return []

    etf_coverage = get_etf_coverage(conn)

    sector_map: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT ticker, sector FROM companies WHERE is_active = true AND sector IS NOT NULL"
        ).fetchall()
        sector_map = {ticker: sector for ticker, sector in rows}
    except Exception as exc:
        logger.warning("sector_diagnostics.generate: could not load sector_map: %s", exc)

    result: list[SectorClusterDiag] = []
    for row in cluster_rows:
        ticker = str(row.get("ticker", ""))
        sector = sector_map.get(ticker)
        etf = SECTOR_TO_ETF.get(sector or "")
        count = etf_coverage.get(etf or "", 0) if etf else 0
        subcause = classify_sector_cluster_row(ticker, sector, etf_coverage)
        result.append(SectorClusterDiag(
            ticker=ticker,
            event_date=str(row.get("event_date", "")),
            sector=sector,
            etf_ticker=etf,
            etf_price_count=count,
            subcause=subcause,
        ))
    return result
