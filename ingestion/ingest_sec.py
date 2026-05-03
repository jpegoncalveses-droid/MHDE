from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.sec_edgar")

_BASE = "https://data.sec.gov"
_USER_AGENT = "MHDE-Engine contact@example.com"
_RATE_DELAY = 0.12

# Only the concepts MHDE scoring actually uses.
_WANTED_CONCEPTS = frozenset([
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "NetIncomeLoss",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
    "CommonStockSharesOutstanding",
    "CommonStockSharesIssued",
    "EarningsPerShareBasic",
    "EarningsPerShareDiluted",
    "AssetsCurrent",
    "LiabilitiesCurrent",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "LongTermDebt",
    "OperatingIncomeLoss",
    "GrossProfit",
    "ResearchAndDevelopmentExpense",
    "CapitalExpenditureDiscontinuedOperations",
    "PaymentsToAcquirePropertyPlantAndEquipment",
])

_FUNDAMENTALS_FRESHNESS_DAYS = 7
_FILINGS_FRESHNESS_DAYS = 1  # Refresh filings daily

# IFRS concept names → GAAP-equivalent name stored in fundamentals_raw.
# Stored as "ifrs-full/{gaap_name}" so LIKE '%NetIncomeLoss%' patterns still match.
_IFRS_CONCEPT_MAP: dict[str, str] = {
    "Revenue": "Revenues",
    "ProfitLoss": "NetIncomeLoss",
    "ProfitLossAttributableToOwnersOfParent": "NetIncomeLoss",
    "BasicEarningsLossPerShare": "EarningsPerShareBasic",
    "DilutedEarningsLossPerShare": "EarningsPerShareDiluted",
    "CurrentAssets": "AssetsCurrent",
    "CurrentLiabilities": "LiabilitiesCurrent",
    "Equity": "StockholdersEquity",
    "EquityAttributableToOwnersOfParent": "StockholdersEquity",
    "CashAndCashEquivalents": "CashAndCashEquivalentsAtCarryingValue",
    "ProfitLossFromOperatingActivities": "OperatingIncomeLoss",
    "GrossProfit": "GrossProfit",
    "ResearchAndDevelopmentExpense": "ResearchAndDevelopmentExpense",
    "WeightedAverageNumberOfOrdinarySharesOutstanding": "CommonStockSharesOutstanding",
}


