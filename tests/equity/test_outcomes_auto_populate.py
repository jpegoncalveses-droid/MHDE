"""Tests for bulk forward-return auto-population."""
import duckdb
import pytest

from outcomes.tracker import populate_forward_returns


@pytest.fixture
def outcome_db():
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE candidate_outcomes (
            candidate_id VARCHAR PRIMARY KEY,
            run_id VARCHAR,
            ticker VARCHAR,
            as_of_date DATE,
            tier VARCHAR,
            total_score DOUBLE,
            reference_price DOUBLE,
            forward_return_1d DOUBLE,
            forward_return_3d DOUBLE,
            forward_return_5d DOUBLE,
            forward_return_10d DOUBLE,
            forward_return_20d DOUBLE,
            forward_return_60d DOUBLE
        )
    """)
    conn.execute("""
        CREATE TABLE prices_daily (
            ticker VARCHAR,
            trade_date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            adjusted_close DOUBLE,
            source VARCHAR,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    return conn


def test_forward_return_1d_populated(outcome_db):
    outcome_db.execute(
        "INSERT INTO candidate_outcomes "
        "(candidate_id, run_id, ticker, as_of_date, reference_price) "
        "VALUES ('c1', 'r1', 'AAAB', '2026-01-01', 100.0)"
    )
    outcome_db.execute(
        "INSERT INTO prices_daily (ticker, trade_date, close) VALUES ('AAAB', '2026-01-02', 105.0)"
    )
    populate_forward_returns(outcome_db, as_of_date="2026-01-10")
    row = outcome_db.execute(
        "SELECT forward_return_1d FROM candidate_outcomes WHERE candidate_id = 'c1'"
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert abs(row[0] - 0.05) < 0.001


def test_does_not_overwrite_existing_value(outcome_db):
    outcome_db.execute(
        "INSERT INTO candidate_outcomes "
        "(candidate_id, run_id, ticker, as_of_date, reference_price, forward_return_1d) "
        "VALUES ('c1', 'r1', 'AAAB', '2026-01-01', 100.0, 0.99)"
    )
    outcome_db.execute(
        "INSERT INTO prices_daily (ticker, trade_date, close) VALUES ('AAAB', '2026-01-02', 105.0)"
    )
    populate_forward_returns(outcome_db, as_of_date="2026-01-10")
    row = outcome_db.execute(
        "SELECT forward_return_1d FROM candidate_outcomes WHERE candidate_id = 'c1'"
    ).fetchone()
    assert abs(row[0] - 0.99) < 0.001


def test_skips_null_reference_price(outcome_db):
    outcome_db.execute(
        "INSERT INTO candidate_outcomes "
        "(candidate_id, run_id, ticker, as_of_date, reference_price) "
        "VALUES ('c1', 'r1', 'AAAB', '2026-01-01', NULL)"
    )
    outcome_db.execute(
        "INSERT INTO prices_daily (ticker, trade_date, close) VALUES ('AAAB', '2026-01-02', 105.0)"
    )
    populate_forward_returns(outcome_db, as_of_date="2026-01-10")
    row = outcome_db.execute(
        "SELECT forward_return_1d FROM candidate_outcomes WHERE candidate_id = 'c1'"
    ).fetchone()
    assert row[0] is None


def test_window_not_mature_yet_stays_null(outcome_db):
    # as_of_date is set to the same day as the outcome — 60d window not mature
    outcome_db.execute(
        "INSERT INTO candidate_outcomes "
        "(candidate_id, run_id, ticker, as_of_date, reference_price) "
        "VALUES ('c1', 'r1', 'AAAB', '2026-01-01', 100.0)"
    )
    outcome_db.execute(
        "INSERT INTO prices_daily (ticker, trade_date, close) VALUES ('AAAB', '2026-01-02', 105.0)"
    )
    # as_of_date is 2026-01-03 — only 2 days after scoring, 60d window not mature
    populate_forward_returns(outcome_db, as_of_date="2026-01-03")
    row = outcome_db.execute(
        "SELECT forward_return_60d FROM candidate_outcomes WHERE candidate_id = 'c1'"
    ).fetchone()
    assert row[0] is None


def test_returns_integer(outcome_db):
    result = populate_forward_returns(outcome_db, as_of_date="2026-01-10")
    assert isinstance(result, int)
