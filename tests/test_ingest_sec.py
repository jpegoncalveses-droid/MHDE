from __future__ import annotations

import logging
from unittest.mock import MagicMock

import duckdb
import pytest

from storage.db import init_schema


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def _seed_company(conn, ticker, cik):
    conn.execute(
        "INSERT INTO companies (ticker, cik, company_name, is_active) VALUES (?, ?, ?, true)",
        [ticker, cik, f"{ticker} Corp"],
    )


def _make_404_response():
    resp = MagicMock()
    resp.status_code = 404
    return resp


def _make_200_response(data: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data
    return resp


def test_404_companyfacts_no_retry(conn, monkeypatch):
    """A 404 on companyfacts must not be retried; total request count is bounded."""
    _seed_company(conn, "AAPL", "0000320193")
    call_urls: list[str] = []

    def mock_get(url, **kwargs):
        call_urls.append(url)
        return _make_404_response()

    monkeypatch.setattr("requests.get", mock_get)
    monkeypatch.setattr("time.sleep", lambda _: None)

    from ingestion.ingest_sec import SECIngestor
    ingestor = SECIngestor(cfg={})
    ingestor.ingest(conn, "run1", ["AAPL"])

    # submissions + companyfacts = at most 2 calls for one ticker (no retry)
    assert len(call_urls) <= 2
    # Each URL is called at most once
    assert len(call_urls) == len(set(call_urls))


def test_404_not_found_count_tracked(conn, monkeypatch):
    """_not_found_count is incremented for each 404 response."""
    _seed_company(conn, "AAPL", "0000320193")
    _seed_company(conn, "MSFT", "0000789019")

    monkeypatch.setattr("requests.get", lambda url, **kw: _make_404_response())
    monkeypatch.setattr("time.sleep", lambda _: None)

    from ingestion.ingest_sec import SECIngestor
    ingestor = SECIngestor(cfg={})
    ingestor.ingest(conn, "run1", ["AAPL", "MSFT"])

    assert ingestor._not_found_count > 0


def test_404_summary_warning_emitted(conn, monkeypatch, caplog):
    """After ingestion, a single WARNING summarising total 404s must be logged,
    not one WARNING per ticker."""
    _seed_company(conn, "AAPL", "0000320193")
    _seed_company(conn, "MSFT", "0000789019")

    monkeypatch.setattr("requests.get", lambda url, **kw: _make_404_response())
    monkeypatch.setattr("time.sleep", lambda _: None)

    from ingestion.ingest_sec import SECIngestor
    ingestor = SECIngestor(cfg={})

    with caplog.at_level(logging.WARNING, logger="mhde.ingestion.sec_edgar"):
        ingestor.ingest(conn, "run1", ["AAPL", "MSFT"])

    # Count WARNING messages that mention 404
    warning_404_msgs = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "404" in str(r.message)
    ]
    # Must be exactly 1 summary, not 2+ (one per ticker)
    assert len(warning_404_msgs) == 1
    # The summary message should include a count or "404"
    assert "404" in warning_404_msgs[0].message
