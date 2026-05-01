from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Optional

import requests

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.cftc")

_TFF_DEFAULT = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_DISAG_DEFAULT = "https://publicreporting.cftc.gov/resource/kh3c-gbw2.json"

# Keyword → market_key mapping; substring match against market_and_exchange_names (uppercased)
_INDEX_TARGETS: dict[str, str] = {
    "E-MINI S&P 500": "es_sp500",
    "NASDAQ MINI": "nq_nasdaq100",          # actual CFTC TFF name: "NASDAQ MINI - CME"
    "RUSSELL E-MINI": "rty_russell2000",    # actual CFTC TFF name: "RUSSELL E-MINI - CME"
    "10-YEAR U.S. TREASURY NOTES": "ust_10y",
}

_COMMODITY_TARGETS: dict[str, str] = {
    "CRUDE OIL, LIGHT SWEET": "wti_crude",
    "GOLD - COMMODITY EXCHANGE": "gold",
}

# Core markets used for coverage scoring (excludes optional treasury)
_CORE_MARKETS: dict[str, list[str]] = {
    "index_positioning": ["es_sp500", "nq_nasdaq100", "rty_russell2000"],
    "commodity_macro_positioning": ["wti_crude", "gold"],
}

_FRESHNESS_CURRENT_DAYS = 10
_FRESHNESS_LATE_DAYS = 17


def _safe_int(row: dict, *fields: str) -> int:
    for field in fields:
        val = row.get(field)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                continue
    return 0


