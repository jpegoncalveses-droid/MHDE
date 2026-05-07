"""Tests for CandidateLifecycle classification and phase detection."""
from __future__ import annotations

import datetime
import uuid

import duckdb
import pytest

from outcomes.candidate_lifecycle import (
    CandidateLifecycle,
    compute_lifecycle,
    detect_episode_start,
)


def _make_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE prices_daily (
            id VARCHAR PRIMARY KEY,
            ticker VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            close DOUBLE NOT NULL,
            volume BIGINT,
            adjusted_close DOUBLE,
            UNIQUE (ticker, trade_date)
        )
    """)
    return conn


def _insert_prices(conn, ticker: str, prices: list[tuple]):
    """Insert (trade_date_str, close, volume) tuples."""
    for trade_date, close, volume in prices:
        conn.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, close, volume) VALUES (?, ?, ?, ?, ?)",
            [uuid.uuid4().hex[:16], ticker, trade_date, close, volume],
        )


def _day_range(start_str: str, n: int) -> list[str]:
    """Return n consecutive dates starting from start_str (calendar, not trading)."""
    start = datetime.date.fromisoformat(start_str)
    return [str(start + datetime.timedelta(days=i)) for i in range(n)]


# ── detect_episode_start ────────────────────────────────────────────────────

def test_detect_episode_start_no_pre_event_move():
    """Flat prices before event → episode_start = event_date."""
    event_date = datetime.date(2026, 3, 27)
    prices = [(datetime.date(2026, 3, 27) - datetime.timedelta(days=i), 50.0, 1_000_000)
              for i in range(1, 21)]
    result = detect_episode_start(prices, event_date)
    assert result == event_date


def test_detect_episode_start_finds_pre_event_run():
    """A ≥5% run in 5-day window before event → episode starts at run onset."""
    event_date = datetime.date(2026, 3, 27)
    # 20 days of flat, then 5 days of 2% daily gain = +10% total run ending just before event
    base_prices = [(datetime.date(2026, 3, 27) - datetime.timedelta(days=i), 40.0, 1_000_000)
                   for i in range(6, 21)]
    # Days 5..1 before event: rising from 40 → 44
    run_prices = [
        (datetime.date(2026, 3, 22), 40.0, 1_500_000),
        (datetime.date(2026, 3, 23), 41.0, 1_500_000),
        (datetime.date(2026, 3, 24), 42.0, 1_500_000),
        (datetime.date(2026, 3, 25), 43.0, 1_500_000),
        (datetime.date(2026, 3, 26), 44.0, 1_500_000),
    ]
    all_prices = sorted(base_prices + run_prices, key=lambda x: x[0])
    result = detect_episode_start(all_prices, event_date)
    assert result <= datetime.date(2026, 3, 22), "Should detect start of the run"
    assert result < event_date, "Episode should start before event"


# ── compute_lifecycle: CTRA-like (validated +29%) ───────────────────────────

def test_ctra_like_validated():
    """event_price=27.86, latest=36.04 (+29.4%) → validated."""
    conn = _make_conn()
    event_date = "2025-11-15"
    # Event day + 63 subsequent days (≈3 months, well past default window)
    prices = []
    prices.append((event_date, 27.86, 2_000_000))
    for i in range(1, 63):
        d = str(datetime.date.fromisoformat(event_date) + datetime.timedelta(days=i))
        prices.append((d, 36.04, 1_500_000))
    _insert_prices(conn, "CTRA", prices)

    lc = compute_lifecycle(
        conn, "CTRA",
        event_date=event_date,
        as_of="2026-02-15",
    )
    assert lc.outcome_status == "validated"
    assert lc.return_since_event is not None
    assert lc.return_since_event > 0.25
    assert lc.current_actionability in ("context", "watch")


def test_ctra_like_event_price_anchored():
    """event_price must be anchored to event_date close, not latest."""
    conn = _make_conn()
    _insert_prices(conn, "CTRA2", [
        ("2025-11-15", 27.86, 2_000_000),
        ("2025-11-17", 30.00, 1_000_000),
        ("2025-11-18", 36.04, 1_000_000),
    ])
    lc = compute_lifecycle(conn, "CTRA2", event_date="2025-11-15", as_of="2025-11-18")
    assert abs(lc.event_price - 27.86) < 0.01


# ── compute_lifecycle: VG-like (pre-event + post-event fade) ─────────────────

def test_vg_like_post_event_fade():
    """Pre-event accumulation + price below event_price → failed, post_event_fade."""
    conn = _make_conn()
    event_date = "2026-03-27"
    prices = []
    # 10 days of rising prices before event (episode accumulation)
    for i in range(10, 0, -1):
        d = str(datetime.date.fromisoformat(event_date) - datetime.timedelta(days=i))
        prices.append((d, 14.0 + (10 - i) * 0.35, 1_000_000))
    # Event day at peak
    prices.append((event_date, 17.53, 2_500_000))
    # Post-event fade over 20 days
    for i in range(1, 21):
        d = str(datetime.date.fromisoformat(event_date) + datetime.timedelta(days=i))
        prices.append((d, 17.53 - i * 0.24, 800_000))
    _insert_prices(conn, "VG", prices)

    lc = compute_lifecycle(conn, "VG", event_date=event_date, as_of="2026-04-20")
    assert lc.outcome_status in ("failed", "pending")
    assert lc.current_phase == "post_event_fade"
    assert lc.return_since_event is not None
    assert lc.return_since_event < -0.04


def test_vg_like_episode_start_before_event():
    """VG-like setup: episode_start_date should be detected before event_date."""
    conn = _make_conn()
    event_date = "2026-03-27"
    prices = []
    for i in range(10, 0, -1):
        d = str(datetime.date.fromisoformat(event_date) - datetime.timedelta(days=i))
        prices.append((d, 14.0 + (10 - i) * 0.35, 1_000_000))
    prices.append((event_date, 17.53, 2_500_000))
    _insert_prices(conn, "VG2", prices)

    lc = compute_lifecycle(conn, "VG2", event_date=event_date, as_of="2026-03-27")
    assert lc.episode_start_date is not None
    assert lc.episode_start_date < datetime.date.fromisoformat(event_date)


# ── compute_lifecycle: outcome states ────────────────────────────────────────

def test_pending_within_window():
    """Price barely moved after event within the window → pending."""
    conn = _make_conn()
    _insert_prices(conn, "FLAT", [
        ("2026-04-01", 100.0, 1_000_000),
        ("2026-04-02", 101.0, 1_000_000),
        ("2026-04-03", 100.5, 1_000_000),
    ])
    lc = compute_lifecycle(conn, "FLAT", event_date="2026-04-01", as_of="2026-04-03")
    assert lc.outcome_status in ("pending", "inconclusive")


def test_expired_past_window_no_validation():
    """Past expected window with only small gain → expired."""
    conn = _make_conn()
    event_date = "2025-01-02"
    prices = [(event_date, 100.0, 1_000_000)]
    for i in range(1, 200):
        d = str(datetime.date.fromisoformat(event_date) + datetime.timedelta(days=i))
        prices.append((d, 102.0, 1_000_000))
    _insert_prices(conn, "EXPD", prices)
    lc = compute_lifecycle(conn, "EXPD", event_date=event_date, as_of="2025-09-01")
    assert lc.outcome_status in ("expired", "inconclusive")


def test_validated_then_faded():
    """Price hit +15% then fell back to -2% → validated_then_faded."""
    conn = _make_conn()
    event_date = "2026-01-02"
    prices = [(event_date, 100.0, 2_000_000)]
    # Rise to +15% over 10 days
    for i in range(1, 11):
        d = str(datetime.date.fromisoformat(event_date) + datetime.timedelta(days=i))
        prices.append((d, 100.0 + i * 1.5, 1_500_000))
    # Fall back to -2%
    for i in range(11, 31):
        d = str(datetime.date.fromisoformat(event_date) + datetime.timedelta(days=i))
        prices.append((d, 115.0 - (i - 10) * 5.85, 1_000_000))
    _insert_prices(conn, "FADE", prices)

    lc = compute_lifecycle(conn, "FADE", event_date=event_date, as_of="2026-02-01")
    assert lc.outcome_status in ("validated_then_faded", "failed")
    assert lc.max_runup_since_event is not None
    assert lc.max_runup_since_event > 0.10


# ── compute_lifecycle: missing data ─────────────────────────────────────────

def test_no_price_data_returns_insufficient():
    """No prices at all → insufficient_data."""
    conn = _make_conn()
    lc = compute_lifecycle(conn, "NONE", event_date="2026-04-01", as_of="2026-04-15")
    assert lc.outcome_status == "insufficient_data"
    assert lc.current_phase == "insufficient_data"
    assert lc.current_actionability == "insufficient_data"


def test_no_event_date_returns_insufficient():
    """No event_date provided → insufficient_data."""
    conn = _make_conn()
    _insert_prices(conn, "XX", [("2026-04-01", 50.0, 1_000_000)])
    lc = compute_lifecycle(conn, "XX", event_date="", as_of="2026-04-01")
    assert lc.outcome_status == "insufficient_data"


# ── dataclass fields ─────────────────────────────────────────────────────────

def test_lifecycle_has_all_required_fields():
    """CandidateLifecycle dataclass must have the expected fields."""
    required = {
        "ticker", "event_date", "episode_start_date", "signal_date",
        "event_price", "episode_start_price", "signal_price", "latest_price",
        "return_since_event", "return_since_episode_start",
        "max_runup_since_event", "max_drawdown_since_event", "return_from_peak",
        "expected_window_days", "validation_threshold",
        "outcome_status", "current_phase", "current_actionability", "explanation",
    }
    import dataclasses
    actual = {f.name for f in dataclasses.fields(CandidateLifecycle)}
    missing = required - actual
    assert not missing, f"Missing dataclass fields: {missing}"


# ── no scoring / no feature flags guard ─────────────────────────────────────

def test_no_scoring_changes():
    """Lifecycle module must not modify any scoring or introduce feature flags."""
    import inspect
    import outcomes.candidate_lifecycle as mod
    src = inspect.getsource(mod)
    for bad in ("feature_flag", "FeatureFlag", "openai", "anthropic"):
        assert bad.lower() not in src.lower(), f"Prohibited term '{bad}' in candidate_lifecycle.py"
