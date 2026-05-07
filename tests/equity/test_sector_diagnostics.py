"""Tests for sector cluster diagnostics engine with ETF attribution."""
from __future__ import annotations

import uuid

import duckdb
import pytest

from health.sector_diagnostics import (
    SECTOR_TO_ETF,
    SectorClusterDiag,
    classify_sector_cluster_row,
    compute_etf_window_return,
    generate_sector_diagnostics,
    get_etf_coverage,
)


def _make_conn(etf_rows: list[tuple] = None, companies: list[tuple] = None) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE prices_daily ("
        "id VARCHAR PRIMARY KEY, ticker VARCHAR, trade_date DATE, close DOUBLE, "
        "source VARCHAR DEFAULT 'polygon')"
    )
    conn.execute(
        "CREATE TABLE companies ("
        "ticker VARCHAR PRIMARY KEY, sector VARCHAR, is_active BOOLEAN DEFAULT true)"
    )
    for row in (etf_rows or []):
        if len(row) == 4:
            etf, trade_date, close, source = row
        else:
            etf, trade_date, close = row
            source = "polygon"
        conn.execute(
            "INSERT INTO prices_daily VALUES (?, ?, ?, ?, ?)",
            [uuid.uuid4().hex[:16], etf, trade_date, close, source],
        )
    for ticker, sector in (companies or []):
        conn.execute(
            "INSERT INTO companies VALUES (?, ?, true)",
            [ticker, sector],
        )
    return conn


# ── SECTOR_TO_ETF mapping ────────────────────────────────────────────────

def test_sector_to_etf_covers_all_11_sectors():
    assert len(SECTOR_TO_ETF) == 11
    assert SECTOR_TO_ETF["Information Technology"] == "XLK"
    assert SECTOR_TO_ETF["Financials"] == "XLF"
    assert SECTOR_TO_ETF["Energy"] == "XLE"
    assert SECTOR_TO_ETF["Consumer Discretionary"] == "XLY"


# ── get_etf_coverage ─────────────────────────────────────────────────────

def test_get_etf_coverage_returns_counts():
    conn = _make_conn([("XLK", "2026-05-01", 1.0), ("XLK", "2026-05-02", 1.0), ("XLF", "2026-05-01", 1.0)])
    cov = get_etf_coverage(conn)
    assert cov["XLK"] == 2
    assert cov["XLF"] == 1
    assert cov.get("XLE", 0) == 0


def test_get_etf_coverage_empty_db():
    conn = _make_conn()
    assert get_etf_coverage(conn) == {}


# ── classify_sector_cluster_row (legacy 3-arg + new 5-arg) ───────────────

def test_classify_missing_sector_when_none():
    assert classify_sector_cluster_row("AAPL", None, {}) == "missing_sector_mapping"


def test_classify_missing_sector_when_empty_string():
    assert classify_sector_cluster_row("AAPL", "", {}) == "missing_sector_mapping"


def test_classify_missing_sector_when_unknown_sector():
    assert classify_sector_cluster_row("AAPL", "Some Unknown Sector", {}) == "missing_sector_mapping"


def test_classify_missing_etf_prices_when_no_coverage():
    assert classify_sector_cluster_row("AAPL", "Information Technology", {}) == "missing_sector_etf_prices"


def test_classify_missing_etf_prices_when_count_zero():
    assert classify_sector_cluster_row("AAPL", "Information Technology", {"XLK": 0}) == "missing_sector_etf_prices"


def test_classify_peer_cluster_only_when_etf_has_prices_but_no_return():
    result = classify_sector_cluster_row("AAPL", "Information Technology", {"XLK": 50})
    assert result == "peer_cluster_only_no_etf_data"


# ── New ETF-aware classification ─────────────────────────────────────────

def test_classify_sector_etf_confirmed():
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 50},
        etf_return=0.03, ticker_return=0.04,
    )
    assert result == "sector_etf_confirmed"


def test_classify_ticker_outperformed_sector():
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 50},
        etf_return=0.02, ticker_return=0.08,
    )
    assert result == "ticker_outperformed_sector"


def test_classify_sector_signal_underweighted():
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 50},
        etf_return=0.05, ticker_return=0.075,
    )
    assert result == "sector_etf_confirmed"


def test_classify_etf_negative_ticker_negative_confirmed():
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 50},
        etf_return=-0.03, ticker_return=-0.04,
    )
    assert result == "sector_etf_confirmed"


def test_classify_peer_only_when_etf_return_none():
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 50},
        etf_return=None, ticker_return=0.05,
    )
    assert result == "peer_cluster_only_no_etf_data"


def test_classify_peer_only_when_ticker_return_none():
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 50},
        etf_return=0.02, ticker_return=None,
    )
    assert result == "peer_cluster_only_no_etf_data"


def test_classify_etf_immaterial_stays_peer_only():
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 50},
        etf_return=0.005, ticker_return=0.005,
    )
    assert result == "peer_cluster_only_no_etf_data"


# ── compute_etf_window_return ────────────────────────────────────────────

def test_compute_etf_window_return_price_based():
    conn = _make_conn([
        ("XLK", "2026-04-28", 100.0),
        ("XLK", "2026-04-29", 102.0),
        ("XLK", "2026-04-30", 104.0),
        ("XLK", "2026-05-01", 105.0),
    ])
    ret = compute_etf_window_return(conn, "XLK", "2026-05-01", 1)
    assert ret is not None
    assert abs(ret - (105.0 - 104.0) / 104.0) < 1e-5


