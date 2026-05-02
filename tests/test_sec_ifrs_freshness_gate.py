"""TDD tests for namespace-aware fundamentals freshness gate.

Bug: _fundamentals_are_fresh() uses MAX(created_at) with no namespace filter.
IFRS filers (20-F/40-F) with stale us-gaap rows get fresh=True → ifrs-full never fetched.

RED state: gate is not yet namespace-aware.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_filing(conn, ticker, form_type, days_ago=30):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, cik, company_name) VALUES (?,?,?)",
        [ticker, "0000111111", f"Corp {ticker}"],
    )
    filing_date = (date.today() - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO filings (id, ticker, cik, form_type, filing_date)"
        " VALUES (?,?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, "0000111111", form_type, filing_date],
    )


def _seed_fundamentals(conn, ticker, concept_prefix, days_old_created, as_of_days_ago=400):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, cik, company_name) VALUES (?,?,?)",
        [ticker, "0000111111", f"Corp {ticker}"],
    )
    created_at = datetime.utcnow() - timedelta(days=days_old_created)
    as_of = (date.today() - timedelta(days=as_of_days_ago)).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO fundamentals_raw (id, ticker, cik, concept, value, unit, as_of_date, form, run_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, "0000111111",
         f"{concept_prefix}/NetIncomeLoss", 1_000_000, "USD", as_of, "20-F", "run1", created_at],
    )


# ── Domestic filers (us-gaap) — unchanged behavior ───────────────────────────

def test_domestic_fresh_usgaap_stays_fresh(conn):
    """Domestic ticker with recent us-gaap rows → fresh (same as before)."""
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "AAPL", "10-K", days_ago=30)
    _seed_fundamentals(conn, "AAPL", "us-gaap", days_old_created=1)
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "AAPL") is True


def test_domestic_stale_usgaap_returns_not_fresh(conn):
    """Domestic ticker with stale us-gaap rows → not fresh."""
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "AAPL", "10-K", days_ago=30)
    _seed_fundamentals(conn, "AAPL", "us-gaap", days_old_created=10)
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "AAPL") is False


def test_no_fundamentals_at_all_returns_not_fresh(conn):
    """Ticker with no fundamentals → not fresh (bootstrap needed)."""
    from ingestion.ingest_sec import SECIngestor
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, cik, company_name) VALUES (?,?,?)",
        ["NEWCO", "0000222222", "New Co"],
    )
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "NEWCO") is False


# ── IFRS filers (20-F/40-F) — new namespace-aware behavior ───────────────────

def test_ifrs_filer_with_stale_usgaap_only_is_not_fresh(conn):
    """20-F filer has recent us-gaap rows (created today) but NO ifrs-full rows → not fresh.

    This is the core bug: the old gate returns True here, blocking ifrs-full fetch.
    The fixed gate must return False so the ingestor fetches and stores ifrs-full rows.
    """
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "EQNR", "20-F", days_ago=30)
    # us-gaap rows created TODAY (fresh created_at), but no ifrs-full rows
    _seed_fundamentals(conn, "EQNR", "us-gaap", days_old_created=0)
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "EQNR") is False, \
        "IFRS filer with no ifrs-full rows must NOT be considered fresh"


def test_ifrs_filer_with_fresh_ifrs_rows_is_fresh(conn):
    """20-F filer with recent ifrs-full rows → fresh (no re-fetch needed)."""
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "EQNR", "20-F", days_ago=30)
    _seed_fundamentals(conn, "EQNR", "ifrs-full", days_old_created=1)
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "EQNR") is True


def test_40f_filer_same_as_20f(conn):
    """40-F filer (Canadian cross-listed) treated same as 20-F — namespace-aware."""
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "CVE", "40-F", days_ago=30)
    # Only us-gaap rows, no ifrs-full
    _seed_fundamentals(conn, "CVE", "us-gaap", days_old_created=0)
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "CVE") is False


def test_ifrs_filer_with_stale_ifrs_rows_is_not_fresh(conn):
    """20-F filer with old ifrs-full rows (>7 days old) → not fresh."""
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "BTI", "20-F", days_ago=30)
    _seed_fundamentals(conn, "BTI", "ifrs-full", days_old_created=10)
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "BTI") is False


def test_ifrs_filer_detected_by_most_recent_filing(conn):
    """Only the most recent major form type matters for IFRS detection.

    Ticker has both a 10-K (old) and 20-F (recent) → detected as IFRS filer.
    """
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "TST", "10-K", days_ago=400)   # old domestic form
    _seed_filing(conn, "TST", "20-F", days_ago=30)    # recent foreign form
    _seed_fundamentals(conn, "TST", "us-gaap", days_old_created=0)
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "TST") is False


def test_ifrs_filer_no_fundamentals_at_all_is_not_fresh(conn):
    """20-F filer with zero fundamentals rows → not fresh."""
    from ingestion.ingest_sec import SECIngestor
    _seed_filing(conn, "BBVA", "20-F", days_ago=30)
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, cik, company_name) VALUES (?,?,?)",
        ["BBVA", "0000333333", "BBVA SA"],
    )
    ingestor = SECIngestor({})
    assert ingestor._fundamentals_are_fresh(conn, "BBVA") is False
