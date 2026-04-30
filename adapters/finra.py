from __future__ import annotations

import csv
import io
import logging
import time
from calendar import monthrange
from datetime import date
from typing import Any, Optional

import requests

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.finra")

_FINRA_BASKET = ["AAPL", "NVDA", "TSLA", "JPM", "UBER", "RKLB"]
_HISTORY_PERIODS = 4
_FRESHNESS_CURRENT_DAYS = 25
_FRESHNESS_LATE_DAYS = 40
_CORE_COVERAGE = 6
_USEFUL_COVERAGE_MIN = 4

_REQUIRED_FIELDS = [
    "symbolCode",
    "currentShortPositionQuantity",
    "previousShortPositionQuantity",
    "settlementDate",
    "averageDailyVolumeQuantity",
]


class FINRAAdapter(BaseAdapter):
    source_name = "finra"
    use_cases = ["short_interest", "short_interest_history"]

    def _base(self) -> str:
        return self.settings["finra"]["base_url"]

    def _delay(self):
        d = self.settings["finra"].get("rate_limit_delay", 0)
        if d:
            time.sleep(d)

    def _probe_url(self, url: str) -> Optional[int]:
        timeout = self.settings["http"]["timeout"]
        try:
            r = requests.head(url, timeout=timeout)
            if r.status_code == 405:
                r2 = requests.get(url, stream=True, timeout=timeout)
                r2.close()
                return r2.status_code
            return r.status_code
        except Exception:
            return None

    def _candidate_dates(self, n_max: int = 10) -> list[date]:
        today = date.today()
        year, month = today.year, today.month
        candidates: list[date] = []
        for _ in range(6):
            last_day = monthrange(year, month)[1]
            end_of_month = date(year, month, last_day)
            mid_of_month = date(year, month, 15)
            if end_of_month <= today:
                candidates.append(end_of_month)
            if mid_of_month <= today:
                candidates.append(mid_of_month)
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        candidates.sort(reverse=True)
        return candidates[:n_max]

    def _find_urls(self, n: int = 1) -> tuple[list[str], Optional[str]]:
        found: list[str] = []
        for candidate in self._candidate_dates():
            url = f"{self._base()}/shrt{candidate.strftime('%Y%m%d')}.csv"
            self._delay()
            status = self._probe_url(url)
            if status is None:
                continue
            if status == 401:
                return [], "auth_fail"
            if status == 200:
                found.append(url)
                if len(found) >= n:
                    break
        return found, None

    def _download_csv(self, url: str) -> str:
        r = requests.get(url, timeout=self.settings["http"]["timeout"])
        r.raise_for_status()
        return r.content.decode("latin-1")

    def _parse_csv(self, text: str, basket: list[str]) -> tuple[list[dict], set, set, Optional[str]]:
        basket_set = set(basket)
        rows: list[dict] = []
        found: set[str] = set()
        reader = csv.DictReader(io.StringIO(text), delimiter="|")
        for row in reader:
            symbol = row.get("symbolCode", "").strip()
            if symbol in basket_set:
                rows.append({k: v.strip() for k, v in row.items()})
                found.add(symbol)
        settlement_date = rows[0]["settlementDate"].strip() if rows else None
        return rows, found, basket_set - found, settlement_date

    def test_access(self) -> tuple[str, Optional[str]]:
        found, auth_error = self._find_urls(n=1)
        if auth_error:
            return "auth_fail", f"HTTP auth error accessing FINRA CDN"
        if not found:
            return "no_available_files", "No FINRA bi-weekly files found in recent candidates"
        return "ok", None

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[Any]:
        n = 1 if use_case == "short_interest" else _HISTORY_PERIODS
        found_urls, auth_error = self._find_urls(n=n)
        if auth_error or not found_urls:
            return None

        all_rows: list[dict] = []
        settlement_dates: list[str] = []
        found_all: set[str] = set()

        for url in found_urls:
            self._delay()
            try:
                text = self._download_csv(url)
                rows, found, _missing, sd = self._parse_csv(text, _FINRA_BASKET)
                all_rows.extend(rows)
                found_all.update(found)
                if sd:
                    settlement_dates.append(sd)
            except Exception as exc:
                logger.warning("FINRA download error %s: %s", url, exc)

        if not all_rows and not settlement_dates:
            return None

        most_recent = max(settlement_dates) if settlement_dates else None
        return {
            "rows": all_rows,
            "found_symbols": found_all,
            "missing_symbols": set(_FINRA_BASKET) - found_all,
            "settlement_date": most_recent,
            "settlement_dates": settlement_dates,
            "periods_found": len(settlement_dates),
        }

    def validate_schema(self, data: Optional[Any], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        for row in data.get("rows", []):
            symbol = row.get("symbolCode", "?")
            for field in _REQUIRED_FIELDS:
                if not row.get(field):
                    missing.append(f"{symbol}.{field}")
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[Any], use_case: str) -> str:
        if not data:
            return "N/A"
        sd = data.get("settlement_date")
        if not sd:
            return "N/A"
        try:
            lag = (date.today() - date.fromisoformat(sd)).days
        except ValueError:
            return "N/A"
        if lag <= _FRESHNESS_CURRENT_DAYS:
            return "biweekly_current"
        if lag <= _FRESHNESS_LATE_DAYS:
            return "one_cycle_late"
        return f"stale:{lag}d"

    def evaluate_history(self, data: Optional[Any], use_case: str) -> str:
        if not data:
            return "N/A"
        periods = data.get("periods_found", 0)
        if periods == 0:
            return "Np"
        return f"{periods}p"

    def summarize_result(
        self, data: Optional[Any], use_case: str, access_result: str
    ) -> ValidationResult:
        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        freshness = self.evaluate_freshness(data, use_case)
        history = self.evaluate_history(data, use_case)
        coverage = len(data["found_symbols"]) if data else 0

        if access_result in ("auth_fail",):
            scores = Scores(1, 1, 1, 1, 3, 5, 4)
        elif not data or coverage == 0:
            scores = Scores(2, 1, 1, 2, 3, 5, 4)
        elif coverage >= _CORE_COVERAGE and fields_ok:
            scores = Scores(4, 5, 4, 4, 3, 5, 4)
        elif coverage >= _USEFUL_COVERAGE_MIN:
            scores = Scores(4, 3, 4, 4, 3, 5, 4)
        else:
            scores = Scores(4, 2, 4, 4, 3, 5, 4)

        final_status = determine_status(scores)

        if access_result == "auth_fail":
            final_status = "Reject for v1"
        elif not data or coverage == 0:
            if final_status in ("Core", "Useful but optional"):
                final_status = "Fallback only"
        elif 1 <= coverage <= 3:
            if final_status in ("Core", "Useful but optional"):
                final_status = "Fallback only"

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=_FINRA_BASKET,
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth=history,
            freshness=freshness,
            parsing_difficulty="moderate",
            rate_limit_notes="No published rate limit; 1.0s delay between CDN requests as courtesy.",
            fallback_suggestion="No direct public equivalent for US bi-weekly short interest.",
            final_status=final_status,
            notes=(
                f"Coverage: {coverage}/{len(_FINRA_BASKET)} basket symbols found. "
                f"Free public CDN, no auth required."
            ),
            scores=scores,
            raw_sample_path=None,
        )
