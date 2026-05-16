"""Tests for crypto/sentiment/funding_aggregate.py — daily volume-weighted
funding rate across the sentiment funding universe.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Composite
sentiment score" (funding_aggregate is one input to the future composite).
"""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from crypto.sentiment.funding_aggregate import (
    compute_daily_aggregate,
    persist_aggregate,
    rebuild_aggregate,
)
from storage.db import get_connection
from storage.migrations import run_migrations


@pytest.fixture
def conn(tmp_path):
    from crypto.schema import create_all_tables
    c = get_connection(str(tmp_path / "mhde.duckdb"))
    run_migrations(c)
    create_all_tables(c)  # crypto-specific tables for inserts below
    return c


def test_compute_daily_aggregate_volume_weighted():
    """Two symbols, one day. Weighted mean of funding by daily quote volume."""
    rates = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "daily_funding_rate": 0.0001},
        {"symbol": "ETHUSDT", "trade_date": date(2025, 1, 1), "daily_funding_rate": 0.0002},
    ])
    volumes = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "quote_volume": 1000.0},
        {"symbol": "ETHUSDT", "trade_date": date(2025, 1, 1), "quote_volume": 1000.0},
    ])
    out = compute_daily_aggregate(rates, volumes)
    # (0.0001 * 1000 + 0.0002 * 1000) / 2000 = 0.00015
    assert len(out) == 1
    row = out.iloc[0]
    assert row["trade_date"] == date(2025, 1, 1)
    assert abs(row["volume_weighted_funding_rate"] - 0.00015) < 1e-9
    assert row["n_constituents"] == 2


def test_compute_daily_aggregate_unequal_weights():
    """BTC 10x ETH volume → BTC dominates the average."""
    rates = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "daily_funding_rate": 0.0001},
        {"symbol": "ETHUSDT", "trade_date": date(2025, 1, 1), "daily_funding_rate": 0.001},
    ])
    volumes = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "quote_volume": 10000.0},
        {"symbol": "ETHUSDT", "trade_date": date(2025, 1, 1), "quote_volume": 1000.0},
    ])
    out = compute_daily_aggregate(rates, volumes)
    expected = (0.0001 * 10000 + 0.001 * 1000) / 11000
    assert abs(out.iloc[0]["volume_weighted_funding_rate"] - expected) < 1e-9


def test_compute_daily_aggregate_skips_days_without_volume():
    """Day with funding but no volume data is dropped."""
    rates = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "daily_funding_rate": 0.0001},
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 2), "daily_funding_rate": 0.0002},
    ])
    volumes = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "quote_volume": 1000.0},
        # No day 2 volume row
    ])
    out = compute_daily_aggregate(rates, volumes)
    assert len(out) == 1
    assert out.iloc[0]["trade_date"] == date(2025, 1, 1)


def test_compute_daily_aggregate_handles_zero_volume_safely():
    """A symbol with zero volume on day t doesn't NaN-poison that day."""
    rates = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "daily_funding_rate": 0.0001},
        {"symbol": "ETHUSDT", "trade_date": date(2025, 1, 1), "daily_funding_rate": 0.0002},
    ])
    volumes = pd.DataFrame([
        {"symbol": "BTCUSDT", "trade_date": date(2025, 1, 1), "quote_volume": 1000.0},
        {"symbol": "ETHUSDT", "trade_date": date(2025, 1, 1), "quote_volume": 0.0},
    ])
    out = compute_daily_aggregate(rates, volumes)
    # ETH zero weight → average = BTC's rate
    assert abs(out.iloc[0]["volume_weighted_funding_rate"] - 0.0001) < 1e-9


def test_persist_aggregate_upserts(conn):
    df = pd.DataFrame([
        {"trade_date": date(2025, 1, 1),
         "volume_weighted_funding_rate": 0.0001, "n_constituents": 20},
        {"trade_date": date(2025, 1, 2),
         "volume_weighted_funding_rate": 0.0002, "n_constituents": 20},
    ])
    n = persist_aggregate(conn, df)
    assert n == 2
    # Re-run with a corrected value for day 1
    df2 = pd.DataFrame([
        {"trade_date": date(2025, 1, 1),
         "volume_weighted_funding_rate": 0.0003, "n_constituents": 19},
    ])
    persist_aggregate(conn, df2)
    rows = conn.execute(
        "SELECT trade_date, volume_weighted_funding_rate, n_constituents "
        "FROM sentiment_funding_aggregate ORDER BY trade_date"
    ).fetchall()
    assert rows == [
        (date(2025, 1, 1), 0.0003, 19),
        (date(2025, 1, 2), 0.0002, 20),
    ]


def test_rebuild_aggregate_integrates_db_data(conn):
    """End-to-end against in-memory DB: insert universe + rates + volumes,
    rebuild aggregate, expect rows in sentiment_funding_aggregate.
    """
    conn.execute(
        "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume) "
        "VALUES ('BTCUSDT', 1), ('ETHUSDT', 2)"
    )
    # Funding rate: 3 settlements/day per symbol on 2025-01-01
    base = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    for sym, rate in [("BTCUSDT", 0.0001), ("ETHUSDT", 0.0002)]:
        for h in (0, 8, 16):
            conn.execute(
                "INSERT INTO crypto_funding_rates "
                "(symbol, funding_time, funding_rate, mark_price) "
                "VALUES (?, ?, ?, ?)",
                [sym, base + timedelta(hours=h), rate, 0.0],
            )
    # Daily volume
    conn.execute(
        "INSERT INTO crypto_prices_daily "
        "(symbol, trade_date, open, high, low, close, volume, trades, taker_buy_volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["BTCUSDT", date(2025, 1, 1), 1, 1, 1, 1, 1000.0, 1, 1],
    )
    conn.execute(
        "INSERT INTO crypto_prices_daily "
        "(symbol, trade_date, open, high, low, close, volume, trades, taker_buy_volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["ETHUSDT", date(2025, 1, 1), 1, 1, 1, 1, 1000.0, 1, 1],
    )

    n = rebuild_aggregate(conn)
    assert n == 1
    row = conn.execute(
        "SELECT trade_date, volume_weighted_funding_rate, n_constituents "
        "FROM sentiment_funding_aggregate"
    ).fetchone()
    assert row[0] == date(2025, 1, 1)
    # Each symbol sums 3 funding events → BTC daily = 0.0003, ETH = 0.0006.
    # Equal weights → avg = 0.00045
    assert abs(row[1] - 0.00045) < 1e-9
    assert row[2] == 2
