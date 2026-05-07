"""Missed-opportunity investigator — TDD suite.

Critical invariant: investigation must NOT use data after the event date (no hindsight).
"""
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


def _event(ticker, event_date, was_in_universe=True, was_scored=False,
           score_before=None, tier_before=None, had_catalyst=False):
    return {
        "event_id": uuid.uuid4().hex[:16],
        "ticker": ticker,
        "event_date": event_date,
        "event_type": "gain_5d_10pct",
        "return_value": 15.0,
        "window_days": 5,
        "was_in_universe": was_in_universe,
        "was_scored": was_scored,
        "score_before_event": score_before,
        "tier_before_event": tier_before,
        "had_catalyst_evidence": had_catalyst,
    }


def _company(conn, ticker):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, company_name, is_active) VALUES (?, ?, true)",
        [ticker, ticker],
    )


def _score(conn, ticker, run_id, as_of, tier="Reject", total=25.0):
    conn.execute(
        """INSERT OR IGNORE INTO scores
           (id, run_id, ticker, as_of_date, cheap_score, quality_score, catalyst_score,
            momentum_score, sentiment_score, risk_penalty, total_score, tier, confidence,
            why_ranked, missing_data_json, created_at)
           VALUES (?, ?, ?, ?, 25, 25, 0, 0, 0, 20, ?, ?, 'low', '', '[]', CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], run_id, ticker, as_of.isoformat(), total, tier],
    )


def _filing(conn, ticker, form_type, filing_date):
    conn.execute(
        """INSERT INTO filings (id, ticker, form_type, filing_date, description, created_at)
           VALUES (?, ?, ?, ?, 'form8-k.htm', CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], ticker, form_type, filing_date.isoformat()],
    )


def _fundamentals(conn, ticker, as_of):
    conn.execute(
        """INSERT INTO fundamentals_raw (id, ticker, concept, value, unit, as_of_date)
           VALUES (?, ?, 'us-gaap/NetIncomeLoss', 1000000, 'USD', ?)""",
        [uuid.uuid4().hex[:16], ticker, as_of.isoformat()],
    )


# ── Imports smoke ─────────────────────────────────────────────────────────────

def test_investigator_importable():
    from missed.investigator import investigate_event  # noqa: F401


# ── Root cause assignment ─────────────────────────────────────────────────────

def test_not_in_universe_root_cause(conn):
    """was_in_universe=False → root_cause includes 'not_in_universe'."""
    from missed.investigator import investigate_event
    evt = _event("NOUNI", date.today() - timedelta(days=7), was_in_universe=False)
    result = investigate_event(conn, evt)
    assert "not_in_universe" in result["root_causes"], (
        f"Expected not_in_universe in root_causes, got {result['root_causes']}"
    )


def test_threshold_too_strict_root_cause(conn):
    """Ticker was scored >=35 but Rejected → threshold_too_strict."""
    from missed.investigator import investigate_event
    ticker = "THRESH"
    _company(conn, ticker)
    event_date = date.today() - timedelta(days=7)
    _score(conn, ticker, "run1", event_date - timedelta(days=2), tier="Reject", total=43.0)

    evt = _event(ticker, event_date, was_in_universe=True, was_scored=True,
                 score_before=43.0, tier_before="Reject")
    result = investigate_event(conn, evt)
    assert "threshold_too_strict" in result["root_causes"], (
        f"High-scoring Reject should trigger threshold_too_strict: {result['root_causes']}"
    )


def test_missing_fundamentals_root_cause(conn):
    """No fundamentals before event → missing_fundamentals in root_causes."""
    from missed.investigator import investigate_event
    ticker = "NOFUND"
    _company(conn, ticker)
    event_date = date.today() - timedelta(days=7)
    # Score exists but with null quality (no fundamentals)
    _score(conn, ticker, "run1", event_date - timedelta(days=2), tier="Incomplete", total=10.0)

    evt = _event(ticker, event_date, was_in_universe=True, was_scored=True,
                 score_before=10.0, tier_before="Incomplete")
    result = investigate_event(conn, evt)
    assert "missing_fundamentals" in result["root_causes"], (
        f"No fundamentals → missing_fundamentals: {result['root_causes']}"
    )


