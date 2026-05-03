"""Tests for earnings estimate ingestion."""
import pytest
from unittest.mock import patch

from ingestion.ingest_earnings_estimates import (
    EarningsSurprise,
    compute_surprise_pct,
    ingest_earnings_for_ticker,
    parse_alpha_vantage_earnings,
)


def test_compute_surprise_pct_positive():
    pct = compute_surprise_pct(reported=2.15, estimated=1.90)
    assert abs(pct - 13.16) < 0.01


def test_compute_surprise_pct_negative():
    pct = compute_surprise_pct(reported=1.50, estimated=2.00)
    assert abs(pct - (-25.0)) < 0.01


def test_compute_surprise_pct_zero_estimate():
    pct = compute_surprise_pct(reported=1.0, estimated=0.0)
    assert pct is None


def test_parse_alpha_vantage_earnings_basic():
    raw = {
        "quarterlyEarnings": [
            {
                "fiscalDateEnding": "2026-03-31",
                "reportedEPS": "2.15",
                "estimatedEPS": "1.90",
                "surprisePercentage": "13.16",
            }
        ]
    }
    results = parse_alpha_vantage_earnings("AAAB", raw)
    assert len(results) == 1
    assert results[0].ticker == "AAAB"
    assert results[0].reported_eps == 2.15
    assert results[0].estimated_eps == 1.90
    assert abs(results[0].surprise_pct - 13.16) < 0.01


def test_parse_handles_none_eps():
    raw = {
        "quarterlyEarnings": [
            {
                "fiscalDateEnding": "2026-03-31",
                "reportedEPS": "None",
                "estimatedEPS": "None",
                "surprisePercentage": "None",
            }
        ]
    }
    results = parse_alpha_vantage_earnings("AAAB", raw)
    assert len(results) == 1
    assert results[0].reported_eps is None
    assert results[0].surprise_pct is None


def test_parse_empty_response():
    results = parse_alpha_vantage_earnings("AAAB", {})
    assert results == []


def test_ingest_no_api_key_returns_zero(tmp_path):
    count = ingest_earnings_for_ticker(
        "AAAB", api_key=None, db_path=str(tmp_path / "test.duckdb")
    )
    assert count == 0


def test_ingest_writes_to_db(tmp_path):
    import duckdb

    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE earnings_estimates (
            ticker VARCHAR NOT NULL,
            fiscal_date DATE NOT NULL,
            reported_eps DOUBLE,
            estimated_eps DOUBLE,
            surprise_eps DOUBLE,
            surprise_pct DOUBLE,
            reported_revenue DOUBLE,
            estimated_revenue DOUBLE,
            revenue_surprise_pct DOUBLE,
            guidance_direction VARCHAR,
            source VARCHAR NOT NULL DEFAULT 'alpha_vantage',
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, fiscal_date, source)
        )
    """)
    conn.close()

    mock_raw = {
        "quarterlyEarnings": [
            {
                "fiscalDateEnding": "2026-03-31",
                "reportedEPS": "2.15",
                "estimatedEPS": "1.90",
                "surprisePercentage": "13.16",
            }
        ]
    }
    with patch(
        "ingestion.ingest_earnings_estimates._fetch_alpha_vantage_earnings",
        return_value=mock_raw,
    ):
        count = ingest_earnings_for_ticker("AAAB", api_key="fake-key", db_path=db_path)

    assert count == 1
    conn2 = duckdb.connect(db_path)
    row = conn2.execute(
        "SELECT ticker, reported_eps, surprise_pct FROM earnings_estimates"
    ).fetchone()
    conn2.close()
    assert row[0] == "AAAB"
    assert abs(row[1] - 2.15) < 0.001
    assert abs(row[2] - 13.16) < 0.01


def test_earnings_surprise_dataclass():
    s = EarningsSurprise(
        ticker="AAAB",
        fiscal_date="2026-03-31",
        reported_eps=2.15,
        estimated_eps=1.90,
        surprise_pct=13.16,
    )
    assert s.ticker == "AAAB"
    assert s.fiscal_date == "2026-03-31"
