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
# Partial-coverage detection (KI-149 regression — the smoking-gun case)
# ──────────────────────────────────────────────────────────────────────


def _seed_prices_for_dates(conn, dates_and_counts: list[tuple[date, int]]) -> None:
    """Seed prices_daily with N synthetic tickers per date."""
    rid = 0
    for d, n in dates_and_counts:
        for i in range(n):
            rid += 1
            conn.execute(
                "INSERT INTO prices_daily (id, ticker, trade_date, close) "
                "VALUES (?, ?, ?, ?)",
                [f"id{rid}", f"T{i:04d}", d, 100.0],
            )


def test_equity_freshness_partial_coverage_on_latest_date_is_not_fresh(temp_db):
    """KI-149: 4 fallback OTC rows on the latest date must NOT count as fresh.

    Smoking-gun scenario from data/processed/finding3_ml_pipeline_gap_root_cause.md:
    prices_daily latest=2026-05-13 with 4 rows, prior days have ~520 rows each.
    MAX(trade_date)-only check incorrectly declares freshness OK; the coverage
    check must reject this.
    """
    today = date(2026, 5, 14)
    # 30 prior trading days at ~520 rows each (well-covered), latest at 4 rows.
    seed = [(date(2026, 5, 13), 4)]
    for i in range(2, 32):
        seed.append((today - timedelta(days=i), 520))
    _seed_prices_for_dates(temp_db, seed)

    rep = check_equity_freshness(temp_db, today=today)
    assert not rep.is_fresh, (
        f"Partial-coverage latest must be rejected (got is_fresh={rep.is_fresh}, "
        f"msg={rep.message!r})"
    )
    assert rep.reason == "partial_coverage"
    assert rep.coverage_row_count == 4
    assert rep.coverage_expected_min is not None
    assert rep.coverage_expected_min > 4


def test_equity_freshness_full_coverage_on_latest_date_is_fresh(temp_db):
    """Latest date with row count comparable to recent history → fresh."""
    today = date(2026, 5, 14)
    seed = [(date(2026, 5, 13), 510)]
    for i in range(1, 31):
        seed.append((today - timedelta(days=i + 1), 520))
    _seed_prices_for_dates(temp_db, seed)

    rep = check_equity_freshness(temp_db, today=today)
    assert rep.is_fresh, f"Full coverage must pass; msg={rep.message!r}"
    assert rep.reason is None


def test_equity_freshness_single_row_history_does_not_crash(temp_db):
    """When prices_daily has only 1 row, the coverage check must not crash.

    With only one historical row, the rolling mean equals that row count
    and the same row is the latest, so it trivially satisfies coverage.
    Existing test_equity_freshness_fresh_today relies on this behavior.
    """
    today = date.today()
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["solo1", "AAPL", today, 150.0],
    )
    rep = check_equity_freshness(temp_db, today=today)
    assert rep.is_fresh
    assert rep.reason is None


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


# ──────────────────────────────────────────────────────────────────────
# FX freshness — forex-closed window (KI-128)
# ──────────────────────────────────────────────────────────────────────


def test_fx_freshness_during_close_with_pre_close_bar_is_fresh(temp_db):
    # Sat 2026-05-16 12:00 UTC; latest bar Fri 21:55 UTC (last bar
    # before close). is_fresh because latest >= fx_close_floor (Fri 22:00).
    # Wait — Fri 21:55 is BEFORE Fri 22:00, but the actual last bar
    # written at the moment of close lands at Fri 21:00 (top of
    # hour) since the FX hourly schedule fires at :05 reading the
    # most recent completed hour. Use Fri 21:00 as the "last bar
    # before close".
    from datetime import datetime as _dt
    now = _dt(2026, 5, 16, 12, 0, 0)
    bar = _dt(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert rep.is_fresh, f"expected fresh during close window with pre-close bar; msg={rep.message}"


def test_fx_freshness_during_close_with_outage_in_flight_is_stale(temp_db):
    # Sat 12:00 UTC; latest bar Wed 10:00 UTC — outage started long
    # before forex closed; latest is BEFORE fx_close_floor.
    from datetime import datetime as _dt
    now = _dt(2026, 5, 16, 12, 0, 0)
    bar = _dt(2026, 5, 13, 10, 0, 0)  # Wed 10:00 UTC
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert not rep.is_fresh, "outage during close window must still be flagged"


def test_fx_freshness_post_resume_with_stale_data_is_stale(temp_db):
    # Sun 23:00 UTC — closed window ended at Sun 22:00. 2h budget
    # active. latest = Fri 21:00 UTC (older than 2h) → stale.
    from datetime import datetime as _dt
    now = _dt(2026, 5, 17, 23, 0, 0)
    bar = _dt(2026, 5, 15, 21, 0, 0)
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert not rep.is_fresh
