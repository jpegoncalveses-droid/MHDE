"""GDELT 2.0 free news ingestion for non-SEC catalyst detection.

No API key required. All calls go through _call_gdelt_api() for easy mocking in tests.
GDELT has no auth — use sparingly (max 25 articles per query).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"

_NEWS_CLASSIFIERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(pentagon|dod|department\s+of\s+defense|army|navy|air\s+force|government|federal\s+agency).{0,80}(contract|award)", re.I), "government_contract"),
    (re.compile(r"(contract\s+(expansion|renewal|awarded|extended)|awarded\s+a\s+contract|contract\s+worth)", re.I), "contract_expansion"),
    (re.compile(r"(general\s+availability|ga\s+release|product\s+launch|announces.{0,30}new\s+(product|platform|service)|commercially\s+available)", re.I), "product_launch"),
    (re.compile(r"(subscriber|user\s+base|monthly\s+active|daily\s+active|customer\s+count).{0,60}(grew|reached|surpassed|hit|million|billion)", re.I), "subscriber_metric"),
    (re.compile(r"(layoff[s]?|workforce\s+reduction|headcount\s+reduction|job\s+cuts?|restructuring).{0,60}(employee|worker|staff|job[s]?)|(layoff[s]?).{0,60}(workforce\s+reduction|headcount\s+reduction)|restructuring.{0,60}layoff[s]?", re.I), "restructuring"),
    (re.compile(r"(insider\s+(bought|purchased|acquired)|director\s+(bought|purchased)|executive\s+purchase)", re.I), "insider_buying"),
]


@dataclass
class NewsArticle:
    title: str
    url: str
    published_at: str
    source: str
    tickers: list[str] = field(default_factory=list)
    snippet: str = ""


def classify_news_catalyst(article: NewsArticle) -> str:
    """Return a deterministic catalyst label for a news article, or 'unknown'."""
    text = f"{article.title} {article.snippet}"
    for pattern, label in _NEWS_CLASSIFIERS:
        if pattern.search(text):
            return label
    return "unknown"


def _call_gdelt_api(query: str, start_date: str, end_date: str) -> dict:
    import json
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode({
        "query": query,
        "mode": "artlist",
        "maxrecords": 25,
        "startdatetime": start_date.replace("-", "") + "000000",
        "enddatetime": end_date.replace("-", "") + "235959",
        "format": "json",
        "sourcelang": "english",
    })
    url = f"{_GDELT_API}?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_gdelt_articles(
    query: str,
    start_date: str,
    end_date: str,
) -> list[NewsArticle]:
    """Fetch news articles from GDELT matching query in date range.

    Returns empty list on any error (network, parse, etc).
    """
    if not query:
        return []
    try:
        data = _call_gdelt_api(query, start_date, end_date)
        articles = []
        for art in data.get("articles", []):
            articles.append(
                NewsArticle(
                    title=art.get("title", ""),
                    url=art.get("url", ""),
                    published_at=art.get("seendate", "")[:8],
                    source=art.get("domain", ""),
                    tickers=[],
                    snippet=art.get("socialimage", ""),
                )
            )
        return articles
    except Exception as exc:
        logger.warning("gdelt: query=%r — %s", query, exc)
        return []


# ---------------------------------------------------------------------------
# Orchestrator-compatible class (used by ingestion.orchestrator)
# ---------------------------------------------------------------------------
from ingestion.base_ingestor import StubIngestor  # noqa: E402


class GDELTIngestor(StubIngestor):
    """GDELT ingestor — exposes fetch_gdelt_articles via the BaseIngestor interface."""

    source_name = "gdelt"

    def ingest(self, conn, run_id, tickers):
        self.logger.info("gdelt: class-based ingest delegates to fetch_gdelt_articles")
        self.log_run(conn, run_id, "news", "stub", 0, 0, 0)
        return {"source": self.source_name, "status": "stub", "records": 0}
