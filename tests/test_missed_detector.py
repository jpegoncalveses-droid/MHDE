"""Missed-opportunity detector — TDD suite."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _company(conn, ticker):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, company_name, is_active) VALUES (?, ?, true)",
        [ticker, ticker],
    )


def _price(conn, ticker, trade_date, close, open_=None, high=None, low=None, volume=None):
    conn.execute(
        """INSERT OR IGNORE INTO prices_daily
           (id, ticker, trade_date, open, high, low, close, volume, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stooq')""",
        [uuid.uuid4().hex[:16], ticker, trade_date.isoformat(),
         open_ or close, high or close, low or close, close, volume or 100000],
    )


def _prices_flat(conn, ticker, start, days, base_close=100.0):
    for i in range(days):
        _price(conn, ticker, start + timedelta(days=i), base_close)


def _score(conn, ticker, run_id, as_of, tier="Reject", total=30.0):
    conn.execute(
        """INSERT OR IGNORE INTO scores
           (id, run_id, ticker, as_of_date, cheap_score, quality_score, catalyst_score,
            momentum_score, sentiment_score, risk_penalty, total_score, tier, confidence,
            why_ranked, missing_data_json, created_at)
           VALUES (?, ?, ?, ?, 30, 30, 0, 0, 0, 20, ?, ?, 'low', '', '[]', CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], run_id, ticker, as_of.isoformat(), total, tier],
    )


def _filing(conn, ticker, form_type, filing_date, description=None):
    conn.execute(
        """INSERT INTO filings (id, ticker, form_type, filing_date, description, created_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], ticker, form_type, filing_date.isoformat(), description],
    )


# ── Imports smoke ─────────────────────────────────────────────────────────────

def test_detect_missed_opportunities_importable():
    from missed.detector import detect_missed_opportunities  # noqa: F401


def test_root_cause_labels_importable():
    from missed.labels import ROOT_CAUSES, EVENT_TYPES  # noqa: F401


# ── Event detection ───────────────────────────────────────────────────────────

def test_gain_5d_10pct_detected(conn):
    """Ticker rising +10% in 5 trading days → event detected."""
    from missed.detector import detect_missed_opportunities
    ticker = "UP5"
    _company(conn, ticker)
    today = date.today()
    # Seed 20 days of history with a jump starting 6 days ago
    for i in range(20, 7, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(7, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 115.0)  # +15%

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker and e["event_type"] == "gain_5d_10pct"]
    assert len(matching) >= 1, f"Expected gain_5d_10pct event for {ticker}, got {events}"


def test_gain_20d_20pct_detected(conn):
    """Ticker rising +20% over 20 days → event detected."""
    from missed.detector import detect_missed_opportunities
    ticker = "UP20"
    _company(conn, ticker)
    today = date.today()
    for i in range(60, 21, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(21, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 125.0)  # +25%

    events = detect_missed_opportunities(conn, lookback_days=90)
    matching = [e for e in events if e["ticker"] == ticker and e["event_type"] == "gain_20d_20pct"]
    assert len(matching) >= 1, f"Expected gain_20d_20pct event, got {[e['event_type'] for e in events]}"


def test_small_move_not_detected(conn):
    """Ticker rising only +5% in 5 days → NOT detected as gain_5d_10pct."""
    from missed.detector import detect_missed_opportunities
    ticker = "SMALL"
    _company(conn, ticker)
    today = date.today()
    for i in range(20, 7, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(7, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 105.0)  # +5%

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker and e["event_type"] == "gain_5d_10pct"]
    assert len(matching) == 0, f"Small move should not be detected, got {matching}"


def test_52wk_high_breakout_detected(conn):
    """Ticker closing above its prior 252-day high → breakout event detected."""
    from missed.detector import detect_missed_opportunities
    ticker = "52WK"
    _company(conn, ticker)
    today = date.today()
    # 252 days of prices at 100, then a new high
    for i in range(252, 6, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(6, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 115.0)  # new high

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker and e["event_type"] == "52wk_high_breakout"]
    assert len(matching) >= 1, f"Expected 52wk_high_breakout for {ticker}"


# ── Universe / score flags ────────────────────────────────────────────────────

def test_was_in_universe_flag_true(conn):
    """Ticker present in companies table → was_in_universe=True on detected event."""
    from missed.detector import detect_missed_opportunities
    ticker = "INUNI"
    _company(conn, ticker)
    today = date.today()
    for i in range(20, 7, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(7, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 120.0)

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker]
    assert len(matching) >= 1
    assert matching[0]["was_in_universe"] is True


def test_was_in_universe_flag_false(conn):
    """Ticker absent from companies table → was_in_universe=False."""
    from missed.detector import detect_missed_opportunities
    ticker = "NOTUNI"
    today = date.today()
    # Seed prices WITHOUT adding to companies table
    for i in range(20, 7, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(7, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 120.0)

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker]
    if matching:
        assert matching[0]["was_in_universe"] is False


def test_was_scored_flag_true(conn):
    """Ticker with a score before the event → was_scored=True."""
    from missed.detector import detect_missed_opportunities
    ticker = "SCORED"
    _company(conn, ticker)
    today = date.today()
    for i in range(20, 7, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(7, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 120.0)
    # Score 10 days ago (before the event)
    _score(conn, ticker, "run_pre", today - timedelta(days=10), tier="Reject", total=25.0)

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker]
    assert len(matching) >= 1
    assert matching[0]["was_scored"] is True


def test_had_catalyst_evidence_flag(conn):
    """Filing before event → had_catalyst_evidence=True."""
    from missed.detector import detect_missed_opportunities
    ticker = "CATEVT"
    _company(conn, ticker)
    today = date.today()
    for i in range(20, 7, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(7, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 120.0)
    _filing(conn, ticker, "8-K", today - timedelta(days=12), description="8k_merger.htm")

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker]
    assert len(matching) >= 1
    assert matching[0]["had_catalyst_evidence"] is True


# ── Event struct fields ───────────────────────────────────────────────────────

def test_event_has_required_fields(conn):
    """Detected event dict contains all required fields."""
    from missed.detector import detect_missed_opportunities
    ticker = "FLD"
    _company(conn, ticker)
    today = date.today()
    for i in range(20, 7, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(7, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 120.0)

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker]
    assert len(matching) >= 1
    event = matching[0]
    required = {"event_id", "ticker", "event_date", "event_type", "return_value",
                "window_days", "was_in_universe", "was_scored",
                "had_catalyst_evidence", "tier_before_event"}
    missing = required - set(event.keys())
    assert not missing, f"Event missing fields: {missing}"
