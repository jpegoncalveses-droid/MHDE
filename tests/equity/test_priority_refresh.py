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


def test_price_only_scored_miss_true_miss_is_p1():
    conn = _make_conn([{
        "ticker": "CTRA", "price_date": "2026-05-03",
        "filing_date": "2025-12-31", "market_cap": 1e11,
    }])
    queue = build_priority_queue(
        conn, as_of_date="2026-05-03",
        price_only_p1_tickers={"CTRA"},
    )
    assert len(queue) == 1
    assert queue[0]["ticker"] == "CTRA"
    assert queue[0]["priority"] == 1
    assert "price_only_scored_miss" in queue[0]["reason"]


def test_price_only_scored_miss_near_threshold_is_p2():
    conn = _make_conn([{
        "ticker": "CTRA", "price_date": "2026-05-03",
        "filing_date": "2025-12-31", "market_cap": 1e11,
    }])
    queue = build_priority_queue(
        conn, as_of_date="2026-05-03",
        price_only_p2_tickers={"CTRA"},
    )
    assert len(queue) == 1
    assert queue[0]["priority"] == 2
    assert "price_only_scored_miss" in queue[0]["reason"]


def test_price_only_p1_beats_p2_for_same_ticker():
    conn = _make_conn([{
        "ticker": "CTRA", "price_date": "2026-05-03",
        "filing_date": "2025-12-31", "market_cap": 1e11,
    }])
    queue = build_priority_queue(
        conn, as_of_date="2026-05-03",
        price_only_p1_tickers={"CTRA"},
        price_only_p2_tickers={"CTRA"},
    )
    assert queue[0]["priority"] == 1


def test_legacy_price_only_tickers_treated_as_p1():
    conn = _make_conn([{
        "ticker": "FULL", "price_date": "2026-05-03",
        "filing_date": "2025-12-31", "market_cap": 1e11,
    }])
    queue = build_priority_queue(
        conn, as_of_date="2026-05-03",
        price_only_tickers={"FULL"},
    )
    assert queue[0]["priority"] == 1
    assert "price_only_scored_miss" in queue[0]["reason"]


def test_no_scoring_changes_in_queue_builder():
    import inspect
    import ingestion.priority_refresh as _mod
    src = inspect.getsource(_mod)
    for bad in ("tier", "llm", "openai", "anthropic", "feature_flag"):
        assert bad not in src.lower(), f"prohibited term '{bad}' found in priority_refresh.py"


def test_polygon_fundamentals_missing_enters_queue_at_p2():
    conn = _make_conn([{
        "ticker": "DDOG", "price_date": "2026-05-03",
        "filing_date": "2026-02-18",
    }])
    queue = build_priority_queue(
        conn, as_of_date="2026-05-04",
        polygon_missing_tickers={"DDOG"},
    )
    assert len(queue) == 1
    assert queue[0]["ticker"] == "DDOG"
    assert queue[0]["priority"] == 2
    assert "polygon_fundamentals_missing_miss" in queue[0]["reason"]


def test_polygon_missing_with_no_market_cap_takes_higher_priority():
    conn = _make_conn([{
        "ticker": "RDDT", "price_date": "2026-05-03",
        "filing_date": "2026-05-01",
    }])
    queue = build_priority_queue(
        conn, as_of_date="2026-05-04",
        polygon_missing_tickers={"RDDT"},
    )
    assert queue[0]["priority"] == 2
    assert "no_market_cap" in queue[0]["reason"]
    assert "polygon_fundamentals_missing_miss" in queue[0]["reason"]


def test_polygon_missing_complete_ticker_still_enters_queue():
    conn = _make_conn([{
        "ticker": "NET", "price_date": "2026-05-03",
        "filing_date": "2026-02-26", "market_cap": 1e11,
    }])
    queue = build_priority_queue(
        conn, as_of_date="2026-05-04",
        polygon_missing_tickers={"NET"},
    )
    assert len(queue) == 1
    assert queue[0]["priority"] == 2
    assert "polygon_fundamentals_missing_miss" in queue[0]["reason"]
