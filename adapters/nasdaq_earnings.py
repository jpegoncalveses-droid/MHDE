from __future__ import annotations

import time
import logging
from datetime import date, timedelta
from typing import Any, Optional

import requests

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.nasdaq_earnings")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MHDE-Validation/1.0)",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


class NasdaqEarningsAdapter(BaseAdapter):
    source_name = "nasdaq_earnings"
    use_cases = ["earnings_calendar"]

    def _base(self) -> str:
        return self.settings["nasdaq_earnings"]["base_url"]

    def _delay(self):
        d = self.settings["nasdaq_earnings"].get("rate_limit_delay", 0)
        if d:
            time.sleep(d)

    def _fetch_for_date(self, query_date: str) -> Optional[dict]:
        url = f"{self._base()}/api/calendar/earnings"
        try:
            r = requests.get(url, params={"date": query_date}, headers=_HEADERS,
                             timeout=self.settings["http"]["timeout"])
            if r.status_code == 200:
                return r.json()
            logger.warning("Nasdaq earnings %s: HTTP %s", query_date, r.status_code)
        except Exception as exc:
            logger.error("Nasdaq earnings %s: %s", query_date, exc)
        return None

    def test_access(self) -> tuple[str, Optional[str]]:
        today = date.today()
        last_monday = today - timedelta(days=today.weekday())
        result = self._fetch_for_date(last_monday.isoformat())
        if result and result.get("status", {}).get("rCode") == 200:
            return "ok", None
        if result is None:
            return "error", "No response"
        return "error", f"Unexpected status: {result.get('status')}"

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[dict]:
        target_tickers = {t["ticker"] for t in tickers}
        found: dict[str, dict] = {}
        today = date.today()
        search_start = today - timedelta(days=30)
        search_end = today + timedelta(days=60)
        current = search_start
        while current <= search_end and len(found) < len(target_tickers):
            self._delay()
            payload = self._fetch_for_date(current.isoformat())
            if payload:
                rows = payload.get("data", {}).get("rows", []) or []
                for row in rows:
                    sym = row.get("symbol", "").upper()
                    if sym in target_tickers and sym not in found:
                        found[sym] = {
                            "date": row.get("date"),
                            "time": row.get("time"),
                            "eps_forecast": row.get("eps_forecast"),
                            "eps_prior": row.get("eps_prior"),
                            "name": row.get("name"),
                        }
            current += timedelta(days=7)
        return found if found else None

    def validate_schema(self, data: Optional[dict], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        for ticker, entry in data.items():
            if "date" not in entry or not entry["date"]:
                missing.append("date")
            if "time" not in entry:
                missing.append("time")
            break
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[dict], use_case: str) -> str:
        return "1d"   # calendar updated daily

    def evaluate_history(self, data: Optional[dict], use_case: str) -> str:
        return "N/A"   # planning-only source; no historical coverage needed

    def summarize_result(self, data: Optional[dict], use_case: str, access_result: str) -> ValidationResult:
        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        found_tickers = list(data.keys()) if data else []
        basket_tickers = [t["ticker"] for t in self.tickers_config]
        coverage = f"{len(found_tickers)}/{len(basket_tickers)} tickers found."

        if access_result == "ok":
            scores = Scores(
                access=3,
                completeness=3 if fields_ok else 1,
                freshness=4,
                reliability=3,
                parsing_ease=4,
                cost_efficiency=5,
                strategic_value=3,
            )
        else:
            scores = Scores(1, 1, 1, 1, 4, 5, 3)

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=basket_tickers,
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth="N/A",
            freshness="1d",
            parsing_difficulty="easy",
            rate_limit_notes="Unofficial API. No auth but may block scrapers.",
            fallback_suggestion="Earnings Whispers API or Yahoo Finance earnings calendar.",
            final_status=determine_status(scores),
            notes=f"PLANNING ONLY — do not use as truth source. {coverage}",
            scores=scores,
            raw_sample_path=None,
        )
