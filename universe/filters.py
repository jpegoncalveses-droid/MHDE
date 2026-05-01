from __future__ import annotations

import re

_ETF_KEYWORDS = [
    "ETF", "ISHARES", "SPDR", "VANGUARD", "INVESCO", "WISDOMTREE",
    "POWERSHARES", "PROSHARES", "DIREXION", "GRANITESHARES",
]
_FUND_KEYWORDS = [
    " FUND", "TRUST", "REIT", "BDC", "MLP ", "PARTNERSHIP",
]
_EXCLUDE_KEYWORDS = [
    "WARRANT", "UNIT SER", "PREFERRED", "NOTE DUE", "BOND DUE",
    "DEP SHS", "DEP SHARES", "DEPOSITARY", "ADR",
]
_BAD_TICKER_CHARS = re.compile(r"[.\-+]")


def _name_upper(name: str) -> str:
    return name.upper()


def classify_company(company: dict) -> dict:
    """Add is_etf, is_fund, is_adr flags based on name heuristics."""
    name = _name_upper(company.get("company_name", ""))
    ticker = company.get("ticker", "")

    is_etf = any(k in name for k in _ETF_KEYWORDS)
    is_fund = any(k in name for k in _FUND_KEYWORDS)
    is_adr = "ADR" in name or " ADS" in name

    company["is_etf"] = is_etf
    company["is_fund"] = is_fund
    company["is_adr"] = is_adr
    company["is_active"] = True
    company["universe_tier"] = "extended"
    return company


def filter_non_equities(companies: list[dict], cfg: dict) -> list[dict]:
    """Remove obvious non-equities based on config flags and name heuristics."""
    exclude_etfs = cfg.get("exclude_etfs", True)
    exclude_funds = cfg.get("exclude_funds", True)
    exclude_adrs = cfg.get("exclude_adrs", False)

    results = []
    for co in companies:
        co = classify_company(co)
        ticker = co.get("ticker", "")
        name = _name_upper(co.get("company_name", ""))

        # Skip tickers with bad characters (warrants, units, etc.)
        if _BAD_TICKER_CHARS.search(ticker):
            continue
        # Skip tickers longer than 5 chars
        if len(ticker) > 5:
            continue
        # Skip names with hard-exclude keywords
        if any(k in name for k in _EXCLUDE_KEYWORDS):
            continue
        if exclude_etfs and co["is_etf"]:
            continue
        if exclude_funds and co["is_fund"]:
            continue
        if exclude_adrs and co["is_adr"]:
            continue

        results.append(co)

    return results
