"""Unit tests for monitoring.pipeline_monitor.checks.fx."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from monitoring.pipeline_monitor.core import Status
from monitoring.pipeline_monitor.checks import fx as F


# 2026-05-12 12:00 UTC is a Tuesday midday — forex open, 2h freshness applies.
NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
# 2026-05-16 is a Saturday — forex closed; close-floor is Fri 2026-05-15 21:00.
NOW_WEEKEND = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def _add_bar(conn, dt: datetime):
    conn.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
        "gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count) VALUES (?,?,?,?,?,?,?,?,?)",
        [dt, dt.date(), dt.weekday(), dt.hour, 1.15, 1.16, 1.14, 1.155, 100],
    )


def _add_signal(conn, dt: datetime, signal_type="BUY_GBP"):
    conn.execute(
        "INSERT INTO fx_signals (datetime_utc, signal_type, gbpeur_price) VALUES (?,?,?)",
        [dt, signal_type, 1.155],
    )


# ── 1. bar ingestion ──────────────────────────────────────────────────
def test_bar_ingestion_green(temp_db):
    _add_bar(temp_db, NOW.replace(tzinfo=None) - timedelta(hours=1))
    assert F.check_bar_ingestion(temp_db, NOW).status is Status.GREEN


def test_bar_ingestion_red_when_stale(temp_db):
    _add_bar(temp_db, NOW.replace(tzinfo=None) - timedelta(hours=5))
    r = F.check_bar_ingestion(temp_db, NOW)
    assert r.status is Status.RED


def test_bar_ingestion_red_when_empty(temp_db):
    r = F.check_bar_ingestion(temp_db, NOW)
    assert r.status is Status.RED and "empty" in r.detail


def test_bar_ingestion_green_during_forex_close(temp_db):
    # last bar before Friday 22:00 close is the 21:00 bar — fresh while closed.
    _add_bar(temp_db, datetime(2026, 5, 15, 21, 0, 0))
    assert F.check_bar_ingestion(temp_db, NOW_WEEKEND).status is Status.GREEN


def test_bar_ingestion_red_during_forex_close_when_outage(temp_db):
    # only an old Thursday bar — an outage that ran through the Friday close
    _add_bar(temp_db, datetime(2026, 5, 14, 12, 0, 0))
    assert F.check_bar_ingestion(temp_db, NOW_WEEKEND).status is Status.RED


# ── 2. signal generation ──────────────────────────────────────────────
def test_signal_green(temp_db):
    bar = NOW.replace(tzinfo=None) - timedelta(hours=1)
    _add_bar(temp_db, bar)
    _add_signal(temp_db, bar, "WAIT")
    r = F.check_signal_generation(temp_db, NOW)
    assert r.status is Status.GREEN and "WAIT" in r.detail


def test_signal_red_when_lagging(temp_db):
    bar = NOW.replace(tzinfo=None) - timedelta(hours=1)
    _add_bar(temp_db, bar)
    _add_signal(temp_db, bar - timedelta(hours=3), "BUY_GBP")
    r = F.check_signal_generation(temp_db, NOW)
    assert r.status is Status.RED and "lags latest bar" in r.detail


def test_signal_red_when_no_signals(temp_db):
    _add_bar(temp_db, NOW.replace(tzinfo=None) - timedelta(hours=1))
    r = F.check_signal_generation(temp_db, NOW)
    assert r.status is Status.RED and "fx_signals is empty" in r.detail


def test_signal_red_when_no_bars(temp_db):
    r = F.check_signal_generation(temp_db, NOW)
    assert r.status is Status.RED and "fx_prices_hourly is empty" in r.detail