class CFTCAdapter(BaseAdapter):
    source_name = "cftc"
    use_cases = ["index_positioning", "commodity_macro_positioning"]

    def _tff_url(self) -> str:
        return self.settings["cftc"].get("tff_url", _TFF_DEFAULT)

    def _disag_url(self) -> str:
        return self.settings["cftc"].get("disag_url", _DISAG_DEFAULT)

    def _delay(self):
        d = self.settings["cftc"].get("rate_limit_delay", 0)
        if d:
            time.sleep(d)

    def _get(self, url: str, params: dict) -> requests.Response:
        return requests.get(url, params=params, timeout=self.settings["http"]["timeout"])

    def test_access(self) -> tuple[str, Optional[str]]:
        try:
            r = self._get(
                self._tff_url(),
                {"$limit": "1", "$order": "report_date_as_yyyy_mm_dd DESC"},
            )
            if r.status_code == 200:
                return "ok", None
            return "error", f"HTTP {r.status_code}"
        except Exception as exc:
            return "error", str(exc)

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[Any]:
        is_index = use_case == "index_positioning"
        endpoint = self._tff_url() if is_index else self._disag_url()
        targets = _INDEX_TARGETS if is_index else _COMMODITY_TARGETS
        extract_fn = self._extract_tff_categories if is_index else self._extract_disag_categories

        weeks = self.settings["cftc"].get("history_weeks", 4)
        self._delay()
        try:
            r = self._get(endpoint, {
                "$limit": str(weeks * 100),
                "$order": "report_date_as_yyyy_mm_dd DESC",
            })
            if r.status_code != 200:
                logger.warning("CFTC %s: HTTP %s", use_case, r.status_code)
                return None
            raw = r.json()
        except Exception as exc:
            logger.error("CFTC %s fetch error: %s", use_case, exc)
            return None

        if not raw:
            return None

        records: list[dict] = []
        found: set[str] = set()

        for row in raw:
            market_name = row.get("market_and_exchange_names", "")
            key = self._match_market(market_name, targets)
            if key is None:
                continue
            rd_raw = row.get("report_date_as_yyyy_mm_dd", "")
            records.append({
                "market_name": market_name,
                "market_key": key,
                "report_date": rd_raw[:10] if rd_raw else "",
                "open_interest": _safe_int(row, "open_interest_all"),
                "categories": extract_fn(row),
            })
            found.add(key)

        if not records:
            return None

        report_dates = sorted({r["report_date"] for r in records}, reverse=True)
        missing = set(targets.values()) - found

        return {
            "found_markets": sorted(found),
            "missing_markets": sorted(missing),
            "report_date": report_dates[0] if report_dates else None,
            "weeks_found": len(report_dates),
            "records": records,
        }

    @staticmethod
    def _match_market(market_name: str, targets: dict[str, str]) -> Optional[str]:
        upper = market_name.upper()
        for keyword, key in targets.items():
            if keyword.upper() in upper:
                return key
        return None

    @staticmethod
    def _extract_tff_categories(row: dict) -> dict:
        def _cat(lf: str, sf: str) -> dict:
            l = _safe_int(row, lf)
            s = _safe_int(row, sf)
            return {"long": l, "short": s, "net": l - s}
        return {
            "dealer":          _cat("dealer_positions_long_all",    "dealer_positions_short_all"),
            "asset_manager":   _cat("asset_mgr_positions_long_all", "asset_mgr_positions_short_all"),
            "leveraged_funds": _cat("lev_money_positions_long_all", "lev_money_positions_short_all"),
            "other_reportable":_cat("other_rept_positions_long_all","other_rept_positions_short_all"),
            "nonreportable":   _cat("nonrept_positions_long_all",   "nonrept_positions_short_all"),
        }

    @staticmethod
    def _extract_disag_categories(row: dict) -> dict:
        def _cat(lf: str, *sfs: str) -> dict:
            l = _safe_int(row, lf)
            s = _safe_int(row, *sfs)
            return {"long": l, "short": s, "net": l - s}
        return {
            "producer_merchant": _cat("prod_merc_positions_long_all", "prod_merc_positions_short_all"),
            # CFTC Socrata dataset uses double-underscore for swap short in some exports
            "swap_dealer":       _cat("swap_positions_long_all", "swap__positions_short_all", "swap_positions_short_all"),
            "managed_money":     _cat("m_money_positions_long_all", "m_money_positions_short_all"),
            "other_reportable":  _cat("other_rept_positions_long_all", "other_rept_positions_short_all"),
            "nonreportable":     _cat("nonrept_positions_long_all", "nonrept_positions_short_all"),
        }

    def validate_schema(self, data: Optional[Any], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        for record in data.get("records", []):
            key = record.get("market_key", "?")
            for field in ("market_name", "market_key", "report_date", "open_interest", "categories"):
                if field not in record:
                    missing.append(f"{key}.{field}")
            cats = record.get("categories", {})
            if not cats:
                missing.append(f"{key}.categories_empty")
            else:
                for cat_name, cat in cats.items():
                    for subfield in ("long", "short", "net"):
                        if subfield not in cat:
                            missing.append(f"{key}.{cat_name}.{subfield}")
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[Any], use_case: str) -> str:
        if not data:
            return "N/A"
        rd = data.get("report_date")
        if not rd:
            return "N/A"
        try:
            lag = (date.today() - date.fromisoformat(rd)).days
        except ValueError:
            return "N/A"
        if lag <= _FRESHNESS_CURRENT_DAYS:
            return "weekly_current"
        if lag <= _FRESHNESS_LATE_DAYS:
            return "one_week_late"
        return f"stale:{lag}d"

    def evaluate_history(self, data: Optional[Any], use_case: str) -> str:
        if not data:
            return "N/A"
        weeks = data.get("weeks_found", 0)
        return f"{weeks}w" if weeks else "N/A"

    def summarize_result(
        self, data: Optional[Any], use_case: str, access_result: str
    ) -> ValidationResult:
        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        freshness = self.evaluate_freshness(data, use_case)
        history = self.evaluate_history(data, use_case)

        core_keys = _CORE_MARKETS[use_case]
        found_list = data["found_markets"] if data else []
        core_found = sum(1 for m in found_list if m in core_keys)
        core_total = len(core_keys)

        if not data or core_found == 0:
            scores = Scores(2, 1, 1, 2, 5, 5, 2)   # total=18 → Fallback only
        elif core_found >= core_total and fields_ok:
            scores = Scores(4, 4, 3, 4, 5, 5, 2)   # total=27 → Useful but optional
        else:
            scores = Scores(4, 3, 3, 4, 5, 5, 2)   # total=26 → Useful but optional

        final_status = determine_status(scores)

        if access_result != "ok" and not data:
            if final_status not in ("Fallback only", "Reject for v1"):
                final_status = "Fallback only"

        targets = _INDEX_TARGETS if use_case == "index_positioning" else _COMMODITY_TARGETS

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=list(targets.values()),
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth=history,
            freshness=freshness,
            parsing_difficulty="easy",
            rate_limit_notes="No auth required. CFTC Socrata API. No published rate limit; 0.5s courtesy delay.",
            fallback_suggestion="Direct CFTC CSV file download as alternative ingestion path.",
            final_status=final_status,
            notes=(
                f"Coverage: {core_found}/{core_total} core markets found. "
                f"Index/commodity-futures-only; weekly cadence with ~3-day lag. Not per-stock data."
            ),
            scores=scores,
            raw_sample_path=None,
        )
