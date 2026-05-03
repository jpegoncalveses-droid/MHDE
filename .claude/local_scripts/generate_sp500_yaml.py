#!/usr/bin/env python3
"""Fetch the S&P 500 constituent table from Wikipedia and write universe/sp500_tickers.yaml.

Run from the MHDE project root:
    venv/bin/python .claude/local_scripts/generate_sp500_yaml.py

Wikipedia table columns (0-indexed within <td> cells):
    0  Symbol          (ticker)
    1  Security        (company name)
    2  GICS Sector
    3  GICS Sub-Industry
    4  Headquarters Location
    5  Date First Added
    6  CIK
    7  Founded
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import requests
import yaml

_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_USER_AGENT = "MHDE-Engine contact@example.com"


def _clean(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    return text.replace("\n", " ").replace("\xa0", " ").strip()


def fetch_sp500() -> list[dict]:
    resp = requests.get(_URL, headers={"User-Agent": _USER_AGENT}, timeout=30)
    resp.raise_for_status()
    # Wikipedia has two wikitables: first is current constituents, second is historical changes.
    # Extract only the first wikitable to avoid picking up the changes table.
    tables = re.findall(
        r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>(.*?)</table>',
        resp.text,
        re.DOTALL,
    )
    if not tables:
        print("ERROR: no wikitable found on the page", file=sys.stderr)
        sys.exit(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.DOTALL)
    companies: list[dict] = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < 4:
            continue
        ticker = _clean(cells[0])
        name = _clean(cells[1])
        if not ticker or not name:
            continue
        entry: dict = {"ticker": ticker, "company_name": name}
        sector = _clean(cells[2]) if len(cells) > 2 else ""
        industry = _clean(cells[3]) if len(cells) > 3 else ""
        cik_raw = _clean(cells[6]) if len(cells) > 6 else ""
        if sector:
            entry["sector"] = sector
        if industry:
            entry["industry"] = industry
        if cik_raw and re.fullmatch(r"\d+", cik_raw):
            entry["cik"] = cik_raw.zfill(10)
        companies.append(entry)
    return companies


def main() -> None:
    print(f"Fetching S&P 500 list from {_URL} ...", file=sys.stderr)
    companies = fetch_sp500()
    if len(companies) < 400:
        print(
            f"ERROR: only {len(companies)} companies found — "
            "Wikipedia table format may have changed",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Found {len(companies)} companies", file=sys.stderr)
    data = {
        "last_updated": str(date.today()),
        "source": _URL,
        "tickers": companies,
    }
    out = Path("universe/sp500_tickers.yaml")
    with out.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"Written {len(companies)} entries to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
