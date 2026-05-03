"""Tests for Polygon ticker-details enricher."""
from unittest.mock import patch

import pytest

from universe.ticker_details_enricher import (
    TickerDetail,
    enrich_ticker_details,
    run_enrichment,
)


def test_ticker_detail_dataclass():
    d = TickerDetail(
        ticker="AAAB",
        market_cap=5e11,
        exchange="XNAS",
        sic_code="7372",
        sic_description="Prepackaged Software",
    )
    assert d.ticker == "AAAB"
    assert d.market_cap == 5e11


def test_enrich_returns_none_when_no_key():
    result = enrich_ticker_details("AAAB", api_key=None)
    assert result is None


def test_enrich_populates_fields_from_polygon_response():
    mock_response = {
        "results": {
            "market_cap": 500_000_000_000,
            "primary_exchange": "XNAS",
            "sic_code": "7372",
            "sic_description": "Prepackaged Software",
        }
    }
    with patch(
        "universe.ticker_details_enricher._fetch_polygon_details",
        return_value=mock_response,
    ):
        detail = enrich_ticker_details("AAAB", api_key="fake-key")

    assert detail is not None
    assert detail.market_cap == 500_000_000_000
    assert detail.exchange == "XNAS"
    assert detail.sic_code == "7372"
    assert detail.sic_description == "Prepackaged Software"


def test_enrich_returns_none_on_api_error():
    with patch(
        "universe.ticker_details_enricher._fetch_polygon_details",
        side_effect=Exception("connection timeout"),
    ):
        detail = enrich_ticker_details("AAAB", api_key="fake-key")

    assert detail is None


def test_enrich_handles_missing_sic_code():
    mock_response = {
        "results": {
            "market_cap": 1e11,
            "primary_exchange": "XNYS",
        }
    }
    with patch(
        "universe.ticker_details_enricher._fetch_polygon_details",
        return_value=mock_response,
    ):
        detail = enrich_ticker_details("AAAB", api_key="fake-key")

    assert detail is not None
    assert detail.sic_code is None
    assert detail.sic_description is None


def test_run_enrichment_no_key_returns_early():
    result = run_enrichment(db_path="fake.db", api_key=None)
    assert result["reason"] == "no_api_key"
    assert result["updated"] == 0


def test_run_enrichment_updates_db(tmp_path):
    import duckdb

    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute(
        "CREATE TABLE companies (ticker VARCHAR PRIMARY KEY, is_active BOOLEAN, market_cap DOUBLE)"
    )
    conn.execute("INSERT INTO companies VALUES ('AAAB', true, NULL)")
    conn.close()

    mock_response = {"results": {"market_cap": 999_000_000_000, "primary_exchange": "XNAS"}}
    with patch(
        "universe.ticker_details_enricher._fetch_polygon_details",
        return_value=mock_response,
    ):
        result = run_enrichment(db_path=db_path, api_key="fake-key", delay=0)

    assert result["updated"] == 1
    conn2 = duckdb.connect(db_path)
    market_cap = conn2.execute("SELECT market_cap FROM companies WHERE ticker='AAAB'").fetchone()[0]
    conn2.close()
    assert market_cap == 999_000_000_000
