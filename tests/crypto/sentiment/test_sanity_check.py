"""Tests for crypto/sentiment/sanity_check.py.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Week 1"
deliverables — row counts, gap analysis, value distributions.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from crypto.sentiment.sanity_check import (
    SanityReport,
    Thresholds,
    fng_gap_days,
    fng_row_count,
    fng_value_distribution,
    funding_aggregate_row_count,
    funding_universe_size,
    is_clean,
)
from storage.db import get_connection
from storage.migrations import run_migrations


@pytest.fixture
def conn(tmp_path):
    from crypto.schema import create_all_tables
    c = get_connection(str(tmp_path / "mhde.duckdb"))
    run_migrations(c)
    create_all_tables(c)
    return c


def _insert_fng(conn, day, value):
    conn.execute(
        "INSERT INTO sentiment_fear_greed (date, value, value_classification, source) "
        "VALUES (?, ?, ?, ?)",
        [day, value, "Neutral", "alternative.me"],
    )


def test_fng_row_count_and_distribution(conn):
    base = date(2025, 1, 1)
    for i, v in enumerate([10, 30, 50, 70, 90]):
        _insert_fng(conn, base + timedelta(days=i), v)
    assert fng_row_count(conn) == 5
    dist = fng_value_distribution(conn)
    assert dist["min"] == 10
    assert dist["max"] == 90
    assert abs(dist["mean"] - 50.0) < 1e-9


def test_fng_gap_days_detects_missing_day(conn):
    base = date(2025, 1, 1)
    _insert_fng(conn, base, 50)
    _insert_fng(conn, base + timedelta(days=1), 50)
    _insert_fng(conn, base + timedelta(days=3), 50)  # gap at day 2
    gaps = fng_gap_days(conn)
    assert gaps == [base + timedelta(days=2)]


def test_fng_gap_days_returns_empty_when_continuous(conn):
    base = date(2025, 1, 1)
    for i in range(5):
        _insert_fng(conn, base + timedelta(days=i), 50)
    assert fng_gap_days(conn) == []


def test_funding_universe_size(conn):
    conn.execute(
        "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume) "
        "VALUES ('BTCUSDT', 1), ('ETHUSDT', 2)"
    )
    assert funding_universe_size(conn) == 2


def test_funding_aggregate_row_count(conn):
    conn.execute(
        "INSERT INTO sentiment_funding_aggregate "
        "(trade_date, volume_weighted_funding_rate, n_constituents) "
        "VALUES (?, ?, ?), (?, ?, ?)",
        [date(2025, 1, 1), 0.0001, 20, date(2025, 1, 2), 0.0002, 20],
    )
    assert funding_aggregate_row_count(conn) == 2


def test_is_clean_passes_when_all_metrics_in_range(conn):
    base = date(2024, 5, 25)
    # 24 months ~ 730 days; insert 100 continuous, well within F&G value range.
    for i in range(100):
        _insert_fng(conn, base + timedelta(days=i), 50)
    for i in range(20):
        conn.execute(
            "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume) "
            "VALUES (?, ?)",
            [f"SYM{i}USDT", i + 1],
        )
    for i in range(50):
        conn.execute(
            "INSERT INTO sentiment_funding_aggregate "
            "(trade_date, volume_weighted_funding_rate, n_constituents) "
            "VALUES (?, ?, ?)",
            [base + timedelta(days=i), 0.0001, 20],
        )
    report = SanityReport.collect(
        conn, Thresholds(min_fng_rows=50, min_universe_size=20, min_aggregate_rows=30),
    )
    assert is_clean(report)


def test_is_clean_fails_on_undersized_universe(conn):
    """Universe < min_universe_size → not clean."""
    conn.execute(
        "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume) "
        "VALUES ('BTCUSDT', 1), ('ETHUSDT', 2)"
    )
    report = SanityReport.collect(conn, Thresholds(min_universe_size=20))
    assert not is_clean(report)


def test_is_clean_fails_on_invalid_fng_values(conn):
    """F&G value out of [0, 100] → not clean."""
    _insert_fng(conn, date(2025, 1, 1), 150)  # impossible value
    report = SanityReport.collect(conn, Thresholds(min_fng_rows=0, min_universe_size=0, min_aggregate_rows=0))
    assert not is_clean(report)
