"""Tests for priority refresh queue builder."""
import csv as _csv
import duckdb
import pytest

from ingestion.priority_refresh import build_priority_queue, save_priority_queue


def _make_conn(rows: list[dict]) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE companies (
            ticker VARCHAR PRIMARY KEY,
            is_active BOOLEAN DEFAULT true,
            market_cap DOUBLE,
            last_financial_filing_date DATE,
            sector VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE prices_daily (
            ticker VARCHAR, trade_date DATE, close DOUBLE,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO companies VALUES (?, ?, ?, ?, ?)",
            [r["ticker"], r.get("is_active", True), r.get("market_cap"), r.get("filing_date"), r.get("sector")],
        )
        if r.get("price_date"):
            conn.execute(
                "INSERT INTO prices_daily VALUES (?, ?, 100.0)",
                [r["ticker"], r["price_date"]],
            )
    return conn


def test_no_prices_is_priority_1():
    conn = _make_conn([{"ticker": "AAAB"}])
    queue = build_priority_queue(conn, as_of_date="2026-05-03")
    assert len(queue) == 1
    assert queue[0]["ticker"] == "AAAB"
    assert queue[0]["priority"] == 1
    assert "no_prices" in queue[0]["reason"]


def test_stale_prices_is_priority_2():
    conn = _make_conn([{"ticker": "AAAB", "price_date": "2026-04-01"}])
    queue = build_priority_queue(conn, as_of_date="2026-05-03")
    assert queue[0]["priority"] == 2
    assert "stale_prices" in queue[0]["reason"]


def test_no_fundamentals_with_fresh_prices_is_priority_3():
    conn = _make_conn([{"ticker": "AAAB", "price_date": "2026-05-03"}])
    queue = build_priority_queue(conn, as_of_date="2026-05-03")
    assert queue[0]["priority"] == 3
    assert "no_fundamentals" in queue[0]["reason"]


def test_no_market_cap_only_is_priority_4():
    conn = _make_conn([{"ticker": "AAAB", "price_date": "2026-05-03", "filing_date": "2025-12-31"}])
    queue = build_priority_queue(conn, as_of_date="2026-05-03")
    assert queue[0]["priority"] == 4
    assert "no_market_cap" in queue[0]["reason"]


def test_complete_ticker_not_in_queue():
    conn = _make_conn([{
        "ticker": "FULL", "price_date": "2026-05-03",
        "filing_date": "2025-12-31", "market_cap": 1e11,
    }])
    queue = build_priority_queue(conn, as_of_date="2026-05-03")
    assert len(queue) == 0


def test_queue_sorted_by_priority():
    conn = _make_conn([
        {"ticker": "MISS", "price_date": "2026-04-01"},  # stale -> priority 2
        {"ticker": "NOPX"},                               # no prices -> priority 1
    ])
    queue = build_priority_queue(conn, as_of_date="2026-05-03")
    assert queue[0]["ticker"] == "NOPX"
    assert queue[1]["ticker"] == "MISS"


def test_max_tickers_limits_output():
    conn = _make_conn([{"ticker": f"T{i:03d}"} for i in range(200)])
    queue = build_priority_queue(conn, as_of_date="2026-05-03", max_tickers=50)
    assert len(queue) == 50


def test_save_priority_queue_csv(tmp_path):
    conn = _make_conn([{"ticker": "AAAB"}])
    queue = build_priority_queue(conn, as_of_date="2026-05-03")
    out_path = str(tmp_path / "queue.csv")
    save_priority_queue(queue, out_path)
    with open(out_path, newline="") as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAAB"
