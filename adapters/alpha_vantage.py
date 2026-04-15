from __future__ import annotations

import time
import logging
from typing import Any, Optional

import requests

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.alpha_vantage")

_TRANSCRIPT_TICKERS = ["AAPL", "NVDA", "TSLA"]
_TRANSCRIPT_QUARTERS = ["2024Q3", "2024Q2"]


class AlphaVantageAdapter(BaseAdapter):
    source_name = "alpha_vantage"
    use_cases = ["transcripts", "estimates"]

    def _api_key(self) -> str:
        return self.settings.get("alpha_vantage", {}).get("api_key", "demo")

    def _base(self) -> str:
        return self.settings["alpha_vantage"]["base_url"]

    def _delay(self):
        d = self.settings["alpha_vantage"].get("rate_limit_delay", 0)
        if d:
            time.sleep(d)

    def _get(self, params: dict) -> requests.Response:
        params["apikey"] = self._api_key()
        return requests.get(
            f"{self._base()}/query", params=params,
            timeout=self.settings["http"]["timeout"]
        )

    def _is_rate_limit_response(self, data: dict) -> bool:
        return "Information" in data or "Note" in data

    def test_access(self) -> tuple[str, Optional[str]]:
        try:
            r = self._get({"function": "SYMBOL_SEARCH", "keywords": "AAPL"})
            if r.status_code != 200:
                return "error", f"HTTP {r.status_code}"
            data = r.json()
            if self._is_rate_limit_response(data):
                return "rate_limited", data.get("Information", data.get("Note", ""))
            if "bestMatches" in data:
                return "ok", None
            return "error", f"Unexpected response keys: {list(data.keys())}"
        except Exception as exc:
            return "error", str(exc)

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[dict]:
        results: dict = {}
        if use_case == "transcripts":
            targets = [t for t in tickers if t["ticker"] in _TRANSCRIPT_TICKERS]
            for t in targets:
                ticker = t["ticker"]
                results[ticker] = []
                for quarter in _TRANSCRIPT_QUARTERS:
                    self._delay()
                    try:
                        r = self._get({"function": "EARNINGS_CALL_TRANSCRIPT",
                                       "symbol": ticker, "quarter": quarter})
                        if r.status_code == 200:
                            data = r.json()
                            if not self._is_rate_limit_response(data):
                                results[ticker].append(data)
                            else:
                                logger.warning("Rate limit hit for %s/%s", ticker, quarter)
                    except Exception as exc:
                        logger.error("AV transcript %s/%s: %s", ticker, quarter, exc)
        else:
            for t in tickers:
                ticker = t["ticker"]
                if t.get("type") == "etf":
                    continue
                self._delay()
                try:
                    r = self._get({"function": "EARNINGS", "symbol": ticker})
                    if r.status_code == 200:
                        data = r.json()
                        if not self._is_rate_limit_response(data):
                            results[ticker] = data
                        else:
                            logger.warning("Rate limit hit for EARNINGS %s", ticker)
                except Exception as exc:
                    logger.error("AV estimates %s: %s", ticker, exc)
        return results if results else None

    def validate_schema(self, data: Optional[dict], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        if use_case == "transcripts":
            for ticker, quarters in data.items():
                if not quarters:
                    missing.append(f"{ticker}.no_quarters")
                    continue
                q = quarters[0]
                for field in ("symbol", "quarter", "transcript"):
                    if field not in q:
                        missing.append(field)
                break
        else:
            for ticker, payload in data.items():
                for field in ("symbol", "annualEarnings", "quarterlyEarnings"):
                    if field not in payload:
                        missing.append(field)
                qe = payload.get("quarterlyEarnings", [])
                if qe:
                    q = qe[0]
                    if "estimatedEPS" not in q:
                        missing.append("quarterlyEarnings[].estimatedEPS")
                    if "reportedEPS" not in q:
                        missing.append("quarterlyEarnings[].reportedEPS")
                break
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[dict], use_case: str) -> str:
        if not data:
            return "N/A"
        if use_case == "transcripts":
            return "1w"    # transcripts typically available within a week of earnings
        return "1d"        # EPS estimates updated frequently

    def evaluate_history(self, data: Optional[dict], use_case: str) -> str:
        if not data:
            return "N/A"
        if use_case == "transcripts":
            return "2y"    # Alpha Vantage transcript coverage ~2 years
        if use_case == "estimates":
            for ticker, payload in data.items():
                annual = payload.get("annualEarnings", [])
                if annual:
                    return f"{len(annual)}y"
            return "N/A"
        return "N/A"

    def summarize_result(self, data: Optional[dict], use_case: str, access_result: str) -> ValidationResult:
        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        freshness = self.evaluate_freshness(data, use_case)
        history = self.evaluate_history(data, use_case)

        covered = sum(1 for v in (data or {}).values() if v) if data else 0
        coverage_note = f"{covered} tickers with data."

        if access_result == "ok":
            scores = Scores(
                access=3,            # free tier rate-limits aggressively
                completeness=3 if fields_ok else 1,
                freshness=3,
                reliability=3,
                parsing_ease=4,
                cost_efficiency=3,   # free tier very limited; premium needed
                strategic_value=4 if use_case == "transcripts" else 3,
            )
        else:
            scores = Scores(1, 1, 1, 1, 4, 3, 4)

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=[t["ticker"] for t in self.tickers_config
                            if t.get("type") != "etf" or use_case != "transcripts"],
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth=history,
            freshness=freshness,
            parsing_difficulty="easy",
            rate_limit_notes="Free: 25 req/day or 5/min. Premium needed for production.",
            fallback_suggestion="Earnings Whispers or Motley Fool for transcripts; "
                                "Consensus from Bloomberg for estimates.",
            final_status=determine_status(scores),
            notes=f"{coverage_note} ETFs excluded from transcripts/estimates.",
            scores=scores,
            raw_sample_path=None,
        )
