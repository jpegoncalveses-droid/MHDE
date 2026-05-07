"""Tests for GDELT news ingestion and catalyst classifiers."""
from unittest.mock import patch

import pytest

from ingestion.ingest_gdelt import (
    NewsArticle,
    _NEWS_CLASSIFIERS,
    classify_news_catalyst,
    fetch_gdelt_articles,
)


def test_news_article_dataclass():
    a = NewsArticle(title="Test", url="http://x.com", published_at="20260101", source="Reuters")
    assert a.title == "Test"
    assert a.tickers == []
    assert a.snippet == ""


def test_classify_government_contract():
    a = NewsArticle(
        title="Company awarded $500M Pentagon contract for cloud services",
        url="http://x.com", published_at="20260101", source="DOD News",
    )
    assert classify_news_catalyst(a) == "government_contract"


def test_classify_contract_expansion():
    a = NewsArticle(
        title="Company announces contract expansion worth $200M",
        url="http://x.com", published_at="20260101", source="PR Newswire",
    )
    assert classify_news_catalyst(a) == "contract_expansion"


def test_classify_product_launch():
    a = NewsArticle(
        title="Company announces general availability of new AI platform",
        url="http://x.com", published_at="20260101", source="TechCrunch",
    )
    assert classify_news_catalyst(a) == "product_launch"


def test_classify_subscriber_metric():
    a = NewsArticle(
        title="Streaming service subscriber count reached 200 million",
        url="http://x.com", published_at="20260101", source="Reuters",
    )
    assert classify_news_catalyst(a) == "subscriber_metric"


def test_classify_restructuring():
    a = NewsArticle(
        title="Company to cut 2000 jobs in restructuring",
        url="http://x.com", published_at="20260101", source="Bloomberg",
        snippet="layoffs announced amid cost cutting",
    )
    assert classify_news_catalyst(a) == "restructuring"


def test_classify_insider_buying():
    a = NewsArticle(
        title="CEO insider purchased 50000 shares at market open",
        url="http://x.com", published_at="20260101", source="SEC Filing",
    )
    assert classify_news_catalyst(a) == "insider_buying"


def test_classify_unknown():
    a = NewsArticle(
        title="Company reports routine quarterly results in line with expectations",
        url="http://x.com", published_at="20260101", source="Reuters",
    )
    assert classify_news_catalyst(a) == "unknown"


def test_fetch_empty_query_returns_empty():
    articles = fetch_gdelt_articles(query="", start_date="2026-01-01", end_date="2026-01-01")
    assert articles == []


def test_fetch_gdelt_mocked():
    mock_data = {
        "articles": [
            {
                "title": "Company awarded government contract",
                "url": "http://example.com/news/1",
                "seendate": "20260101T120000Z",
                "domain": "example.com",
                "socialimage": "",
            }
        ]
    }
    with patch("ingestion.ingest_gdelt._call_gdelt_api", return_value=mock_data):
        articles = fetch_gdelt_articles("contract", "2026-01-01", "2026-01-01")
    assert len(articles) == 1
    assert articles[0].title == "Company awarded government contract"
    assert articles[0].source == "example.com"


def test_fetch_handles_api_error():
    with patch(
        "ingestion.ingest_gdelt._call_gdelt_api",
        side_effect=Exception("network timeout"),
    ):
        articles = fetch_gdelt_articles("test", "2026-01-01", "2026-01-01")
    assert articles == []


def test_fetch_handles_empty_articles_key():
    with patch("ingestion.ingest_gdelt._call_gdelt_api", return_value={}):
        articles = fetch_gdelt_articles("test", "2026-01-01", "2026-01-01")
    assert articles == []


def test_classifiers_list_has_all_labels():
    labels = {label for _, label in _NEWS_CLASSIFIERS}
    for expected in ("government_contract", "contract_expansion", "product_launch",
                     "subscriber_metric", "restructuring", "insider_buying"):
        assert expected in labels


def test_classify_uses_snippet_text():
    a = NewsArticle(
        title="Company update",
        url="http://x.com", published_at="20260101", source="Reuters",
        snippet="layoffs announced with significant workforce reduction",
    )
    assert classify_news_catalyst(a) == "restructuring"
