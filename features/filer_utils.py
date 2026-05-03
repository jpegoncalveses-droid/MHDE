"""Shared utilities for detecting foreign filer status and reporting currency."""
from __future__ import annotations

import duckdb

_FOREIGN_FORMS = {"20-F", "6-K", "40-F"}

# All ISO 4217 monetary currencies that could appear in XBRL unit fields.
# Does not include meta-units like 'pure', 'shares', 'USD/shares'.
_MONETARY_CURRENCIES = {
    "USD", "CNY", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "HKD",
    "INR", "KRW", "BRL", "MXN", "SEK", "NOK", "DKK", "SGD", "TWD",
    "ZAR", "RUB", "ILS", "TRY", "SAR", "AED", "NZD", "THB", "IDR",
}


def is_foreign_filer(conn: duckdb.DuckDBPyConnection, ticker: str) -> bool:
    """Return True if any 20-F, 6-K, or 40-F filing exists for this ticker."""
    placeholders = ",".join(["?"] * len(_FOREIGN_FORMS))
    row = conn.execute(
        f"SELECT COUNT(*) FROM filings WHERE ticker=? AND form_type IN ({placeholders})",
        [ticker] + list(_FOREIGN_FORMS),
    ).fetchone()
    return bool(row and row[0] > 0)


def get_reporting_currency(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    concepts: list[str],
) -> str | None:
    """
    Return the most-recent monetary unit for the given concepts.

    Returns:
        'USD'         — filer reports in USD
        '<currency>'  — filer reports in a non-USD currency (e.g. 'CNY')
        None          — no monetary unit found (unknown)
    """
    placeholders = ",".join(["?"] * len(concepts))
    row = conn.execute(
        f"""
        SELECT unit FROM fundamentals_raw
        WHERE ticker=? AND concept IN ({placeholders})
          AND unit IS NOT NULL AND value IS NOT NULL
        ORDER BY as_of_date DESC LIMIT 1
        """,
        [ticker] + concepts,
    ).fetchone()
    if not row or row[0] not in _MONETARY_CURRENCIES:
        return None
    return row[0]
