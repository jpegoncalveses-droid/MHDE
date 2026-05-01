from __future__ import annotations

import pytest

from storage.db import get_connection, init_schema, get_table_names, table_exists, row_count


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_schema_creates_all_tables(conn):
    tables = get_table_names(conn)
    required = [
        "companies", "source_runs", "filings", "fundamentals_raw", "fundamentals_features",
        "prices_daily", "macro_series", "short_interest", "events", "features", "scores",
        "hypotheses", "rejections", "candidate_outcomes", "backtest_runs", "model_runs",
        "llm_runs", "alerts", "health_checks", "schema_version",
    ]
    for t in required:
        assert t in tables, f"Missing table: {t}"


def test_paper_trades_table_not_created(conn):
    tables = get_table_names(conn)
    assert "paper_trades" not in tables, "paper_trades must not exist"


def test_table_exists(conn):
    assert table_exists(conn, "companies")
    assert not table_exists(conn, "nonexistent_table")


def test_row_count_empty(conn):
    assert row_count(conn, "companies") == 0


def test_companies_insert(conn):
    conn.execute(
        "INSERT INTO companies (ticker, company_name) VALUES ('TEST', 'Test Corp')"
    )
    assert row_count(conn, "companies") == 1


def test_candidate_outcomes_schema(conn):
    conn.execute(
        """
        INSERT INTO candidate_outcomes
            (candidate_id, run_id, ticker, as_of_date, tier, total_score)
        VALUES ('abc123', 'run001', 'TEST', '2026-01-01', 'A', 80.0)
        """
    )
    rows = conn.execute("SELECT candidate_id, tier FROM candidate_outcomes").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "A"


def test_candidate_outcomes_unique_constraint(conn):
    conn.execute(
        "INSERT INTO candidate_outcomes (candidate_id, run_id, ticker, as_of_date, tier, total_score) VALUES ('a', 'r1', 'TICK', '2026-01-01', 'B', 60)"
    )
    conn.execute(
        "INSERT INTO candidate_outcomes (candidate_id, run_id, ticker, as_of_date, tier, total_score) VALUES ('b', 'r1', 'TICK', '2026-01-01', 'B', 60) ON CONFLICT (run_id, ticker) DO NOTHING"
    )
    assert row_count(conn, "candidate_outcomes") == 1
