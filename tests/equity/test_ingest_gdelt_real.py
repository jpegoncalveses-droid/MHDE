"""Tests for GDELT experimental ingestor."""
from unittest.mock import patch

import pytest

from ingestion.ingest_gdelt import GDELTIngestor, NewsArticle, classify_news_catalyst


def test_gdelt_ingestor_class_exists():
    ingestor = GDELTIngestor()
    assert hasattr(ingestor, "ingest")


def test_gdelt_ingestor_returns_dict_with_records():
    mock_articles = [
        NewsArticle(title="Company signs contract", url="http://x.com/1",
                    published_at="20260501", source="Reuters", tickers=[], snippet="")
    ]
    with patch("ingestion.ingest_gdelt.fetch_gdelt_articles", return_value=mock_articles):
        ingestor = GDELTIngestor()
        result = ingestor.ingest(query="contract", start_date="2026-05-01", end_date="2026-05-03")
    assert isinstance(result, dict)
    assert "records" in result
    assert result["records"] == 1
    assert result.get("status") in ("experimental_ok", "ok")


def test_gdelt_ingestor_handles_api_failure():
    with patch("ingestion.ingest_gdelt.fetch_gdelt_articles", side_effect=Exception("timeout")):
        ingestor = GDELTIngestor()
        result = ingestor.ingest(query="any", start_date="2026-05-01", end_date="2026-05-01")
    assert result.get("records") == 0
    assert "error" in result.get("status", "error")


def test_gdelt_ingestor_zero_articles():
    with patch("ingestion.ingest_gdelt.fetch_gdelt_articles", return_value=[]):
        ingestor = GDELTIngestor()
        result = ingestor.ingest(query="xyz", start_date="2026-05-01", end_date="2026-05-01")
    assert result["records"] == 0
    assert result.get("status") in ("experimental_ok", "ok")


def test_classify_news_catalyst_government_contract():
    article = NewsArticle(
        title="Pentagon awarded $500M contract to defense company",
        url="", published_at="", source="", tickers=[], snippet="",
    )
    assert classify_news_catalyst(article) == "government_contract"
