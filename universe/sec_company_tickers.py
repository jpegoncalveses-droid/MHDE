from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger("mhde.universe")

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_USER_AGENT = "MHDE-Engine contact@example.com"


def fetch_sec_company_tickers(timeout: int = 30) -> list[dict]:
    """Fetch company ticker list from SEC. Returns list of {ticker, cik, company_name}."""
    try:
        r = requests.get(
            _SEC_TICKERS_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        logger.error("Failed to fetch SEC company tickers: %s", exc)
        return []

    companies = []
    for entry in raw.values():
        cik = str(entry.get("cik_str", "")).zfill(10)
        ticker = (entry.get("ticker") or "").upper().strip()
        name = (entry.get("title") or "").strip()
        if ticker and name:
            companies.append({"ticker": ticker, "cik": cik, "company_name": name})

    logger.info("Fetched %d companies from SEC", len(companies))
    return companies
