"""Tests for per-ticker data freshness metrics."""
import datetime
import duckdb
import pytest

from health.data_freshness import TickerFreshness, compute_freshness, freshness_summary


def _make_conn(tickers: list[dict]) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE companies (
            ticker VARCHAR PRIMARY KEY,
            is_active BOOLEAN DEFAULT true,
            market_cap DOUBLE,
            last_financial_filing_date DATE,
            last_seen_at TIMESTAMP,
            sector VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE prices_daily (
            ticker VARCHAR, trade_date DATE, close DOUBLE,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    for t in tickers:
        conn.execute(
            "INSERT INTO companies (ticker, is_active, market_cap, last_financial_filing_date) VALUES (?, ?, ?, ?)",
            [t["ticker"], t.get("is_active", True), t.get("market_cap"), t.get("filing_date")],
        )
        if t.get("price_date"):
            conn.execute(
                "INSERT INTO prices_daily (ticker, trade_date, close) VALUES (?, ?, 100.0)",
                [t["ticker"], t["price_date"]],
            )
    return conn


def test_fresh_ticker():
    today = str(datetime.date.today())
    conn = _make_conn([{"ticker": "AAAB", "price_date": today, "filing_date": "2025-12-31", "market_cap": 1e11}])
    results = compute_freshness(conn, as_of_date=today)
    assert len(results) == 1
    r = results[0]
    assert r.ticker == "AAAB"
    assert r.has_prices is True
    assert r.price_age_days == 0
    assert r.has_fundamentals is True
    assert r.has_market_cap is True
    assert r.freshness_label == "fresh"


def test_missing_prices():
    conn = _make_conn([{"ticker": "AAAB"}])
    results = compute_freshness(conn, as_of_date="2026-05-03")
    assert results[0].has_prices is False
    assert results[0].price_age_days is None
    assert results[0].freshness_label == "missing"


def test_stale_prices():
    conn = _make_conn([{"ticker": "AAAB", "price_date": "2026-04-01"}])
    results = compute_freshness(conn, as_of_date="2026-05-03")
    r = results[0]
    assert r.has_prices is True
    assert r.price_age_days == 32
    assert r.freshness_label == "stale"


def test_no_fundamentals():
    conn = _make_conn([{"ticker": "AAAB", "price_date": "2026-05-03"}])
    results = compute_freshness(conn, as_of_date="2026-05-03")
    assert results[0].has_fundamentals is False
    assert results[0].filing_age_days is None


def test_inactive_tickers_excluded():
    conn = _make_conn([
        {"ticker": "ACTIVE", "price_date": "2026-05-03"},
        {"ticker": "DEAD", "is_active": False, "price_date": "2026-05-03"},
    ])
    results = compute_freshness(conn, as_of_date="2026-05-03")
    tickers = [r.ticker for r in results]
    assert "ACTIVE" in tickers
    assert "DEAD" not in tickers


def test_freshness_summary():
    conn = _make_conn([
        {"ticker": "A", "price_date": str(datetime.date.today()), "filing_date": "2025-12-31", "market_cap": 1e11},
        {"ticker": "B", "price_date": "2026-04-01"},
        {"ticker": "C"},
    ])
    results = compute_freshness(conn, as_of_date="2026-05-03")
    summary = freshness_summary(results)
    assert summary["total"] == 3
    assert summary["has_prices"] == 2
    assert summary["has_fundamentals"] == 1
    assert summary["has_market_cap"] == 1
    assert summary["missing"] == 1
    assert summary["stale"] == 1
    assert summary["fresh"] == 1
