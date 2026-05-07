"""Diagnose why tickers receive an Incomplete score."""
from __future__ import annotations

import csv
import datetime
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class IncompleteReason(Enum):
    MISSING_PRICES = "missing_prices"
    STALE_FUNDAMENTALS = "stale_fundamentals"
    MISSING_FUNDAMENTALS = "missing_fundamentals"
    IFRS_FILER = "ifrs_filer"
    FOREIGN_FILER = "foreign_filer"
    MISSING_VALUATION_INPUTS = "missing_valuation_inputs"
    LOW_FEATURE_COVERAGE = "low_feature_coverage"
    UNKNOWN = "unknown"


@dataclass
class IncompleteDiagnostic:
    ticker: str
    reason: str
    sector: str
    detail: str


def _query_incomplete_tickers(conn) -> list[tuple]:
    return conn.execute("""
        SELECT s.ticker,
               s.tier,
               s.missing_data_json,
               s.confidence,
               c.last_financial_filing_date,
               c.sector
        FROM scores s
        LEFT JOIN companies c ON s.ticker = c.ticker
        WHERE s.tier = 'Incomplete'
          AND s.run_id = (SELECT MAX(run_id) FROM scores)
    """).fetchall()


def _classify_reason(row: tuple) -> IncompleteDiagnostic:
    ticker, _tier, missing_json, confidence, last_filing, sector = row

    missing: dict = {}
    try:
        missing = json.loads(missing_json or "{}")
    except Exception:
        pass

    missing_str = json.dumps(missing).lower()
    reason = IncompleteReason.UNKNOWN
    detail = ""

    # IFRS check (common for foreign large-caps)
    if "ifrs" in missing_str:
        reason = IncompleteReason.IFRS_FILER
        detail = "IFRS reporter — XBRL concepts differ from US GAAP"
    # Valuation inputs
    elif "valuation" in missing_str or "pe_proxy" in missing_str or "ps_proxy" in missing_str:
        reason = IncompleteReason.MISSING_VALUATION_INPUTS
        detail = f"missing: {missing_str[:80]}"
    # Stale or missing fundamentals based on filing date
    elif last_filing:
        try:
            days_stale = (datetime.date.today() - datetime.date.fromisoformat(str(last_filing))).days
            if days_stale > 365:
                reason = IncompleteReason.MISSING_FUNDAMENTALS
                detail = f"no filing in {days_stale}d"
            elif days_stale > 180:
                reason = IncompleteReason.STALE_FUNDAMENTALS
                detail = f"last filing {days_stale}d ago"
        except Exception:
            pass
    # Low coverage
    if reason == IncompleteReason.UNKNOWN and confidence is not None and float(confidence or 0) < 0.3:
        reason = IncompleteReason.LOW_FEATURE_COVERAGE
        detail = f"coverage={float(confidence):.2f}"

    return IncompleteDiagnostic(
        ticker=ticker,
        reason=reason.value,
        sector=sector or "",
        detail=detail,
    )


def diagnose_incomplete(conn) -> list[IncompleteDiagnostic]:
    """Query latest run and return diagnostics for all Incomplete-tier tickers."""
    rows = _query_incomplete_tickers(conn)
    return [_classify_reason(r) for r in rows]


def write_diagnostics_csv(diagnostics: list[IncompleteDiagnostic], path: str) -> None:
    """Write diagnostics to a CSV file."""
    if not diagnostics:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["ticker", "reason", "sector", "detail"]
        )
        writer.writeheader()
        for d in diagnostics:
            writer.writerow(
                {"ticker": d.ticker, "reason": d.reason, "sector": d.sector, "detail": d.detail}
            )