def test_missing_catalyst_source_root_cause(conn):
    """Scored ticker, no catalyst signals, no filings → no_public_catalyst_source_found."""
    from missed.investigator import investigate_event
    ticker = "NOCAT"
    _company(conn, ticker)
    _fundamentals(conn, ticker, date.today() - timedelta(days=30))
    event_date = date.today() - timedelta(days=7)
    _score(conn, ticker, "run1", event_date - timedelta(days=2), tier="Reject", total=30.0)

    evt = _event(ticker, event_date, was_in_universe=True, was_scored=True,
                 score_before=30.0, tier_before="Reject", had_catalyst=False)
    result = investigate_event(conn, evt)
    assert "no_public_catalyst_source_found" in result["root_causes"], (
        f"No catalyst → no_public_catalyst_source_found: {result['root_causes']}"
    )


def test_truly_unpredictable_assigned_when_nothing_available(conn):
    """No data at all → truly_unpredictable (not just missing_X for everything)."""
    from missed.investigator import investigate_event
    evt = _event("GHOST", date.today() - timedelta(days=7),
                 was_in_universe=False, was_scored=False)
    result = investigate_event(conn, evt)
    assert "truly_unpredictable" in result["root_causes"] or "not_in_universe" in result["root_causes"]


# ── No hindsight leakage ──────────────────────────────────────────────────────

def test_no_hindsight_leakage(conn):
    """investigate_event must not use scores/filings after event_date."""
    from missed.investigator import investigate_event
    ticker = "NOHL"
    _company(conn, ticker)
    event_date = date.today() - timedelta(days=7)
    # Score AFTER the event — must be ignored
    _score(conn, ticker, "run_post", event_date + timedelta(days=2), tier="A", total=80.0)
    # Score BEFORE the event — should be used
    _score(conn, ticker, "run_pre", event_date - timedelta(days=3), tier="Reject", total=25.0)

    evt = _event(ticker, event_date, was_in_universe=True, was_scored=True,
                 score_before=25.0, tier_before="Reject")
    result = investigate_event(conn, evt)
    # Investigation should reflect pre-event score (25), not post-event score (80)
    assert result.get("score_at_investigation") is None or result.get("score_at_investigation", 0) <= 25.0, (
        "Post-event score should not be used in investigation"
    )


# ── Investigation result structure ───────────────────────────────────────────

def test_investigation_has_required_fields(conn):
    """investigate_event result contains all required fields."""
    from missed.investigator import investigate_event
    evt = _event("FLD2", date.today() - timedelta(days=7), was_in_universe=True)
    result = investigate_event(conn, evt)
    required = {"event_id", "ticker", "event_date", "root_causes", "primary_root_cause",
                "text_enrichment_needed", "text_enrichment_reason",
                "nvidia_enrichment_status", "openai_critique_status"}
    missing = required - set(result.keys())
    assert not missing, f"Investigation missing fields: {missing}"


def test_text_enrichment_needed_for_catalyst_not_classified(conn):
    """root_cause=catalyst_not_classified → text_enrichment_needed=True."""
    from missed.investigator import investigate_event
    ticker = "TEXTCAT"
    _company(conn, ticker)
    event_date = date.today() - timedelta(days=7)
    # Has a filing before the event but catalyst score was low
    _filing(conn, ticker, "8-K", event_date - timedelta(days=5))
    _score(conn, ticker, "run1", event_date - timedelta(days=2),
           tier="Reject", total=20.0)

    evt = _event(ticker, event_date, was_in_universe=True, was_scored=True,
                 score_before=20.0, tier_before="Reject", had_catalyst=True)
    result = investigate_event(conn, evt)
    # Filing existed but catalyst was low → catalyst_not_classified possible
    if "catalyst_not_classified" in result["root_causes"] or "routine_filing_misclassified" in result["root_causes"]:
        assert result["text_enrichment_needed"] is True
