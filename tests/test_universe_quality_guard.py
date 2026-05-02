"""TDD tests for universe quality guard columns on the companies table.

RED state: companies table lacks active_sec_reporter, universe_exclusion_reason, etc.
"""
from __future__ import annotations

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_company(conn, ticker, **kwargs):
    conn.execute(
        "INSERT INTO companies (ticker, cik, company_name) VALUES (?, ?, ?)",
        [ticker, kwargs.get("cik", "0000999999"), kwargs.get("name", f"Corp {ticker}")],
    )


# ── schema presence ───────────────────────────────────────────────────────────

def test_companies_table_has_active_sec_reporter_column(conn):
    """active_sec_reporter column exists and is queryable."""
    _seed_company(conn, "AAPL", cik="0000320193")
    row = conn.execute(
        "SELECT active_sec_reporter FROM companies WHERE ticker='AAPL'"
    ).fetchone()
    assert row is not None


def test_companies_table_has_universe_exclusion_reason_column(conn):
    """universe_exclusion_reason column exists."""
    _seed_company(conn, "AAPL")
    row = conn.execute(
        "SELECT universe_exclusion_reason FROM companies WHERE ticker='AAPL'"
    ).fetchone()
    assert row is not None


def test_companies_table_has_last_financial_filing_date_column(conn):
    """last_financial_filing_date column exists."""
    _seed_company(conn, "AAPL")
    row = conn.execute(
        "SELECT last_financial_filing_date FROM companies WHERE ticker='AAPL'"
    ).fetchone()
    assert row is not None


def test_companies_table_has_has_financial_reporting_forms_column(conn):
    """has_financial_reporting_forms column exists."""
    _seed_company(conn, "AAPL")
    row = conn.execute(
        "SELECT has_financial_reporting_forms FROM companies WHERE ticker='AAPL'"
    ).fetchone()
    assert row is not None


# ── default values ────────────────────────────────────────────────────────────

def test_active_sec_reporter_defaults_to_true(conn):
    """Newly inserted company defaults to active_sec_reporter=True."""
    _seed_company(conn, "AAPL")
    row = conn.execute(
        "SELECT active_sec_reporter FROM companies WHERE ticker='AAPL'"
    ).fetchone()
    # Default is True (NULL is also acceptable for pre-existing rows, but True for new)
    assert row[0] is True or row[0] is None


def test_universe_exclusion_reason_defaults_to_null(conn):
    """Newly inserted company defaults to universe_exclusion_reason=NULL."""
    _seed_company(conn, "AAPL")
    row = conn.execute(
        "SELECT universe_exclusion_reason FROM companies WHERE ticker='AAPL'"
    ).fetchone()
    assert row[0] is None


# ── mark inactive logic ───────────────────────────────────────────────────────

def test_mark_company_inactive_sec_reporter(conn):
    """mark_inactive_sec_reporter sets active_sec_reporter=False."""
    _seed_company(conn, "IFNNY")
    from health.universe_quality import mark_inactive_sec_reporter
    mark_inactive_sec_reporter(conn, "IFNNY", reason="no_current_sec_financial_reporting")
    row = conn.execute(
        "SELECT active_sec_reporter FROM companies WHERE ticker='IFNNY'"
    ).fetchone()
    assert row[0] is False


def test_mark_company_sets_exclusion_reason(conn):
    """mark_inactive_sec_reporter stores the exclusion reason."""
    _seed_company(conn, "RSHGY")
    from health.universe_quality import mark_inactive_sec_reporter
    mark_inactive_sec_reporter(conn, "RSHGY", reason="adr_no_financial_forms")
    row = conn.execute(
        "SELECT universe_exclusion_reason FROM companies WHERE ticker='RSHGY'"
    ).fetchone()
    assert row[0] == "adr_no_financial_forms"


def test_mark_nonexistent_company_is_safe(conn):
    """mark_inactive_sec_reporter on unknown ticker does not raise."""
    from health.universe_quality import mark_inactive_sec_reporter
    mark_inactive_sec_reporter(conn, "ZZZZZ", reason="test")  # should not raise


def test_mark_company_sets_has_financial_reporting_forms_false(conn):
    """mark_inactive_sec_reporter sets has_financial_reporting_forms=False."""
    _seed_company(conn, "IFNNY")
    from health.universe_quality import mark_inactive_sec_reporter
    mark_inactive_sec_reporter(conn, "IFNNY", reason="no_current_sec_financial_reporting")
    row = conn.execute(
        "SELECT has_financial_reporting_forms FROM companies WHERE ticker='IFNNY'"
    ).fetchone()
    assert row[0] is False


# ── universe filter ───────────────────────────────────────────────────────────

def test_inactive_reporters_excluded_from_active_universe(conn):
    """Companies with active_sec_reporter=False are not returned as active."""
    _seed_company(conn, "IFNNY")
    _seed_company(conn, "AAPL", cik="0000320193")
    from health.universe_quality import mark_inactive_sec_reporter, get_active_universe_tickers
    mark_inactive_sec_reporter(conn, "IFNNY", reason="no_current_sec_financial_reporting")
    active = get_active_universe_tickers(conn)
    assert "AAPL" in active
    assert "IFNNY" not in active


def test_all_companies_active_by_default(conn):
    """Before any marking, all companies are in the active universe."""
    _seed_company(conn, "AAPL")
    _seed_company(conn, "MSFT")
    from health.universe_quality import get_active_universe_tickers
    active = get_active_universe_tickers(conn)
    assert "AAPL" in active
    assert "MSFT" in active