def test_compute_etf_window_return_price_5d():
    conn = _make_conn([
        ("XLK", "2026-04-24", 100.0),
        ("XLK", "2026-04-25", 101.0),
        ("XLK", "2026-04-28", 102.0),
        ("XLK", "2026-04-29", 103.0),
        ("XLK", "2026-04-30", 104.0),
        ("XLK", "2026-05-01", 110.0),
    ])
    ret = compute_etf_window_return(conn, "XLK", "2026-05-01", 5)
    assert ret is not None
    assert abs(ret - (110.0 - 101.0) / 101.0) < 1e-5


def test_compute_etf_return_from_daily_returns_1d():
    conn = _make_conn([
        ("XLK", "2026-04-30", 0.005, "polygon_sector_etf"),
        ("XLK", "2026-05-01", 0.012, "polygon_sector_etf"),
    ])
    ret = compute_etf_window_return(conn, "XLK", "2026-05-01", 1)
    assert ret is not None
    assert abs(ret - 0.012) < 1e-6


def test_compute_etf_return_from_daily_returns_5d_compounds():
    conn = _make_conn([
        ("XLK", "2026-04-25", 0.01, "polygon_sector_etf"),
        ("XLK", "2026-04-28", 0.02, "polygon_sector_etf"),
        ("XLK", "2026-04-29", -0.01, "polygon_sector_etf"),
        ("XLK", "2026-04-30", 0.015, "polygon_sector_etf"),
        ("XLK", "2026-05-01", 0.005, "polygon_sector_etf"),
    ])
    ret = compute_etf_window_return(conn, "XLK", "2026-05-01", 5)
    assert ret is not None
    expected = (1.02 * 0.99 * 1.015 * 1.005) - 1.0
    assert abs(ret - expected) < 1e-5


def test_compute_etf_window_return_missing_data():
    conn = _make_conn()
    ret = compute_etf_window_return(conn, "XLK", "2026-05-01", 1)
    assert ret is None


def test_compute_etf_window_return_single_return_row():
    conn = _make_conn([("XLK", "2026-05-01", 0.01, "polygon_sector_etf")])
    ret = compute_etf_window_return(conn, "XLK", "2026-05-01", 1)
    assert ret is not None
    assert abs(ret - 0.01) < 1e-6


# ── generate_sector_diagnostics ──────────────────────────────────────────

def test_generate_sector_diagnostics_empty_when_no_rows():
    conn = _make_conn()
    assert generate_sector_diagnostics(conn, []) == []


def test_generate_sector_diagnostics_skips_non_cluster_rows():
    conn = _make_conn()
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "missing_cik"}]
    assert generate_sector_diagnostics(conn, rows) == []


def test_generate_sector_diagnostics_with_etf_returns():
    conn = _make_conn(
        etf_rows=[
            ("XLK", "2026-04-30", 0.005, "polygon_sector_etf"),
            ("XLK", "2026-05-01", 0.02, "polygon_sector_etf"),
        ],
        companies=[("AAPL", "Information Technology")],
    )
    rows = [
        {"ticker": "AAPL", "event_date": "2026-05-01",
         "enriched_root_cause": "sector_cluster_move",
         "return_value": "5.0", "window_days": "1"},
    ]
    diags = generate_sector_diagnostics(conn, rows)
    assert len(diags) == 1
    d = diags[0]
    assert d.ticker == "AAPL"
    assert d.sector == "Information Technology"
    assert d.etf_ticker == "XLK"
    assert d.etf_return is not None
    assert abs(d.etf_return - 0.02) < 1e-6
    assert d.ticker_return is not None
    assert abs(d.ticker_return - 0.05) < 1e-6
    assert d.relative_return is not None
    assert d.subcause == "ticker_outperformed_sector"


def test_generate_sector_diagnostics_missing_etf_prices():
    conn = _make_conn(companies=[("AAPL", "Information Technology")])
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01",
             "enriched_root_cause": "sector_cluster_move",
             "return_value": "5.0", "window_days": "1"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert diags[0].subcause == "missing_sector_etf_prices"


def test_generate_sector_diagnostics_no_sector():
    conn = _make_conn()
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01",
             "enriched_root_cause": "sector_cluster_move",
             "return_value": "5.0", "window_days": "1"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert diags[0].subcause == "missing_sector_mapping"


def test_generate_sector_diagnostics_has_suggested_fix():
    conn = _make_conn(companies=[("AAPL", "Information Technology")])
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01",
             "enriched_root_cause": "sector_cluster_move",
             "return_value": "5.0", "window_days": "1"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert diags[0].suggested_fix != ""


def test_generate_sector_diagnostics_peer_count():
    conn = _make_conn(companies=[
        ("AAPL", "Information Technology"),
        ("MSFT", "Information Technology"),
        ("GOOG", "Information Technology"),
    ])
    rows = [
        {"ticker": "AAPL", "event_date": "2026-05-01",
         "enriched_root_cause": "sector_cluster_move",
         "return_value": "5.0", "window_days": "1"},
        {"ticker": "MSFT", "event_date": "2026-05-01",
         "enriched_root_cause": "sector_cluster_move",
         "return_value": "4.0", "window_days": "1"},
        {"ticker": "GOOG", "event_date": "2026-05-01",
         "enriched_root_cause": "sector_cluster_move",
         "return_value": "6.0", "window_days": "1"},
    ]
    diags = generate_sector_diagnostics(conn, rows)
    for d in diags:
        assert d.peer_cluster_count == 3


# ── No production scoring mutation ───────────────────────────────────────

def test_no_scoring_mutation_in_diagnostics():
    import inspect
    import health.sector_diagnostics as mod
    src = inspect.getsource(mod)
    for bad in ("feature_flag", "FeatureFlag", "openai", "anthropic"):
        assert bad.lower() not in src.lower(), f"Prohibited term '{bad}' in sector_diagnostics"
