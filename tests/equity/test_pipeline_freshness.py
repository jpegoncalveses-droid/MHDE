"""Unit tests for pipelines/freshness.py — per-engine freshness reports.

Covers the three engine-level freshness functions invoked at the top of
every prediction pipeline. Lives under tests/equity/ because that's
where the bulk of equity-side health logic is collected; the FX/crypto
engines exercise their own freshness paths in their respective
test_predict.py.
"""
from __future__ import annotations

import logging
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
# Backward-scan coverage selection (KI-149 follow-up — graceful T-2)
#
# Original KI-149 hardening rejected the silent T-2 skip: when MAX had
# partial coverage the freshness check returned is_fresh=False and the
# pipeline skipped entirely. Under the T-2 honest direction, the
# Polygon-free-tier 403-on-current-day pattern recurs every weekday and
# bumps MAX(trade_date) with a partial fallback row count, so the
# strict-MAX behavior produced no predictions ever. This branch
# (fix-freshness-backward-scan) keeps KI-149's refuse-to-silently-skip
# guarantee but degrades the failure to a "select latest fully-covered
# date" path, exposing the gap via is_partial_max + a WARNING log.
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


def test_equity_freshness_partial_max_with_covered_prior_degrades_to_prior(temp_db):
    """KI-149 follow-up: partial MAX + covered prior in scan range →
    is_fresh=True with latest_covered_date set to the prior date.

    Smoking-gun reproduction from data/processed/finding5_pipeline_gap_and_t2_alignment.md:
    today=2026-05-15, prices_daily MAX=2026-05-14 with 68 rows (Yahoo
    fallback only, Polygon 403'd), 2026-05-13 with 536 rows (full
    Polygon coverage). The pipeline must produce a T-2 prediction
    rather than skip entirely.
    """
    today = date(2026, 5, 15)  # Friday
    # Latest (T-1) is partial; T-2 is full; deep history is full.
    seed = [(date(2026, 5, 14), 68)]
    for i in range(2, 32):
        seed.append((today - timedelta(days=i), 520))
    _seed_prices_for_dates(temp_db, seed)

    rep = check_equity_freshness(temp_db, today=today)
    assert rep.is_fresh, (
        f"Graceful degradation must succeed (got is_fresh={rep.is_fresh}, "
        f"msg={rep.message!r})"
    )
    assert rep.latest == date(2026, 5, 14), "latest must remain MAX(trade_date)"
    assert rep.latest_covered_date == date(2026, 5, 13), (
        f"latest_covered_date should degrade to fully-covered T-2; got "
        f"{rep.latest_covered_date}"
    )
    assert rep.is_partial_max is True
    assert rep.reason == "partial_coverage_degraded"


def test_equity_freshness_full_max_coverage_returns_max_unchanged(temp_db):
    """When MAX(trade_date) is fully covered, latest_covered_date == MAX
    and is_partial_max=False (current behavior preserved)."""
    today = date(2026, 5, 15)
    seed = [(date(2026, 5, 14), 510)]
    for i in range(1, 31):
        seed.append((today - timedelta(days=i + 1), 520))
    _seed_prices_for_dates(temp_db, seed)

    rep = check_equity_freshness(temp_db, today=today)
    assert rep.is_fresh, f"Full coverage must pass; msg={rep.message!r}"
    assert rep.latest == date(2026, 5, 14)
    assert rep.latest_covered_date == date(2026, 5, 14)
    assert rep.is_partial_max is False
    assert rep.reason is None


def test_equity_freshness_all_dates_in_scan_range_partial_is_not_fresh(temp_db):
    """When every date within the trading-day threshold is partial,
    is_fresh=False (no graceful degradation possible)."""
    today = date(2026, 5, 15)
    # T-1 and T-2 both partial; only T-3+ is full but that's outside
    # the default max_trading_days=2 scan range.
    seed = [
        (date(2026, 5, 14), 50),  # T-1, partial
        (date(2026, 5, 13), 40),  # T-2, partial
    ]
    for i in range(5, 32):
        seed.append((today - timedelta(days=i), 520))
    _seed_prices_for_dates(temp_db, seed)

    rep = check_equity_freshness(temp_db, today=today)
    assert not rep.is_fresh, (
        f"No covered date in scan range must reject (got is_fresh={rep.is_fresh}, "
        f"msg={rep.message!r})"
    )
    assert rep.latest == date(2026, 5, 14)
    assert rep.latest_covered_date is None
    assert rep.is_partial_max is True
    assert rep.reason == "no_covered_date_in_scan"


def test_equity_freshness_partial_max_emits_warning_log(temp_db, caplog):
    """Degraded path must emit a WARNING describing the upstream gap and
    the date the pipeline degraded to."""
    today = date(2026, 5, 15)
    seed = [(date(2026, 5, 14), 68)]
    for i in range(2, 32):
        seed.append((today - timedelta(days=i), 520))
    _seed_prices_for_dates(temp_db, seed)

    with caplog.at_level(logging.WARNING, logger="mhde.freshness"):
        rep = check_equity_freshness(temp_db, today=today)

    assert rep.is_fresh and rep.is_partial_max
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    matching = [
        r for r in warnings
        if "upstream gap" in r.getMessage().lower()
        and "2026-05-14" in r.getMessage()
        and "2026-05-13" in r.getMessage()
    ]
    assert matching, (
        "Expected one WARNING naming the partial MAX date and the degraded "
        f"target. Got records: {[r.getMessage() for r in warnings]}"
    )


def test_equity_freshness_full_coverage_does_not_emit_partial_warning(temp_db, caplog):
    """Healthy path must NOT emit the upstream-gap warning."""
    today = date(2026, 5, 15)
    seed = [(date(2026, 5, 14), 510)]
    for i in range(1, 31):
        seed.append((today - timedelta(days=i + 1), 520))
    _seed_prices_for_dates(temp_db, seed)

    with caplog.at_level(logging.WARNING, logger="mhde.freshness"):
        rep = check_equity_freshness(temp_db, today=today)

    assert rep.is_fresh and not rep.is_partial_max
    assert not [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "upstream gap" in r.getMessage().lower()
    ], "Full-coverage path must stay silent on the upstream-gap warning"


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
    assert rep.latest_covered_date == today
    assert rep.is_partial_max is False


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
