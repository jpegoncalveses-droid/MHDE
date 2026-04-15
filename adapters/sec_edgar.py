from __future__ import annotations

import time
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.sec_edgar")

_REQUIRED_FILING_FIELDS = ["filings.recent.form", "filings.recent.accessionNumber", "filings.recent.filingDate"]
_REQUIRED_FACT_KEYS = ["us-gaap.NetIncomeLoss", "us-gaap.Revenues", "dei.EntityCommonStockSharesOutstanding"]

_CANARY_CIK = "0000320193"   # AAPL


class SECEdgarAdapter(BaseAdapter):
    source_name = "sec_edgar"
    use_cases = ["filings", "fundamentals"]

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = self.settings["sec_edgar"]["user_agent"]
        s.headers["Accept"] = "application/json"
        return s

    def _delay(self):
        delay = self.settings["sec_edgar"].get("rate_limit_delay", 0.12)
        if delay:
            time.sleep(delay)

    def _base(self) -> str:
        return self.settings["sec_edgar"]["base_url"]

    def test_access(self) -> tuple[str, Optional[str]]:
        url = f"{self._base()}/submissions/CIK{_CANARY_CIK}.json"
        try:
            r = self._session().get(url, timeout=self.settings["http"]["timeout"])
            if r.status_code == 200:
                return "ok", None
            if r.status_code == 401:
                return "auth_fail", f"HTTP {r.status_code}"
            return "error", f"HTTP {r.status_code}"
        except Exception as exc:
            return "error", str(exc)

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[dict]:
        session = self._session()
        results = {}
        for t in tickers:
            cik = t.get("cik")
            if not cik:
                continue
            padded = cik.zfill(10)
            if use_case == "filings":
                url = f"{self._base()}/submissions/CIK{padded}.json"
            else:
                url = f"{self._base()}/api/xbrl/companyfacts/CIK{padded}.json"
            try:
                self._delay()
                r = session.get(url, timeout=self.settings["http"]["timeout"])
                if r.status_code == 200:
                    results[t["ticker"]] = r.json()
                else:
                    logger.warning("SEC %s %s: HTTP %s", use_case, t["ticker"], r.status_code)
            except Exception as exc:
                logger.error("SEC %s %s: %s", use_case, t["ticker"], exc)
        return results if results else None

    def validate_schema(self, data: Optional[dict], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        if use_case == "filings":
            for ticker, payload in data.items():
                recent = payload.get("filings", {}).get("recent", {})
                for field in ("form", "accessionNumber", "filingDate"):
                    if field not in recent:
                        key = f"filings.recent.{field}"
                        if key not in missing:
                            missing.append(key)
        else:
            for ticker, payload in data.items():
                facts = payload.get("facts", {})
                gaap = facts.get("us-gaap", {})
                dei = facts.get("dei", {})
                if "NetIncomeLoss" not in gaap:
                    missing.append("us-gaap.NetIncomeLoss")
                if "Revenues" not in gaap and "RevenueFromContractWithCustomerExcludingAssessedTax" not in gaap:
                    missing.append("us-gaap.Revenues")
                if "EntityCommonStockSharesOutstanding" not in dei:
                    missing.append("dei.EntityCommonStockSharesOutstanding")
                break   # check one ticker is enough for schema
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[dict], use_case: str) -> str:
        if not data:
            return "N/A"
        if use_case == "filings":
            most_recent = date.min
            for payload in data.values():
                dates = payload.get("filings", {}).get("recent", {}).get("filingDate", [])
                for d in dates:
                    try:
                        parsed = datetime.strptime(d, "%Y-%m-%d").date()
                        if parsed > most_recent:
                            most_recent = parsed
                    except ValueError:
                        pass
            if most_recent == date.min:
                return "N/A"
            age = (date.today() - most_recent).days
            if age <= 1:
                return "same-day"
            if age <= 7:
                return "1d"
            if age <= 30:
                return "1w"
            return ">1mo"
        return "regulatory"   # fundamentals are as-filed

    def evaluate_history(self, data: Optional[dict], use_case: str) -> str:
        if not data:
            return "N/A"
        if use_case == "fundamentals":
            for payload in data.values():
                facts = payload.get("facts", {}).get("us-gaap", {})
                if "NetIncomeLoss" in facts:
                    units = facts["NetIncomeLoss"].get("units", {}).get("USD", [])
                    if units:
                        oldest = min(u["end"] for u in units if "end" in u)
                        try:
                            years = (date.today() - datetime.strptime(oldest, "%Y-%m-%d").date()).days // 365
                            return f"{years}y"
                        except ValueError:
                            pass
            return "N/A"
        return "all_available"   # EDGAR has all historical filings

    def summarize_result(self, data: Optional[dict], use_case: str, access_result: str) -> ValidationResult:
        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        freshness = self.evaluate_freshness(data, use_case) if data else "N/A"
        history = self.evaluate_history(data, use_case) if data else "N/A"

        if access_result == "ok":
            scores = Scores(
                access=5,
                completeness=5 if fields_ok else 2,
                freshness=4,          # filings delay ~1 business day after event
                reliability=5,
                parsing_ease=4,       # clean JSON but nested structure
                cost_efficiency=5,    # free
                strategic_value=5,
            )
        else:
            scores = Scores(1, 1, 1, 1, 1, 5, 5)

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=[t["ticker"] for t in self.tickers_config if t.get("cik")],
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth=history,
            freshness=freshness,
            parsing_difficulty="easy",
            rate_limit_notes="No auth required. Recommend <=10 req/sec (use 0.12s delay).",
            fallback_suggestion="No fallback needed; EDGAR is authoritative.",
            final_status=determine_status(scores),
            notes="CIK-indexed. ETFs (IWM, XLE) have no fundamental filings.",
            scores=scores,
            raw_sample_path=None,
        )
