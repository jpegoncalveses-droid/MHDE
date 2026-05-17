"""Tests for migration v12: sentiment tables (F&G + funding aggregate + universe).

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Sentiment ingestion".
"""
import duckdb
import pytest

from storage.db import get_connection
from storage.migrations import _CURRENT_VERSION, run_migrations


def _columns(conn, table):
    return {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    }


def _tables(conn):
    return {r[0] for r in conn.execute("SHOW TABLES").fetchall()}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "mhde_v12_test.duckdb")


def test_current_version_is_12():
    assert _CURRENT_VERSION == 12


def test_v12_creates_sentiment_fear_greed(db_path):
    conn = get_connection(db_path)
    run_migrations(conn)
    assert "sentiment_fear_greed" in _tables(conn)
    cols = _columns(conn, "sentiment_fear_greed")
    assert {"date", "value", "value_classification", "source", "ingested_at"}.issubset(cols)


def test_v12_creates_sentiment_funding_universe(db_path):
    conn = get_connection(db_path)
    run_migrations(conn)
    assert "sentiment_funding_universe" in _tables(conn)
    cols = _columns(conn, "sentiment_funding_universe")
    assert {"symbol", "rank_by_volume", "quote_volume_24mo", "snapshot_at"}.issubset(cols)


def test_v12_creates_sentiment_funding_aggregate(db_path):
    conn = get_connection(db_path)
    run_migrations(conn)
    assert "sentiment_funding_aggregate" in _tables(conn)
    cols = _columns(conn, "sentiment_funding_aggregate")
    assert {
        "trade_date", "volume_weighted_funding_rate", "n_constituents",
        "computed_at",
    }.issubset(cols)


def test_v12_marks_schema_version(db_path):
    conn = get_connection(db_path)
    run_migrations(conn)
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()}
    assert 12 in versions


def test_v12_is_idempotent(db_path):
    conn = get_connection(db_path)
    run_migrations(conn)
    tables_first = _tables(conn)
    rows_first = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    run_migrations(conn)
    assert _tables(conn) == tables_first
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == rows_first


def test_v12_pk_constraints(db_path):
    """Insert duplicate primary keys → second should fail or be a no-op."""
    conn = get_connection(db_path)
    run_migrations(conn)
    # sentiment_fear_greed: PK = date
    conn.execute(
        "INSERT INTO sentiment_fear_greed (date, value, value_classification, source) "
        "VALUES ('2025-01-01', 50, 'Neutral', 'alternative.me')"
    )
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO sentiment_fear_greed (date, value, value_classification, source) "
            "VALUES ('2025-01-01', 60, 'Greed', 'alternative.me')"
        )

    # sentiment_funding_universe: PK = symbol
    conn.execute(
        "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume, quote_volume_24mo) "
        "VALUES ('BTCUSDT', 1, 1e12)"
    )
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume, quote_volume_24mo) "
            "VALUES ('BTCUSDT', 2, 9e11)"
        )

    # sentiment_funding_aggregate: PK = trade_date
    conn.execute(
        "INSERT INTO sentiment_funding_aggregate (trade_date, volume_weighted_funding_rate, n_constituents) "
        "VALUES ('2025-01-01', 0.0001, 20)"
    )
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO sentiment_funding_aggregate (trade_date, volume_weighted_funding_rate, n_constituents) "
            "VALUES ('2025-01-01', 0.0002, 20)"
        )
