"""Durable CI guard for build_universe's hysteresis-window date selection.

These are pure synthetic-data unit tests for ``_recent_ranking_dates`` and the
``build_universe`` distinct-date guard. They lock the LOGIC contract: the helper
returns the ``min(N, hysteresis_days)`` most-recent distinct dates (newest
first), and the guard raises below ``hysteresis_days`` / passes at or above it.

LIMITATION — these tests do NOT reproduce the DuckDB 1.5.2
``DISTINCT + ORDER BY DESC + LIMIT`` optimizer collapse that the fix addresses.
That bug is layout/statistics-sensitive and only manifests against the real,
incrementally written ``data/mhde.duckdb`` — synthetic buffers (any shape/scale)
and even a CTAS copy of the live table always return the correct rows. The
actual bug reproduction is host-only and lives in
``tests/integration/test_universe_builder_live_buffer.py``. Do NOT assume that a
green run here means the optimizer bug itself is covered; it is not.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from crypto.ingestion import universe_builder
from crypto.ingestion.binance_client import BinanceClient
from crypto.ingestion.universe_builder import (
    HYSTERESIS_DAYS,
    _recent_ranking_dates,
    build_universe,
)
from crypto.schema import create_all_tables

TODAY = date(2026, 5, 16)


def _buffer_with_dates(n_dates, *, symbol="ANCHORUSDT", in_top_50=True):
    """In-memory buffer seeded with ``n_dates`` distinct ranking_dates (newest=TODAY)."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    for i in range(n_dates):
        d = TODAY - timedelta(days=i)
        conn.execute(
            "INSERT INTO crypto_universe_ranking_buffer "
            "(symbol, ranking_date, avg_daily_volume_30d, rank_by_volume, in_top_50) "
            "VALUES (?, ?, ?, ?, ?)",
            [symbol, d, 1e8, 10, in_top_50],
        )
    return conn


@pytest.mark.parametrize(
    "n_dates,expected",
    [(10, HYSTERESIS_DAYS), (HYSTERESIS_DAYS, HYSTERESIS_DAYS), (3, 3), (1, 1)],
)
def test_recent_ranking_dates_returns_min_of_n_and_window(n_dates, expected):
    """Helper returns min(N, hysteresis_days) distinct dates, newest-first."""
    conn = _buffer_with_dates(n_dates)
    try:
        dates = _recent_ranking_dates(conn, HYSTERESIS_DAYS)
    finally:
        conn.close()
    assert len(dates) == expected
    assert dates == sorted(dates, reverse=True), "dates must be newest-first"
    assert len(set(dates)) == len(dates), "dates must be distinct"
    assert dates[0] == TODAY


def test_build_universe_raises_below_hysteresis_window(monkeypatch):
    """Fewer than HYSTERESIS_DAYS distinct dates -> guard raises ValueError."""
    monkeypatch.setattr(universe_builder, "_today_utc", lambda: TODAY)
    conn = _buffer_with_dates(HYSTERESIS_DAYS - 1)
    try:
        with pytest.raises(ValueError, match="distinct dates"):
            build_universe(conn, dry_run=True)
    finally:
        conn.close()


def test_build_universe_passes_guard_at_hysteresis_window(monkeypatch):
    """At or above HYSTERESIS_DAYS distinct dates -> guard does NOT raise."""
    monkeypatch.setattr(universe_builder, "_today_utc", lambda: TODAY)
    monkeypatch.setattr(
        BinanceClient,
        "fetch_futures_exchange_info",
        lambda self: [
            {"symbol": "ANCHORUSDT", "base_asset": "ANCHOR",
             "onboard_date": TODAY - timedelta(days=500)}
        ],
    )
    conn = _buffer_with_dates(HYSTERESIS_DAYS)
    try:
        result = build_universe(conn, dry_run=True)  # must clear the guard
    finally:
        conn.close()
    assert result["latest_buffer_date"] == TODAY
