"""Industry classification utilities for MHDE feature guards."""
from __future__ import annotations

import duckdb

# XBRL concepts unique to banks
_BANK_XBRL = {
    "us-gaap/NetInterestIncome",
    "us-gaap/InterestAndDividendIncomeOperating",
    # Note: InterestIncomeExpenseNet excluded — non-banks report negative values (interest expense)
    "us-gaap/LoansAndLeasesReceivableNetReportedAmount",
}

# XBRL concepts unique to insurers — presence overrides bank detection
_INSURER_XBRL = {
    "us-gaap/SupplementaryInsuranceInformationPremiumRevenue",
    "us-gaap/PolicyholderBenefitsAndClaimsIncurredNet",
    "us-gaap/PremiumsEarnedNet",
    "us-gaap/InsuranceLossReserves",
}

# Company name keywords (case-insensitive) that signal industry
_BANK_NAME_KEYWORDS = {
    "BANCORP", "BANCSHARES", "BANKERS", "BANKING",
    "NATIONAL BANK", "SAVINGS BANK", "FEDERAL SAVINGS",
    "FINANCIAL GROUP",   # e.g. Citizens Financial Group, First Financial Group
}
_INSURER_NAME_KEYWORDS = {
    "INSURANCE", "INSURER", "REINSURANCE", "REINSURER",
    "CASUALTY", "LIFE & ANNUITY",
}


def detect_industry(conn: duckdb.DuckDBPyConnection, ticker: str) -> dict:
    """Return industry flags for a ticker.

    Returns:
        dict with keys:
            is_bank    — True if detected as a bank/thrift
            is_insurer — True if detected as an insurer (overrides bank)
    """
    # XBRL-based detection (primary signal)
    bank_xbrl_placeholders = ",".join(["?"] * len(_BANK_XBRL))
    insurer_xbrl_placeholders = ",".join(["?"] * len(_INSURER_XBRL))

    bank_row = conn.execute(
        f"SELECT COUNT(DISTINCT concept) FROM fundamentals_raw WHERE ticker=? AND concept IN ({bank_xbrl_placeholders})",
        [ticker] + list(_BANK_XBRL),
    ).fetchone()
    insurer_row = conn.execute(
        f"SELECT COUNT(DISTINCT concept) FROM fundamentals_raw WHERE ticker=? AND concept IN ({insurer_xbrl_placeholders})",
        [ticker] + list(_INSURER_XBRL),
    ).fetchone()

    bank_xbrl_count = bank_row[0] if bank_row else 0
    insurer_xbrl_count = insurer_row[0] if insurer_row else 0

    # Insurer XBRL overrides bank XBRL (insurers also file investment income concepts)
    if insurer_xbrl_count > 0:
        return {"is_bank": False, "is_insurer": True}
    if bank_xbrl_count > 0:
        return {"is_bank": True, "is_insurer": False}

    # Name-based fallback
    name_row = conn.execute(
        "SELECT company_name FROM companies WHERE ticker=?", [ticker]
    ).fetchone()
    if name_row and name_row[0]:
        name_upper = name_row[0].upper()
        if any(kw in name_upper for kw in _INSURER_NAME_KEYWORDS):
            return {"is_bank": False, "is_insurer": True}
        if any(kw in name_upper for kw in _BANK_NAME_KEYWORDS):
            return {"is_bank": True, "is_insurer": False}

    return {"is_bank": False, "is_insurer": False}


# Revenue concepts that are fee-income only — not usable as bank total revenue for P/S
_BANK_FEE_INCOME_ONLY_CONCEPTS = {
    "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap/SalesRevenueGoodsNet",
    "us-gaap/SalesRevenueServicesNet",
    "us-gaap/SalesRevenueNet",
}

# Revenue concepts acceptable for bank P/S computation
_BANK_TOTAL_REVENUE_CONCEPTS = {
    "us-gaap/Revenues",
}


def bank_has_total_revenue(conn: duckdb.DuckDBPyConnection, ticker: str) -> bool:
    """Return True if at least one bank-acceptable total revenue concept exists."""
    placeholders = ",".join(["?"] * len(_BANK_TOTAL_REVENUE_CONCEPTS))
    row = conn.execute(
        f"SELECT COUNT(*) FROM fundamentals_raw WHERE ticker=? AND concept IN ({placeholders}) AND value IS NOT NULL",
        [ticker] + list(_BANK_TOTAL_REVENUE_CONCEPTS),
    ).fetchone()
    return bool(row and row[0] > 0)
