"""LLM activation policy for missed-opportunity learning — TDD suite.

From the addendum:
- Deterministic first: numeric root causes must NOT trigger LLM
- NVIDIA: text-related root causes (catalyst_not_classified, routine_filing_misclassified,
  missing_catalyst_source with filing evidence)
- OpenAI: limited to top complex cases (max_events cap)
- Thresholds: 5+ text-related misses/week OR >30% share → enable auto enrichment
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


def _investigation(conn, ticker, root_cause, has_filing_before_event=False):
    event_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO missed_opportunity_events
           (event_id, ticker, event_date, event_type, return_value, window_days,
            was_in_universe, was_scored, had_catalyst_evidence, investigation_status)
           VALUES (?, ?, ?, 'gain_5d_10pct', 12.0, 5, true, true, ?, 'investigated')""",
        [event_id, ticker, (date.today() - timedelta(days=3)).isoformat(),
         has_filing_before_event],
    )
    inv_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO missed_opportunity_investigations
           (investigation_id, event_id, ticker, event_date, root_causes_json,
            primary_root_cause, text_enrichment_needed, nvidia_enrichment_status,
            openai_critique_status)
           VALUES (?, ?, ?, ?, ?, ?, false, 'not_needed', 'not_needed')""",
        [inv_id, event_id, ticker, (date.today() - timedelta(days=3)).isoformat(),
         f'["{root_cause}"]', root_cause],
    )
    return inv_id


# ── Imports smoke ─────────────────────────────────────────────────────────────

def test_llm_policy_importable():
    from missed.llm_policy import (  # noqa: F401
        is_text_enrichment_needed,
        get_nvidia_eligible_investigations,
        get_openai_critique_candidates,
        should_enable_auto_enrichment,
    )


# ── Numeric root causes → no LLM ─────────────────────────────────────────────

def test_numeric_root_cause_does_not_trigger_llm(conn):
    """not_in_universe root cause → text_enrichment_needed=False."""
    from missed.llm_policy import is_text_enrichment_needed
    inv_id = _investigation(conn, "NUM1", "not_in_universe")
    assert is_text_enrichment_needed(conn, inv_id) is False


def test_missing_fundamentals_does_not_trigger_llm(conn):
    """missing_fundamentals root cause → text_enrichment_needed=False."""
    from missed.llm_policy import is_text_enrichment_needed
    inv_id = _investigation(conn, "NUM2", "missing_fundamentals")
    assert is_text_enrichment_needed(conn, inv_id) is False


def test_threshold_too_strict_does_not_trigger_llm(conn):
    """threshold_too_strict root cause → text_enrichment_needed=False."""
    from missed.llm_policy import is_text_enrichment_needed
    inv_id = _investigation(conn, "NUM3", "threshold_too_strict")
    assert is_text_enrichment_needed(conn, inv_id) is False


# ── Text root causes → NVIDIA eligible ───────────────────────────────────────

def test_filing_before_move_triggers_nvidia_eligibility(conn):
    """had_catalyst_evidence=True + catalyst_not_classified → NVIDIA eligible."""
    from missed.llm_policy import get_nvidia_eligible_investigations
    _investigation(conn, "TXT1", "catalyst_not_classified", has_filing_before_event=True)
    eligible = get_nvidia_eligible_investigations(conn)
    tickers = [e["ticker"] for e in eligible]
    assert "TXT1" in tickers, f"Filing-before-move should be NVIDIA eligible, got {tickers}"


def test_routine_filing_misclassified_triggers_nvidia(conn):
    """routine_filing_misclassified root cause → NVIDIA eligible."""
    from missed.llm_policy import get_nvidia_eligible_investigations
    _investigation(conn, "TXT2", "routine_filing_misclassified", has_filing_before_event=True)
    eligible = get_nvidia_eligible_investigations(conn)
    tickers = [e["ticker"] for e in eligible]
    assert "TXT2" in tickers


# ── Auto-enrichment threshold ─────────────────────────────────────────────────

def test_below_threshold_auto_enrichment_disabled(conn):
    """3 text-related misses this week → auto enrichment NOT enabled."""
    from missed.llm_policy import should_enable_auto_enrichment
    for i in range(3):
        _investigation(conn, f"BLW{i}", "catalyst_not_classified", True)
    assert should_enable_auto_enrichment(conn) is False


def test_above_threshold_auto_enrichment_enabled(conn):
    """5+ text-related misses this week → auto enrichment should be enabled."""
    from missed.llm_policy import should_enable_auto_enrichment
    for i in range(6):
        _investigation(conn, f"ABV{i}", "catalyst_not_classified", True)
    # Also seed numeric ones to test share calculation
    for i in range(2):
        _investigation(conn, f"NUM{i}", "not_in_universe")
    # 6 text / 8 total = 75% > 30% threshold
    assert should_enable_auto_enrichment(conn) is True


# ── OpenAI cap ────────────────────────────────────────────────────────────────

def test_openai_critique_limited_to_max_events(conn):
    """get_openai_critique_candidates returns at most MAX_OPENAI_EVENTS items."""
    from missed.llm_policy import get_openai_critique_candidates, MAX_OPENAI_EVENTS
    for i in range(20):
        _investigation(conn, f"OA{i}", "catalyst_not_classified", True)
    candidates = get_openai_critique_candidates(conn)
    assert len(candidates) <= MAX_OPENAI_EVENTS, (
        f"OpenAI candidates should be capped at {MAX_OPENAI_EVENTS}, got {len(candidates)}"
    )
