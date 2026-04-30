from __future__ import annotations

import time
import logging
from datetime import date, timedelta
from typing import Any, Optional

import requests

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.polygon")

_HISTORY_FROM = "2020-01-01"
_REQUIRED_AGG_FIELDS = ["t", "o", "h", "l", "c", "v"]
_REQUIRED_SNAP_FIELDS = ["day.o", "day.h", "day.l", "day.c", "day.v", "prevDay.c"]


class _PlanLimited:
    """Sentinel returned by fetch_sample_data when an endpoint responds 403 plan-limited."""


_PLAN_LIMITED = _PlanLimited()


class PolygonAdapter(BaseAdapter):
    source_name = "polygon"
    use_cases = ["recent_daily_prices", "deep_historical_prices", "recent_snapshot"]

    def _api_key(self) -> str:
        return self.settings.get("polygon", {}).get("api_key", "")

    def _base(self) -> str:
        return self.settings["polygon"]["base_url"]

    def _delay(self):
        d = self.settings["polygon"].get("rate_limit_delay", 0)
        if d:
            time.sleep(d)

    def _get(self, url: str, params: dict = None) -> requests.Response:
        params = params or {}
        params["apiKey"] = self._api_key()
        return requests.get(url, params=params, timeout=self.settings["http"]["timeout"])

    def test_access(self) -> tuple[str, Optional[str]]:
        today = date.today()
        from_date = (today - timedelta(days=5)).isoformat()
        to_date = (today - timedelta(days=3)).isoformat()
        url = f"{self._base()}/v2/aggs/ticker/AAPL/range/1/day/{from_date}/{to_date}"
        try:
            r = self._get(url)
            if r.status_code == 200:
                return "ok", None
            if r.status_code == 401:
                return "auth_fail", f"HTTP {r.status_code}"
            if r.status_code == 403:
                return "plan_limited", f"HTTP {r.status_code}"
            if r.status_code == 429:
                return "rate_limited", "HTTP 429"
            return "error", f"HTTP {r.status_code}"
        except Exception as exc:
            return "error", str(exc)

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[Any]:
        results = {}
        today = date.today()
        for t in tickers:
            ticker = t["ticker"]
            self._delay()
            try:
                if use_case == "recent_daily_prices":
                    from_date = (today - timedelta(days=5)).isoformat()
                    url = f"{self._base()}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{today.isoformat()}"
                    r = self._get(url, {"adjusted": "true", "sort": "asc"})
                elif use_case == "deep_historical_prices":
                    url = f"{self._base()}/v2/aggs/ticker/{ticker}/range/1/day/{_HISTORY_FROM}/{today.isoformat()}"
                    r = self._get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
                else:
                    url = f"{self._base()}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
                    r = self._get(url)

                if r.status_code == 200:
                    results[ticker] = r.json()
                elif r.status_code == 403:
                    logger.warning("Polygon %s %s: HTTP 403 plan-limited", use_case, ticker)
                    return _PLAN_LIMITED
                else:
                    logger.warning("Polygon %s %s: HTTP %s", use_case, ticker, r.status_code)
            except Exception as exc:
                logger.error("Polygon %s %s: %s", use_case, ticker, exc)
        return results if results else None

    def validate_schema(self, data: Optional[Any], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        if use_case in ("recent_daily_prices", "deep_historical_prices"):
            for ticker, payload in data.items():
                results = payload.get("results", [])
                if not results:
                    missing.append(f"{ticker}.results_empty")
                    continue
                sample = results[0]
                for field in _REQUIRED_AGG_FIELDS:
                    if field not in sample:
                        missing.append(f"results[].{field}")
                break
        else:
            for ticker, payload in data.items():
                ticker_data = payload.get("ticker", {})
                day = ticker_data.get("day", {})
                prev = ticker_data.get("prevDay", {})
                for field in ["o", "h", "l", "c", "v"]:
                    if field not in day:
                        missing.append(f"ticker.day.{field}")
                if "c" not in prev:
                    missing.append("ticker.prevDay.c")
                break
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[Any], use_case: str) -> str:
        if not data:
            return "N/A"
        if use_case == "recent_snapshot":
            return "same-day"
        return "1d"

    def evaluate_history(self, data: Optional[Any], use_case: str) -> str:
        if not data:
            return "N/A"
        if use_case == "recent_snapshot":
            return "N/A"
        if use_case == "recent_daily_prices":
            return "5d"
        return "5y"

    def summarize_result(self, data: Optional[Any], use_case: str, access_result: str) -> ValidationResult:
        if data is _PLAN_LIMITED:
            access_result = "plan_limited"
            data = None

        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        freshness = self.evaluate_freshness(data, use_case)
        history = self.evaluate_history(data, use_case)

        if access_result == "ok":
            if data is None:
                # Key is valid but fetch returned nothing (rate-limited, network, etc.) —
                # can't confirm the use case works; score conservatively.
                scores = Scores(2, 1, 1, 2, 5, 3, 3)  # total 17 → Fallback only
            elif use_case == "recent_daily_prices":
                scores = Scores(
                    access=3,
                    completeness=5 if fields_ok else 2,
                    freshness=4,
                    reliability=4,
                    parsing_ease=5,
                    cost_efficiency=3,
                    strategic_value=3,
                )  # total 27 → Useful but optional
            else:
                scores = Scores(
                    access=4,
                    completeness=5 if fields_ok else 2,
                    freshness=5 if use_case == "recent_snapshot" else 4,
                    reliability=4,
                    parsing_ease=5,
                    cost_efficiency=3,
                    strategic_value=5,
                )
        elif access_result == "plan_limited":
            scores = Scores(2, 1, 1, 1, 5, 1, 2)  # total 13 → Reject for v1
        elif access_result == "auth_fail":
            scores = Scores(1, 1, 1, 1, 5, 3, 5)
        else:
            scores = Scores(2, 1, 1, 2, 5, 3, 5)

        notes_map = {
            "recent_daily_prices": "Last 5 days OHLCV available on free tier. Good for recent price context.",
            "deep_historical_prices": "Free tier 403: requires paid plan for data older than 2 years.",
            "recent_snapshot": "Free tier 403: intraday snapshot requires paid plan.",
        }

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=[t["ticker"] for t in self.tickers_config],
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth=history,
            freshness=freshness,
            parsing_difficulty="easy",
            rate_limit_notes="Free tier: 5 calls/min. Paid plans remove this limit.",
            fallback_suggestion="yfinance for historical; no good snapshot fallback.",
            final_status=determine_status(scores),
            notes=notes_map.get(use_case, ""),
            scores=scores,
            raw_sample_path=None,
        )
