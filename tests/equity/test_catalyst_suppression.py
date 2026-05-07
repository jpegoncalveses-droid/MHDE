"""Catalyst routine suppression — Phase 2 TDD suite.

After the experiment is applied:
  - 10-Q/10-K: +5 (was +20)
  - 8-K material (keyword match): +30 (unchanged)
  - 8-K routine (no keyword): +15 (was +30)
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema
from features.catalyst import compute_catalyst


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _company(conn, ticker, name="Test Corp"):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, company_name, is_active) VALUES (?, ?, true)",
        [ticker, name],
    )


def _filing(conn, ticker, form_type, filing_date=None, description=None):
    conn.execute(
        """INSERT INTO filings (id, ticker, form_type, filing_date, description, created_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], ticker, form_type,
         (filing_date or date.today() - timedelta(days=5)).isoformat(),
         description],
    )


def _event(conn, ticker, event_type="earnings", event_date=None, is_upcoming=True):
    conn.execute(
        """INSERT INTO events (id, ticker, event_type, event_date, title, source, is_upcoming, created_at)
           VALUES (?, ?, ?, ?, ?, 'nasdaq_earnings', ?, CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], ticker, event_type,
         (event_date or date.today() + timedelta(days=7)).isoformat(),
         f"Earnings: {ticker}", is_upcoming],
    )


def _short_interest(conn, ticker, si, settlement_date):
    conn.execute(
        """INSERT INTO short_interest (id, ticker, short_interest, settlement_date, source, created_at)
           VALUES (?, ?, ?, ?, 'finra', CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], ticker, si, settlement_date.isoformat()],
    )


def _catalyst_feat(conn, ticker):
    _company(conn, ticker)
    feats = compute_catalyst(conn, "run1", ticker, date.today())
    return feats[0]


# ── 10-Q / 10-K scoring ───────────────────────────────────────────────────────

def test_10q_does_not_inflate_catalyst_like_before(conn):
    """10-Q alone → catalyst_score == 5, not 20."""
    _company(conn, "TQQ")
    _filing(conn, "TQQ", "10-Q", description="10q.htm")

    feat = compute_catalyst(conn, "run1", "TQQ", date.today())
    score = feat[0]["feature_score"]
    assert score == 5.0, f"10-Q alone should score 5, got {score}"


def test_10k_does_not_inflate_catalyst(conn):
    """10-K alone → catalyst_score == 5, not 20."""
    _company(conn, "TKK")
    _filing(conn, "TKK", "10-K", description="10k.htm")

    feat = compute_catalyst(conn, "run1", "TKK", date.today())
    score = feat[0]["feature_score"]
    assert score == 5.0, f"10-K alone should score 5, got {score}"


# ── 8-K material vs routine ───────────────────────────────────────────────────

def test_8k_with_no_description_gets_routine_score(conn):
    """8-K with description=None → routine score (15), not full score (30)."""
    _company(conn, "R8K")
    _filing(conn, "R8K", "8-K", description=None)

    feat = compute_catalyst(conn, "run1", "R8K", date.today())
    score = feat[0]["feature_score"]
    assert score == 15.0, f"8-K with no description should score 15, got {score}"


def test_8k_with_material_keyword_in_description_gets_full_score(conn):
    """8-K with 'merger' in description → full material score (30)."""
    _company(conn, "M8K")
    _filing(conn, "M8K", "8-K", description="8k_merger_agreement.htm")

    feat = compute_catalyst(conn, "run1", "M8K", date.today())
    score = feat[0]["feature_score"]
    assert score == 30.0, f"8-K with material keyword should score 30, got {score}"


def test_8k_generic_filename_gets_routine_score(conn):
    """8-K with generic filename 'form8-k.htm' (no material keyword) → 15."""
    _company(conn, "G8K")
    _filing(conn, "G8K", "8-K", description="form8-k.htm")

    feat = compute_catalyst(conn, "run1", "G8K", date.today())
    score = feat[0]["feature_score"]
    assert score == 15.0, f"8-K with generic filename should score 15, got {score}"


# ── Signals list populated ────────────────────────────────────────────────────

def test_signals_list_still_populated(conn):
    """After suppression, signals list is still populated for 10-Q filings."""
    _company(conn, "SIG")
    _filing(conn, "SIG", "10-Q", description="10q.htm")

    feat = compute_catalyst(conn, "run1", "SIG", date.today())
    signals = feat[0]["metadata"].get("signals", [])
    assert len(signals) >= 1, f"signals list should be populated, got {signals}"


# ── Total score comparison ────────────────────────────────────────────────────

def test_total_score_lower_for_routine_only_catalyst(conn):
    """Routine-only catalyst (10-Q + routine 8-K) scores < 40 after suppression."""
    _company(conn, "LOW")
    _filing(conn, "LOW", "10-Q", description="10q.htm")
    _filing(conn, "LOW", "8-K", description="form8-k.htm")

    feat = compute_catalyst(conn, "run1", "LOW", date.today())
    score = feat[0]["feature_score"]
    # 10-Q=5 + routine 8-K=15 = 20; old score would have been 20+30=50
    assert score < 40, f"Routine-only catalyst should score < 40, got {score}"


# ── Routine flag in metadata ──────────────────────────────────────────────────

def test_routine_flag_in_metadata(conn):
    """10-Q filing adds routine_filing=True to the signals metadata."""
    _company(conn, "RFL")
    _filing(conn, "RFL", "10-Q", description="10q.htm")

    feat = compute_catalyst(conn, "run1", "RFL", date.today())
    meta = feat[0].get("metadata", {})
    assert meta.get("routine_filing") is True, (
        f"10-Q should set routine_filing=True in metadata, got {meta}"
    )


# ── Non-routine signals unaffected ────────────────────────────────────────────

def test_short_interest_spike_still_scores(conn):
    """Short interest spike >10% still adds +15 regardless of suppression."""
    _company(conn, "SIS")
    _short_interest(conn, "SIS", 1_000_000, date.today() - timedelta(days=14))
    _short_interest(conn, "SIS", 1_200_000, date.today() - timedelta(days=1))

    feat = compute_catalyst(conn, "run1", "SIS", date.today())
    score = feat[0]["feature_score"]
    assert score >= 15.0, f"Short interest spike should still score >=15, got {score}"
    signals = feat[0]["metadata"].get("signals", [])
    assert any("Short interest" in s for s in signals), f"SI signal missing: {signals}"


def test_earnings_event_still_scores(conn):
    """Upcoming earnings event within 14d still adds +25."""
    _company(conn, "EES")
    _event(conn, "EES", event_type="earnings",
           event_date=date.today() + timedelta(days=7), is_upcoming=True)

    feat = compute_catalyst(conn, "run1", "EES", date.today())
    score = feat[0]["feature_score"]
    assert score >= 25.0, f"Earnings event should still score >=25, got {score}"
    signals = feat[0]["metadata"].get("signals", [])
    assert any("Earnings" in s for s in signals), f"Earnings signal missing: {signals}"
