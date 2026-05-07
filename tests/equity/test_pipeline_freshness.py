"""Unit tests for pipelines/freshness.py — per-engine freshness reports.

Covers the three engine-level freshness functions invoked at the top of
every prediction pipeline. Lives under tests/equity/ because that's
where the bulk of equity-side health logic is collected; the FX/crypto
engines exercise their own freshness paths in their respective
test_predict.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from pipelines.freshness import (
    check_equity_freshness,
    check_crypto_freshness,
    check_fx_freshness,
    check_all,
)


# ──────────────────────────────────────────────────────────────────────
# Equity freshness — trading-day arithmetic
# ──────────────────────────────────────────────────────────────────────


def test_equity_freshness_empty(temp_db):
    rep = check_equity_freshness(temp_db)
    assert not rep.is_fresh
    assert rep.engine == "equity"
    assert rep.latest is None
    assert "empty" in rep.message.lower()


def test_equity_freshness_fresh_today(temp_db):
    today = date.today()
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["t1", "AAPL", today, 150.0],
    )
    rep = check_equity_freshness(temp_db, today=today)
    assert rep.is_fresh
    assert rep.latest == today


def test_equity_freshness_two_trading_days_ago_still_fresh(temp_db):
    # Pick a Wednesday so we don't hit weekend skip
    today = date(2026, 5, 6)  # a Wednesday
    two_back = date(2026, 5, 4)  # Monday — 2 trading days ago
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["t1", "AAPL", two_back, 150.0],
    )
    rep = check_equity_freshness(temp_db, today=today)
    assert rep.is_fresh


def test_equity_freshness_stale_after_threshold(temp_db):
    """Latest 5 trading days back is stale at default threshold of 2."""
    today = date(2026, 5, 6)
    stale_date = date(2026, 4, 27)  # 7 trading days back
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["t1", "AAPL", stale_date, 150.0],
    )
    rep = check_equity_freshness(temp_db, today=today)
    assert not rep.is_fresh


# ──────────────────────────────────────────────────────────────────────
# Crypto freshness — calendar-day arithmetic
# ──────────────────────────────────────────────────────────────────────


def test_crypto_freshness_empty(temp_db):
    rep = check_crypto_freshness(temp_db)
    assert not rep.is_fresh
    assert rep.engine == "crypto"


def test_crypto_freshness_fresh_today(temp_db):
    today = date.today()
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["BTCUSDT", today, 50_000.0],
    )
    rep = check_crypto_freshness(temp_db, today=today)
    assert rep.is_fresh


def test_crypto_freshness_one_day_old_still_fresh(temp_db):
    today = date(2026, 5, 6)
    yesterday = today - timedelta(days=1)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["BTCUSDT", yesterday, 50_000.0],
    )
    rep = check_crypto_freshness(temp_db, today=today)
    assert rep.is_fresh


def test_crypto_freshness_three_days_old_is_stale(temp_db):
    today = date(2026, 5, 6)
    stale = today - timedelta(days=3)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["BTCUSDT", stale, 50_000.0],
    )
    rep = check_crypto_freshness(temp_db, today=today)
    assert not rep.is_fresh


# ──────────────────────────────────────────────────────────────────────
# FX freshness — hourly window
# ──────────────────────────────────────────────────────────────────────


def test_fx_freshness_empty(temp_db):
    rep = check_fx_freshness(temp_db)
    assert not rep.is_fresh
    assert rep.engine == "fx"


def test_fx_freshness_30m_old_is_fresh(temp_db):
    now = datetime.utcnow().replace(tzinfo=None)
    bar = now - timedelta(minutes=30)
    bar = bar.replace(minute=0, second=0, microsecond=0)
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert rep.is_fresh


def test_fx_freshness_3h_old_is_stale(temp_db):
    now = datetime(2026, 5, 6, 12, 0, 0)
    bar = now - timedelta(hours=3)
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert not rep.is_fresh


# ──────────────────────────────────────────────────────────────────────
# check_all roll-up
# ──────────────────────────────────────────────────────────────────────


def test_check_all_returns_three_reports(temp_db):
    reports = check_all(temp_db)
    assert set(reports.keys()) == {"equity", "crypto", "fx"}
    for engine, rep in reports.items():
        assert rep.engine == engine