class SECIngestor(BaseIngestor):
    source_name = "sec_edgar"

    def _headers(self) -> dict:
        ua = self.cfg.get("settings", {}).get("sec_edgar", {}).get("user_agent", _USER_AGENT)
        return {"User-Agent": ua, "Accept": "application/json"}

    def _get(self, url: str) -> dict | None:
        time.sleep(_RATE_DELAY)
        try:
            r = requests.get(url, headers=self._headers(), timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                self._not_found_count += 1
                logger.debug("SEC 404: %s", url)
            else:
                logger.warning("SEC %s -> HTTP %s", url, r.status_code)
        except Exception as exc:
            logger.warning("SEC fetch error: %s", exc)
        return None

    def _get_cik(self, conn, ticker: str) -> str | None:
        row = conn.execute(
            "SELECT cik FROM companies WHERE ticker = ?", [ticker]
        ).fetchone()
        if row and row[0]:
            return row[0].lstrip("0") or None
        return None

    def _filings_are_fresh(self, conn, ticker: str) -> bool:
        cutoff = date.today() - timedelta(days=_FILINGS_FRESHNESS_DAYS)
        row = conn.execute(
            "SELECT MAX(created_at) FROM filings WHERE ticker = ?", [ticker]
        ).fetchone()
        if not row or not row[0]:
            return False
        last = row[0]
        if hasattr(last, "date"):
            last = last.date()
        return last >= cutoff

    def _is_ifrs_filer(self, conn, ticker: str) -> bool:
        """True if the ticker has a 20-F or 40-F filing (foreign IFRS filer)."""
        row = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE ticker = ? AND form_type IN ('20-F', '40-F')",
            [ticker],
        ).fetchone()
        return bool(row and row[0] > 0)

    def _fundamentals_are_fresh(self, conn, ticker: str) -> bool:
        cutoff = date.today() - timedelta(days=_FUNDAMENTALS_FRESHNESS_DAYS)
        if self._is_ifrs_filer(conn, ticker):
            # For IFRS filers, freshness requires recent ifrs-full rows — not just any row.
            # A ticker with only stale us-gaap rows and no ifrs-full rows is NOT fresh.
            row = conn.execute(
                "SELECT MAX(created_at) FROM fundamentals_raw WHERE ticker = ? AND concept LIKE 'ifrs-full/%'",
                [ticker],
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(created_at) FROM fundamentals_raw WHERE ticker = ?", [ticker]
            ).fetchone()
        if not row or not row[0]:
            return False
        last = row[0]
        if hasattr(last, "date"):
            last = last.date()
        return last >= cutoff

    def ingest(self, conn, run_id, tickers):
        self._not_found_count = 0
        started = datetime.utcnow()
        attempted = inserted = failed = 0
        skipped_filings = skipped_fundamentals = 0

        incremental = self.cfg.get("ingestion", {}).get("incremental", True)
        skip_fundamentals = self.cfg.get("ingestion", {}).get("skip_sec_fundamentals", False)

        for ticker in tickers:
            cik = self._get_cik(conn, ticker)
            if not cik:
                continue

            cik_padded = cik.zfill(10)

            # Fetch recent filings — skip if already fresh
            if incremental and self._filings_are_fresh(conn, ticker):
                skipped_filings += 1
            else:
                data = self._get(f"{_BASE}/submissions/CIK{cik_padded}.json")
                if data:
                    recent = data.get("filings", {}).get("recent", {})
                    forms = recent.get("form", [])
                    accessions = recent.get("accessionNumber", [])
                    dates = recent.get("filingDate", [])
                    descriptions = recent.get("primaryDocument", [])

                    filing_rows = []
                    for form, acc, date_str, desc in zip(forms, accessions, dates, descriptions):
                        attempted += 1
                        try:
                            filing_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None
                            filing_rows.append([
                                uuid.uuid4().hex[:16], ticker, cik, form,
                                acc, filing_date, str(desc)[:500], run_id,
                                datetime.utcnow(),
                            ])
                        except Exception:
                            failed += 1

                    if filing_rows:
                        try:
                            conn.executemany(
                                """
                                INSERT INTO filings
                                    (id, ticker, cik, form_type, accession_number,
                                     filing_date, description, run_id, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT DO NOTHING
                                """,
                                filing_rows,
                            )
                            inserted += len(filing_rows)
                        except Exception as exc:
                            logger.warning("SEC filings batch insert error for %s: %s", ticker, exc)
                            failed += len(filing_rows)

            # Fetch XBRL fundamentals
            if skip_fundamentals or (incremental and self._fundamentals_are_fresh(conn, ticker)):
                skipped_fundamentals += 1
                continue

            facts = self._get(f"{_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json")
            if facts:
                facts_ns = facts.get("facts", {})
                fundamental_rows = []

                us_gaap = facts_ns.get("us-gaap", {})
                for concept, concept_data in us_gaap.items():
                    if concept not in _WANTED_CONCEPTS:
                        continue
                    units = concept_data.get("units", {})
                    for unit_type, entries in units.items():
                        for entry in entries[-4:]:
                            attempted += 1
                            try:
                                val = entry.get("val")
                                end = entry.get("end")
                                form = entry.get("form", "")
                                as_of = datetime.strptime(end, "%Y-%m-%d").date() if end else None
                                fundamental_rows.append([
                                    uuid.uuid4().hex[:16], ticker, cik,
                                    f"us-gaap/{concept}", val, unit_type,
                                    as_of, form, run_id, datetime.utcnow(),
                                ])
                            except Exception:
                                failed += 1

                ifrs_full = facts_ns.get("ifrs-full", {})
                for concept, concept_data in ifrs_full.items():
                    gaap_name = _IFRS_CONCEPT_MAP.get(concept)
                    if not gaap_name:
                        continue
                    units = concept_data.get("units", {})
                    for unit_type, entries in units.items():
                        for entry in entries[-4:]:
                            attempted += 1
                            try:
                                val = entry.get("val")
                                end = entry.get("end")
                                form = entry.get("form", "")
                                as_of = datetime.strptime(end, "%Y-%m-%d").date() if end else None
                                fundamental_rows.append([
                                    uuid.uuid4().hex[:16], ticker, cik,
                                    f"ifrs-full/{gaap_name}", val, unit_type,
                                    as_of, form, run_id, datetime.utcnow(),
                                ])
                            except Exception:
                                failed += 1

                if fundamental_rows:
                    try:
                        conn.executemany(
                            """
                            INSERT INTO fundamentals_raw
                                (id, ticker, cik, concept, value, unit,
                                 as_of_date, form, run_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT DO NOTHING
                            """,
                            fundamental_rows,
                        )
                        inserted += len(fundamental_rows)
                    except Exception as exc:
                        logger.warning("SEC fundamentals batch insert error for %s: %s", ticker, exc)
                        failed += len(fundamental_rows)

        if skipped_filings:
            logger.info("SEC: skipped %d tickers with fresh filings (<%dd old)",
                        skipped_filings, _FILINGS_FRESHNESS_DAYS)
        if skipped_fundamentals:
            logger.info("SEC: skipped %d tickers with fresh fundamentals (<%dd old)",
                        skipped_fundamentals, _FUNDAMENTALS_FRESHNESS_DAYS)

        if self._not_found_count:
            logger.warning(
                "SEC: %d requests returned 404 (CIK not found or no EDGAR filing)",
                self._not_found_count,
            )

        self.log_run(conn, run_id, "filings+fundamentals", "ok",
                     attempted, inserted, failed, started_at=started)
        self.logger.info("SEC: %d inserted, %d failed (of %d)", inserted, failed, attempted)
        return {"source": self.source_name, "status": "ok",
                "records": inserted, "failed": failed}
