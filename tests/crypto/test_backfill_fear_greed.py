"""Tests for crypto/ingestion/backfill_fear_greed.py."""
from datetime import date
from unittest.mock import MagicMock

import duckdb
import pytest

from crypto.ingestion.backfill_fear_greed import backfill_fear_greed
from storage.db import get_connection
from storage.migrations import run_migrations


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "mhde.duckdb"))
    run_migrations(c)
    return c


def test_backfill_inserts_rows(conn):
    client = MagicMock()
    client.fetch_history.return_value = [
        {"date": date(2025, 1, 1), "value": 55, "value_classification": "Neutral"},
        {"date": date(2025, 1, 2), "value": 60, "value_classification": "Greed"},
    ]
    n = backfill_fear_greed(conn, client=client, limit=0)
    assert n == 2
    rows = conn.execute(
        "SELECT date, value, value_classification FROM sentiment_fear_greed ORDER BY date"
    ).fetchall()
    assert rows == [
        (date(2025, 1, 1), 55, "Neutral"),
        (date(2025, 1, 2), 60, "Greed"),
    ]


def test_backfill_is_idempotent(conn):
    """Re-running upserts (latest value wins) and doesn't error on duplicates."""
    client = MagicMock()
    client.fetch_history.return_value = [
        {"date": date(2025, 1, 1), "value": 55, "value_classification": "Neutral"},
    ]
    backfill_fear_greed(conn, client=client)

    # Second call: same date, different value (e.g., late correction)
    client.fetch_history.return_value = [
        {"date": date(2025, 1, 1), "value": 60, "value_classification": "Greed"},
    ]
    backfill_fear_greed(conn, client=client)

    rows = conn.execute(
        "SELECT date, value, value_classification FROM sentiment_fear_greed"
    ).fetchall()
    assert len(rows) == 1
    # Upsert: later value overwrites
    assert rows[0] == (date(2025, 1, 1), 60, "Greed")


def test_backfill_returns_zero_on_empty_response(conn):
    client = MagicMock()
    client.fetch_history.return_value = []
    n = backfill_fear_greed(conn, client=client)
    assert n == 0


def test_backfill_passes_limit_to_client(conn):
    """Driver respects the limit argument."""
    client = MagicMock()
    client.fetch_history.return_value = []
    backfill_fear_greed(conn, client=client, limit=30)
    client.fetch_history.assert_called_once_with(limit=30)
