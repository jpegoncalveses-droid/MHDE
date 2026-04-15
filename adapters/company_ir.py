from __future__ import annotations

import re
import time
import logging
from datetime import date, datetime
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from adapters.base import BaseAdapter, Scores, ValidationResult
from runner.scoring import determine_status

logger = logging.getLogger("mhde.adapter.company_ir")

# Ordered list of CSS selector strategies to try for press release items.
# Each entry: (container_selector, title_selector, date_selector)
_PRESS_STRATEGIES = [
    ("div.press-release-item", "a", "span.date"),
    ("article", "h2 a", "time"),
    ("li.news-item", "a.news-title", "span.news-date"),
    ("div.news-release", "a", "span"),
    ("table tr", "td a", "td:nth-of-type(2)"),
]

_DATE_FORMATS = [
    "%B %d, %Y",      # October 31, 2024
    "%b %d, %Y",      # Oct 31, 2024
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d %B %Y",
]


def _parse_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        from dateutil import parser as dp
        return dp.parse(raw).date().isoformat()
    except Exception:
        pass
    return None


def _scrape_items(html: str, strategy_list: list) -> tuple[list[dict], str]:
    """Try each strategy, return first that finds items."""
    soup = BeautifulSoup(html, "lxml")
    for container_sel, title_sel, date_sel in strategy_list:
        containers = soup.select(container_sel)
        if not containers:
            continue
        items = []
        for c in containers[:10]:
            title_el = c.select_one(title_sel)
            date_el = c.select_one(date_sel)
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            raw_date = ""
            if date_el:
                raw_date = date_el.get("datetime", "") or date_el.get_text(strip=True)
            parsed_date = _parse_date(raw_date) if raw_date else None
            if title:
                items.append({"title": title, "date": parsed_date or raw_date, "url": href})
        if items:
            return items, container_sel
    return [], "none_matched"


class CompanyIRAdapter(BaseAdapter):
    source_name = "company_ir"
    use_cases = ["press_releases", "events"]

    def _delay(self):
        d = self.settings["company_ir"].get("request_delay", 0)
        if d:
            time.sleep(d)

    def _headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (compatible; MHDE-Validation/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        }

    def _fetch_html(self, url: str) -> tuple[Optional[str], Optional[str]]:
        try:
            r = requests.get(
                url, headers=self._headers(),
                timeout=self.settings["company_ir"].get("request_timeout", 20),
                allow_redirects=True,
            )
            if r.status_code == 200:
                return r.text, None
            return None, f"HTTP {r.status_code}"
        except Exception as exc:
            return None, str(exc)

    def _canary_ticker(self) -> dict:
        for t in self.tickers_config:
            if t.get("ir_press_url"):
                return t
        return {}

    def test_access(self) -> tuple[str, Optional[str]]:
        canary = self._canary_ticker()
        if not canary:
            return "error", "No ticker with ir_press_url in config"
        html, err = self._fetch_html(canary["ir_press_url"])
        if html is not None:
            return "ok", None
        return "error", err

    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[dict]:
        results = {}
        url_key = "ir_press_url" if use_case == "press_releases" else "ir_events_url"
        for t in tickers:
            url = t.get(url_key)
            if not url:
                continue
            ticker = t["ticker"]
            self._delay()
            html, err = self._fetch_html(url)
            if html is None:
                results[ticker] = {"status": "error", "error": err, "items": []}
                continue
            items, strategy_used = _scrape_items(html, _PRESS_STRATEGIES)
            status = "ok" if items else "no_items_found"
            results[ticker] = {
                "status": status,
                "items": items,
                "strategy_used": strategy_used,
                "url": url,
                "html_length": len(html),
            }
            logger.info("company_ir %s %s: %d items via %s", ticker, use_case, len(items), strategy_used)
        return results if results else None

    def validate_schema(self, data: Optional[dict], use_case: str) -> tuple[bool, list[str]]:
        if not data:
            return False, ["no_data"]
        missing: list[str] = []
        for ticker, payload in data.items():
            if payload.get("status") not in ("ok", "parsed"):
                continue
            items = payload.get("items", [])
            if not items:
                continue
            item = items[0]
            if "title" not in item or not item["title"]:
                missing.append("items[].title")
            if "date" not in item or not item["date"]:
                missing.append("items[].date")
            break
        return len(missing) == 0, missing

    def evaluate_freshness(self, data: Optional[dict], use_case: str) -> str:
        if not data:
            return "N/A"
        most_recent = date.min
        for payload in data.values():
            for item in payload.get("items", []):
                raw = item.get("date", "")
                if raw:
                    try:
                        d = datetime.strptime(raw[:10], "%Y-%m-%d").date()
                        if d > most_recent:
                            most_recent = d
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

    def evaluate_history(self, data: Optional[dict], use_case: str) -> str:
        return "varies"   # IR pages show rolling window; no structured history

    def summarize_result(self, data: Optional[dict], use_case: str, access_result: str) -> ValidationResult:
        fields_ok, missing = self.validate_schema(data, use_case) if data else (False, ["no_data"])
        freshness = self.evaluate_freshness(data, use_case)

        ok_count = sum(1 for v in (data or {}).values() if v.get("status") == "ok")
        total_count = len(data) if data else 0
        parse_note = f"{ok_count}/{total_count} tickers parsed successfully."

        if access_result == "ok":
            scores = Scores(
                access=3,
                completeness=3 if fields_ok else 1,
                freshness=4,
                reliability=2,    # scraping is inherently brittle
                parsing_ease=2,   # HTML scraping, each site different
                cost_efficiency=5,
                strategic_value=4,
            )
        else:
            scores = Scores(1, 1, 1, 1, 2, 5, 4)

        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=[t["ticker"] for t in self.tickers_config if t.get("ir_press_url")],
            access_result=access_result,
            access_error=None,
            required_fields_present=fields_ok,
            missing_fields=missing,
            historical_depth="varies",
            freshness=freshness,
            parsing_difficulty="hard",
            rate_limit_notes="No auth. Some sites block scrapers or require JS rendering.",
            fallback_suggestion="Use Finviz or Globe Newswire RSS feeds as fallback.",
            final_status=determine_status(scores),
            notes=f"{parse_note} ETFs excluded. JS-heavy sites may return empty results.",
            scores=scores,
            raw_sample_path=None,
        )
