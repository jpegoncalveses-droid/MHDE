from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.sec_edgar")

_BASE = "https://data.sec.gov"
_USER_AGENT = "MHDE-Engine contact@example.com"
_RATE_DELAY = 0.12


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

    def ingest(self, conn, run_id, tickers):
        started = datetime.utcnow()
        attempted = inserted = failed = 0

        for ticker in tickers:
            cik = self._get_cik(conn, ticker)
            if not cik:
                continue

            cik_padded = cik.zfill(10)

            # Fetch recent filings
            data = self._get(f"{_BASE}/submissions/CIK{cik_padded}.json")
            if data:
                recent = data.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                accessions = recent.get("accessionNumber", [])
                dates = recent.get("filingDate", [])
                descriptions = recent.get("primaryDocument", [])

                for form, acc, date_str, desc in zip(forms, accessions, dates, descriptions):
                    attempted += 1
                    try:
                        filing_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None
                        conn.execute(
                            """
                            INSERT INTO filings
                                (id, ticker, cik, form_type, accession_number,
                                 filing_date, description, run_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT DO NOTHING
                            """,
                            [
                                uuid.uuid4().hex[:16], ticker, cik, form,
                                acc, filing_date, str(desc)[:500], run_id,
                                datetime.utcnow(),
                            ],
                        )
                        inserted += 1
                    except Exception:
                        failed += 1

            # Fetch XBRL facts (fundamentals)
            facts = self._get(f"{_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json")
            if facts:
                us_gaap = facts.get("facts", {}).get("us-gaap", {})
                for concept, concept_data in us_gaap.items():
                    units = concept_data.get("units", {})
                    for unit_type, entries in units.items():
                        for entry in entries[-4:]:  # keep last 4 periods
                            attempted += 1
                            try:
                                val = entry.get("val")
                                end = entry.get("end")
                                form = entry.get("form", "")
                                as_of = datetime.strptime(end, "%Y-%m-%d").date() if end else None
                                conn.execute(
                                    """
                                    INSERT INTO fundamentals_raw
                                        (id, ticker, cik, concept, value, unit,
                                         as_of_date, form, run_id, created_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    ON CONFLICT DO NOTHING
                                    """,
                                    [
                                        uuid.uuid4().hex[:16], ticker, cik,
                                        f"us-gaap/{concept}", val, unit_type,
                                        as_of, form, run_id, datetime.utcnow(),
                                    ],
                                )
                                inserted += 1
                            except Exception:
                                failed += 1

        self.log_run(conn, run_id, "filings+fundamentals", "ok",
                     attempted, inserted, failed, started_at=started)
        self.logger.info("SEC: %d inserted, %d failed (of %d)", inserted, failed, attempted)
        return {"source": self.source_name, "status": "ok",
                "records": inserted, "failed": failed}
