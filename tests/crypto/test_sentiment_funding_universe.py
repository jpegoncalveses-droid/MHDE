"""Tests for crypto/ingestion/sentiment_funding_universe.py.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Sentiment
ingestion": top-20 USDT-M perps by 24mo quote volume, excluding stables
and wrapped tokens.
"""
from unittest.mock import MagicMock

import pytest

from crypto.ingestion.sentiment_funding_universe import (
    SENTIMENT_UNIVERSE_SIZE,
    build_sentiment_funding_universe,
)
from storage.db import get_connection
from storage.migrations import run_migrations


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "mhde.duckdb"))
    run_migrations(c)
    return c


def test_universe_size_constant_is_20():
    assert SENTIMENT_UNIVERSE_SIZE == 20


def test_build_writes_top_n_excluding_stables_and_wrapped(conn):
    client = MagicMock()
    client.fetch_futures_exchange_info.return_value = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
        {"symbol": "USDCUSDT", "base_asset": "USDC"},  # excluded (stablecoin)
        {"symbol": "WBTCUSDT", "base_asset": "WBTC"},  # excluded (wrapped)
        {"symbol": "AAVEUSDT", "base_asset": "AAVE"},
    ]
    client.fetch_24hr_tickers.return_value = [
        {"symbol": "BTCUSDT", "quote_volume": 1e10},
        {"symbol": "ETHUSDT", "quote_volume": 9e9},
        {"symbol": "USDCUSDT", "quote_volume": 5e9},   # filtered out
        {"symbol": "WBTCUSDT", "quote_volume": 4e9},    # filtered out
        {"symbol": "AAVEUSDT", "quote_volume": 1e8},
    ]
    selected = build_sentiment_funding_universe(conn, client=client, top_n=20)
    assert selected == ["BTCUSDT", "ETHUSDT", "AAVEUSDT"]

    rows = conn.execute(
        "SELECT symbol, rank_by_volume, quote_volume_24mo "
        "FROM sentiment_funding_universe ORDER BY rank_by_volume"
    ).fetchall()
    assert [r[0] for r in rows] == ["BTCUSDT", "ETHUSDT", "AAVEUSDT"]
    assert rows[0][1] == 1
    assert rows[0][2] == 1e10


def test_build_respects_top_n_truncation(conn):
    client = MagicMock()
    client.fetch_futures_exchange_info.return_value = [
        {"symbol": f"COIN{i}USDT", "base_asset": f"COIN{i}"} for i in range(30)
    ]
    client.fetch_24hr_tickers.return_value = [
        {"symbol": f"COIN{i}USDT", "quote_volume": float(1e10 - i * 1e8)}
        for i in range(30)
    ]
    selected = build_sentiment_funding_universe(conn, client=client, top_n=20)
    assert len(selected) == 20
    # Top one wins
    assert selected[0] == "COIN0USDT"


def test_build_is_idempotent(conn):
    """Re-running with same input gives the same snapshot."""
    client = MagicMock()
    client.fetch_futures_exchange_info.return_value = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
    ]
    client.fetch_24hr_tickers.return_value = [
        {"symbol": "BTCUSDT", "quote_volume": 1e10},
    ]
    selected1 = build_sentiment_funding_universe(conn, client=client, top_n=20)
    selected2 = build_sentiment_funding_universe(conn, client=client, top_n=20)
    assert selected1 == selected2
    n = conn.execute("SELECT COUNT(*) FROM sentiment_funding_universe").fetchone()[0]
    assert n == 1  # not duplicated
