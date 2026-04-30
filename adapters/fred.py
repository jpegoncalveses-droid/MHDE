from __future__ import annotations

import time
import logging
from datetime import date
from typing import Any, Optional

import requests

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.fred")

_FRED_SERIES = ["FEDFUNDS", "DGS10", "CPIAUCSL", "UNRATE", "PAYEMS", "GDP"]

_SERIES_FREQ: dict[str, str] = {
    "FEDFUNDS": "monthly",
    "DGS10":    "daily",
    "CPIAUCSL": "monthly",
    "UNRATE":   "monthly",
    "PAYEMS":   "monthly",
    "GDP":      "quarterly",
}

_FREQ_TOLERANCE_DAYS = {"daily": 7, "monthly": 60, "quarterly": 150}
_FRESHNESS_LABEL = {"daily": "1d", "monthly": "1mo", "quarterly": "1q"}

_CORE_COVERAGE_MIN = 5  # of 6 series must have usable data


class FREDAdapter(BaseAdapter):
    source_name = "fred"
    use_cases = ["macro_series", "release_calendar"]

    def _api_key(self) -> str:
        return self.settings.get("fred", {}).get("api_key", "")

    def _base(self) -> str:
        return self.settings["fred"]["base_url"]

    def _delay(self):
        d = self.settings["fred"].get("rate_limit_delay", 0)
        if d:
            time.sleep(d)

    def _get(self, path: str, params: dict = None) -> requests.Response:
        p = dict(params or {})
        p["api_key"] = self._api_key()
        p["file_type"] = "json"
        return requests.get(
            f"{self._base()}{path}", params=p,
            timeout=self.settings["http"]["timeout"],
        )

    def test_access(self) -> tuple[str, Optional[str]]:
        if not self._api_key():
            return "auth_fail", "No API key configured (set FRED_API_KEY)"
        try:
            r = self._get("/series/observations", {"series_id": "FEDFUNDS", "limit": "1"})
            if r.status_code == 200:
                return "ok", None
            if r.status_code in (401, 403):
                return "auth_fail", f"HTTP {r.status_code}"
            if r.status_code == 429:
                return "rate_limited", "HTTP 429"
            if r.status_code == 400:
                try:
                    msg = r.json().get("error_message", "")
                    if "api_key" in msg.lower() or "bad api key" in msg.lower():
                        return "auth_fail", msg
                except Exception:
                    pass
                return "error", "HTTP 400"
            return "error", f"HTTP {r.status_code}"
        except Exception as exc:
            return "error", str(exc)

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[Any]:
        if use_case == "macro_series":
            return self._fetch_macro()
        return self._fetch_releases()

    def _fetch_macro(self) -> Optional[dict]:
        results: dict = {}
        for series_id in _FRED_SERIES:
            self._delay()
            try:
                r = self._get("/series/observations", {
                    "series_id": series_id,
                    "sort_order": "desc",
                    "limit": "10",
                })
                if r.status_code == 200:
                    results[series_id] = r.json()
                else:
                    logger.warning("FRED macro %s: HTTP %s", series_id, r.status_code)
            except Exception as exc:
                logger.error("FRED macro %s: %s", series_id, exc)
        return results if results else None

    def _fetch_releases(self) -> Optional[dict]:
        results: dict = {}
        today = date.today().isoformat()
        for series_id in _FRED_SERIES:
            self._delay()
            try:
                r = self._get("/series/release", {"series_id": series_id})
                if r.status_code != 200:
                    logger.warning("FRED series/release %s: HTTP %s", series_id, r.status_code)
                    continue
                releases = r.json().get("releases", [])
                if not releases:
                    logger.warning("FRED series/release %s: empty releases list", series_id)
                    continue
                release_id = releases[0]["id"]
                release_name = releases[0]["name"]
            except Exception as exc:
                logger.error("FRED series/release %s: %s", series_id, exc)
                continue

            # Try upcoming dates
            self._delay()
            source = "upcoming"
            dates: list[str] = []
            try:
                r = self._get("/release/dates", {
                    "release_id": str(release_id),
                    "include_release_dates_with_no_data": "true",
                    "realtime_start": today,
                    "sort_order": "asc",
                    "limit": "5",
                })
                if r.status_code == 200:
                    dates = [d["date"] for d in r.json().get("release_dates", [])]
            except Exception as exc:
                logger.warning("FRED upcoming dates %s: %s", series_id, exc)

            # Fallback to recent dates
            if not dates:
                source = "recent"
                self._delay()
                try:
                    r = self._get("/release/dates", {
                        "release_id": str(release_id),
                        "include_release_dates_with_no_data": "true",
                        "sort_order": "desc",
                        "limit": "5",
                    })
                    if r.status_code == 200:
                        dates = [d["date"] for d in r.json().get("release_dates", [])]
                except Exception as exc:
                    logger.warning("FRED recent dates %s: %s", series_id, exc)

            if not dates:
                source = "missing_dates"

            results[series_id] = {
                "series_id": series_id,
                "release_id": release_id,
                "release_name": release_name,
                "dates": dates,
                "source": source,
            }

        return results if results else None

    def validate_schema(self, data: Optional[Any], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        if use_case == "macro_series":
            for series_id, payload in data.items():
                obs = payload.get("observations", [])
                if not obs:
                    missing.append(f"{series_id}.observations_empty")
                    continue
                sample = obs[0]
                for field in ("date", "value"):
                    if field not in sample:
                        missing.append(f"{series_id}.obs[].{field}")
        else:
            for series_id, record in data.items():
                for field in ("release_id", "release_name"):
                    if not record.get(field):
                        missing.append(f"{series_id}.{field}")
                if not record.get("dates"):
                    missing.append(f"{series_id}.dates_empty")
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[Any], use_case: str) -> str:
        if not data:
            return "N/A"
        if use_case == "release_calendar":
            sources = [r.get("source", "missing_dates") for r in data.values()]
            if "missing_dates" in sources:
                return "missing_dates"
            if any(s == "recent" for s in sources):
                return "recent_fallback"
            return "upcoming_available"

        today = date.today()
        worst_lag = 0
        worst_series: Optional[str] = None
        for series_id, payload in data.items():
            obs = payload.get("observations", [])
            if not obs:
                continue
            date_str = obs[0].get("date", "")
            if not date_str:
                continue
            try:
                lag = (today - date.fromisoformat(date_str)).days
                if lag > worst_lag:
                    worst_lag = lag
                    worst_series = series_id
            except ValueError:
                continue

        if worst_series is None:
            return "N/A"
        freq = _SERIES_FREQ.get(worst_series, "monthly")
        if worst_lag <= _FREQ_TOLERANCE_DAYS[freq]:
            return _FRESHNESS_LABEL[freq]
        return f"stale:{worst_series}:{worst_lag}d"

    def evaluate_history(self, data: Optional[Any], use_case: str) -> str:
        if not data or use_case == "release_calendar":
            return "N/A"
        return "10obs"

    def summarize_result(self, data: Optional[Any], use_case: str, access_result: str) -> ValidationResult:
        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        freshness = self.evaluate_freshness(data, use_case)
        history = self.evaluate_history(data, use_case)

        coverage = 0
        if data:
            if use_case == "macro_series":
                coverage = sum(1 for v in data.values() if v.get("observations"))
            else:
                coverage = sum(1 for v in data.values() if v.get("dates"))

        notes_parts: list[str] = []
        if use_case == "release_calendar" and data:
            fallback = [s for s, r in data.items() if r.get("source") == "recent"]
            missing_d = [s for s, r in data.items() if r.get("source") == "missing_dates"]
            if fallback:
                notes_parts.append(f"Recent fallback used for: {', '.join(fallback)}.")
            if missing_d:
                notes_parts.append(f"No dates found for: {', '.join(missing_d)}.")
        notes = " ".join(notes_parts) or "Free public API. No meaningful rate limits for validation."

        if access_result == "ok" and coverage > 0:
            scores = Scores(
                access=4,
                completeness=5 if fields_ok else 2,
                freshness=4,
                reliability=5,
                parsing_ease=5,
                cost_efficiency=5,
                strategic_value=5,
            )
        elif access_result == "auth_fail":
            scores = Scores(1, 1, 1, 1, 5, 5, 5)
        else:
            scores = Scores(2, 1, 1, 2, 5, 5, 5)

        final_status = determine_status(scores)

        # Hard overrides
        if access_result == "auth_fail" or not self._api_key():
            final_status = "Reject for v1"
        elif coverage == 0:
            if final_status in ("Core", "Useful but optional"):
                final_status = "Fallback only"
        elif not fields_ok or coverage < _CORE_COVERAGE_MIN:
            if final_status == "Core":
                final_status = "Useful but optional"

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=_FRED_SERIES,
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth=history,
            freshness=freshness,
            parsing_difficulty="easy",
            rate_limit_notes="No published rate limit; 0.5s delay between calls as courtesy.",
            fallback_suggestion="World Bank API for some macro series; no equivalent for US release calendar.",
            final_status=final_status,
            notes=notes,
            scores=scores,
            raw_sample_path=None,
        )
