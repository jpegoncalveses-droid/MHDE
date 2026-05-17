"""Tests for crypto/ingestion/backfill_sentiment_funding.py.

Thin orchestration wrapper: reads sentiment_funding_universe symbols,
calls existing backfill_funding (idempotent) for each.
"""
from datetime import date

import pytest

from crypto.ingestion.backfill_sentiment_funding import (
    backfill_sentiment_funding,
    sentiment_universe_symbols,
)
from storage.db import get_connection
from storage.migrations import run_migrations


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "mhde.duckdb"))
    run_migrations(c)
    return c


def test_sentiment_universe_symbols_reads_table_in_rank_order(conn):
    conn.execute(
        "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume) "
        "VALUES ('AAVEUSDT', 3), ('BTCUSDT', 1), ('ETHUSDT', 2)"
    )
    out = sentiment_universe_symbols(conn)
    assert out == ["BTCUSDT", "ETHUSDT", "AAVEUSDT"]


def test_sentiment_universe_symbols_empty(conn):
    out = sentiment_universe_symbols(conn)
    assert out == []


def test_backfill_calls_funding_for_each_symbol(monkeypatch, conn):
    conn.execute(
        "INSERT INTO sentiment_funding_universe (symbol, rank_by_volume) "
        "VALUES ('BTCUSDT', 1), ('ETHUSDT', 2)"
    )
    seen: list[str] = []

    def fake_backfill_funding(c, symbols=None, start_date=None, end_date=None):
        seen.extend(symbols or [])
        return len(symbols or [])

    import crypto.ingestion.backfill_sentiment_funding as mod
    monkeypatch.setattr(mod, "backfill_funding", fake_backfill_funding)

    n = backfill_sentiment_funding(
        conn, start_date=date(2024, 5, 25), end_date=date(2026, 5, 14),
    )
    assert seen == ["BTCUSDT", "ETHUSDT"]
    assert n == 2


def test_backfill_returns_zero_when_universe_empty(conn):
    n = backfill_sentiment_funding(conn)
    assert n == 0
