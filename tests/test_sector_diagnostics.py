"""Tests for sector cluster diagnostics engine."""
from __future__ import annotations

import uuid

import duckdb
import pytest


def _make_conn(etf_rows: list[tuple] = None) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE prices_daily (id VARCHAR PRIMARY KEY, ticker VARCHAR, trade_date DATE, close DOUBLE)")
    conn.execute("CREATE TABLE companies (ticker VARCHAR PRIMARY KEY, sector VARCHAR, is_active BOOLEAN DEFAULT true)")
    for etf, trade_date in (etf_rows or []):
        conn.execute(
            "INSERT INTO prices_daily VALUES (?, ?, ?, 1.0)",
            [uuid.uuid4().hex[:16], etf, trade_date],
        )
    return conn


def test_sector_to_etf_covers_all_11_sectors():
    from health.sector_diagnostics import SECTOR_TO_ETF
    assert len(SECTOR_TO_ETF) == 11
    assert SECTOR_TO_ETF["Information Technology"] == "XLK"
    assert SECTOR_TO_ETF["Financials"] == "XLF"
    assert SECTOR_TO_ETF["Energy"] == "XLE"
    assert SECTOR_TO_ETF["Consumer Discretionary"] == "XLY"


def test_get_etf_coverage_returns_counts():
    from health.sector_diagnostics import get_etf_coverage
    conn = _make_conn([("XLK", "2026-05-01"), ("XLK", "2026-05-02"), ("XLF", "2026-05-01")])
    cov = get_etf_coverage(conn)
    assert cov["XLK"] == 2
    assert cov["XLF"] == 1
    assert cov.get("XLE", 0) == 0


def test_get_etf_coverage_empty_db():
    from health.sector_diagnostics import get_etf_coverage
    conn = _make_conn()
    assert get_etf_coverage(conn) == {}


def test_classify_missing_sector_when_none():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", None, {}) == "missing_sector_mapping"


def test_classify_missing_sector_when_empty_string():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "", {}) == "missing_sector_mapping"


def test_classify_missing_sector_when_unknown_sector():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "Some Unknown Sector", {}) == "missing_sector_mapping"


def test_classify_missing_etf_prices_when_no_coverage():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "Information Technology", {}) == "missing_sector_etf_prices"


def test_classify_missing_etf_prices_when_count_zero():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "Information Technology", {"XLK": 0}) == "missing_sector_etf_prices"


def test_classify_peer_cluster_only_when_etf_has_prices():
    from health.sector_diagnostics import classify_sector_cluster_row
    result = classify_sector_cluster_row("AAPL", "Information Technology", {"XLK": 50})
    assert result == "peer_cluster_only_no_etf_data"


def test_generate_sector_diagnostics_empty_when_no_rows():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    assert generate_sector_diagnostics(conn, []) == []


def test_generate_sector_diagnostics_skips_non_cluster_rows():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "missing_cik"}]
    assert generate_sector_diagnostics(conn, rows) == []


def test_generate_sector_diagnostics_peer_cluster_only_with_etf():
    from health.sector_diagnostics import generate_sector_diagnostics, SectorClusterDiag
    conn = _make_conn([("XLK", "2026-05-01")])
    conn.execute("INSERT INTO companies VALUES ('AAPL', 'Information Technology', true)")
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "sector_cluster_move"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert len(diags) == 1
    d = diags[0]
    assert d.ticker == "AAPL"
    assert d.sector == "Information Technology"
    assert d.etf_ticker == "XLK"
    assert d.etf_price_count == 1
    assert d.subcause == "peer_cluster_only_no_etf_data"


def test_generate_sector_diagnostics_missing_etf_prices():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    conn.execute("INSERT INTO companies VALUES ('AAPL', 'Information Technology', true)")
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "sector_cluster_move"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert diags[0].subcause == "missing_sector_etf_prices"


def test_generate_sector_diagnostics_no_sector():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    # Ticker not in companies — sector lookup returns None
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "sector_cluster_move"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert diags[0].subcause == "missing_sector_mapping"
