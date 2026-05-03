"""Tests for SEC EDGAR ticker-details enricher."""
from unittest.mock import patch

import duckdb
import pytest

from universe.ticker_details_enricher import (
    TickerDetail,
    _cik_url,
    _extract_shares_outstanding,
    enrich_ticker_details,
    run_enrichment,
)


# ---------------------------------------------------------------------------
# _cik_url
# ---------------------------------------------------------------------------

def test_cik_url_pads_to_10_digits():
    url = _cik_url("320193")
    assert "CIK0000320193" in url


def test_cik_url_handles_already_padded():
    url = _cik_url("0000320193")
    assert "CIK0000320193" in url


def test_cik_url_handles_cik_prefix():
    url = _cik_url("CIK0000320193")
    assert "CIK0000320193" in url


# ---------------------------------------------------------------------------
# _extract_shares_outstanding
# ---------------------------------------------------------------------------

def _make_facts(shares_val: int, form: str = "10-K", end: str = "2025-12-31") -> dict:
    return {
        "facts": {
            "us-gaap": {
                "CommonStockSharesOutstanding": {
                    "units": {
                        "shares": [{"val": shares_val, "form": form, "end": end, "filed": end}]
                    }
                }
            }
        }
    }


def test_extract_shares_from_10k():
    facts = _make_facts(1_000_000_000, form="10-K")
    assert _extract_shares_outstanding(facts) == 1_000_000_000


def test_extract_shares_from_10q():
    facts = _make_facts(500_000_000, form="10-Q")
    assert _extract_shares_outstanding(facts) == 500_000_000


def test_extract_shares_prefers_most_recent():
    facts = {
        "facts": {
            "us-gaap": {
                "CommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {"val": 900_000_000, "form": "10-K", "end": "2024-12-31"},
                            {"val": 950_000_000, "form": "10-Q", "end": "2025-09-30"},
                        ]
                    }
                }
            }
        }
    }
    assert _extract_shares_outstanding(facts) == 950_000_000


def test_extract_shares_falls_back_to_dei():
    facts = {
        "facts": {
            "us-gaap": {},
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"val": 300_000_000, "form": "10-K", "end": "2025-12-31"}]}
                }
            },
        }
    }
    assert _extract_shares_outstanding(facts) == 300_000_000


def test_extract_shares_returns_none_when_empty():
    assert _extract_shares_outstanding({"facts": {}}) is None


# ---------------------------------------------------------------------------
# enrich_ticker_details
# ---------------------------------------------------------------------------

def _mock_facts(shares: int):
    return _make_facts(shares)


def test_enrich_computes_market_cap():
    with patch(
        "universe.ticker_details_enricher._fetch_sec_companyfacts",
        return_value=_mock_facts(1_000_000_000),
    ):
        detail = enrich_ticker_details("AAAB", cik="0001234567", latest_price=100.0)

    assert detail is not None
    assert detail.market_cap == 100_000_000_000.0
    assert detail.shares_outstanding == 1_000_000_000


def test_enrich_returns_none_market_cap_when_no_price():
    with patch(
        "universe.ticker_details_enricher._fetch_sec_companyfacts",
        return_value=_mock_facts(1_000_000_000),
    ):
        detail = enrich_ticker_details("AAAB", cik="0001234567", latest_price=None)

    assert detail is not None
    assert detail.market_cap is None
    assert detail.shares_outstanding == 1_000_000_000


def test_enrich_returns_none_on_network_error():
    with patch(
        "universe.ticker_details_enricher._fetch_sec_companyfacts",
        side_effect=Exception("network timeout"),
    ):
        detail = enrich_ticker_details("AAAB", cik="0001234567", latest_price=100.0)

    assert detail is None


def test_enrich_returns_none_when_no_shares_in_xbrl():
    with patch(
        "universe.ticker_details_enricher._fetch_sec_companyfacts",
        return_value={"facts": {}},
    ):
        detail = enrich_ticker_details("AAAB", cik="0001234567", latest_price=100.0)

    assert detail is not None
    assert detail.market_cap is None
    assert detail.shares_outstanding is None


def test_ticker_detail_dataclass():
    d = TickerDetail(ticker="AAAB", market_cap=5e11, shares_outstanding=5_000_000_000)
    assert d.ticker == "AAAB"
    assert d.market_cap == 5e11
    assert d.shares_outstanding == 5_000_000_000
    assert d.exchange is None


# ---------------------------------------------------------------------------
# run_enrichment
# ---------------------------------------------------------------------------

def _make_db(tmp_path, rows: list[tuple]) -> str:
    """Create minimal DB with companies and prices_daily tables."""
    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE companies (
            ticker VARCHAR PRIMARY KEY, cik VARCHAR,
            is_active BOOLEAN DEFAULT true,
            active_sec_reporter BOOLEAN DEFAULT true,
            market_cap DOUBLE
        )
    """)
    conn.execute("""
        CREATE TABLE prices_daily (
            ticker VARCHAR, trade_date DATE, close DOUBLE,
            adjusted_close DOUBLE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    for ticker, cik, price in rows:
        conn.execute(
            "INSERT INTO companies (ticker, cik, market_cap) VALUES (?, ?, NULL)",
            [ticker, cik],
        )
        if price is not None:
            conn.execute(
                "INSERT INTO prices_daily (ticker, trade_date, close) VALUES (?, '2025-12-31', ?)",
                [ticker, price],
            )
    conn.close()
    return db_path


def test_run_enrichment_updates_market_cap(tmp_path):
    db_path = _make_db(tmp_path, [("AAAB", "0001234567", 150.0)])

    with patch(
        "universe.ticker_details_enricher._fetch_sec_companyfacts",
        return_value=_mock_facts(1_000_000_000),
    ):
        result = run_enrichment(db_path=db_path, delay=0)

    assert result["updated"] == 1
    assert result["errors"] == 0
    conn = duckdb.connect(db_path, read_only=True)
    mc = conn.execute("SELECT market_cap FROM companies WHERE ticker='AAAB'").fetchone()[0]
    conn.close()
    assert mc == 150_000_000_000.0


def test_run_enrichment_skips_tickers_without_cik(tmp_path):
    db_path = _make_db(tmp_path, [("NOCIK", None, 100.0)])

    with patch("universe.ticker_details_enricher._fetch_sec_companyfacts") as mock_fetch:
        result = run_enrichment(db_path=db_path, delay=0)

    mock_fetch.assert_not_called()
    assert result["updated"] == 0


def test_run_enrichment_error_counted_on_network_failure(tmp_path):
    db_path = _make_db(tmp_path, [("AAAB", "0001234567", 100.0)])

    with patch(
        "universe.ticker_details_enricher._fetch_sec_companyfacts",
        side_effect=Exception("timeout"),
    ):
        result = run_enrichment(db_path=db_path, delay=0)

    assert result["errors"] == 1
    assert result["updated"] == 0


def test_run_enrichment_returns_ok_reason(tmp_path):
    db_path = _make_db(tmp_path, [])
    result = run_enrichment(db_path=db_path, delay=0)
    assert result["reason"] == "ok"
