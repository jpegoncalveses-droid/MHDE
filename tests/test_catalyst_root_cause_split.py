"""TDD tests for catalyst root cause refinement.

Splits the broad 'catalyst_not_classified' into 5 precise causes.
RED state: new root causes do not exist in labels.py or investigator.py yet.
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


def _seed_company(conn, ticker):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, cik, company_name) VALUES (?,?,?)",
        [ticker, "0000111111", f"Corp {ticker}"],
    )


def _seed_score(conn, ticker, run_id, as_of_date, catalyst_score=5.0, total=30.0):
    conn.execute(
        "INSERT OR IGNORE INTO scores (id, run_id, ticker, as_of_date, total_score, tier, catalyst_score)"
        " VALUES (?,?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], run_id, ticker, as_of_date.isoformat(), total, "Reject", catalyst_score],
    )


def _seed_filing(conn, ticker, form_type, filing_date):
    conn.execute(
        "INSERT OR IGNORE INTO filings (id, ticker, cik, form_type, filing_date) VALUES (?,?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, "0000111111", form_type, filing_date.isoformat()],
    )


def _seed_prices(conn, ticker, n, max_date):
    for i in range(n):
        d = (max_date - timedelta(days=n - 1 - i)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO prices_daily (id, ticker, trade_date, close, source)"
            " VALUES (?,?,?,?,?)",
            [uuid.uuid4().hex[:16], ticker, d, 100.0, "stooq"],
        )


def _make_event(ticker, event_date, was_scored=True, had_catalyst=True):
    return {
        "event_id": uuid.uuid4().hex[:16],
        "ticker": ticker,
        "event_date": event_date,
        "event_type": "gain_20d_20pct",
        "return_value": 22.0,
        "window_days": 20,
        "was_in_universe": True,
        "was_scored": was_scored,
        "score_before_event": 30.0 if was_scored else None,
        "tier_before_event": "Reject" if was_scored else None,
        "was_rejected": was_scored,
        "was_incomplete": False,
        "had_catalyst_evidence": had_catalyst,
    }


# ── New root cause labels ─────────────────────────────────────────────────────

def test_new_root_causes_in_labels():
    """New root causes are present in ROOT_CAUSES list."""
    from missed.labels import ROOT_CAUSES
    for cause in [
        "text_evidence_available_not_classified",
        "no_public_catalyst_source_found",
        "price_move_without_known_catalyst",
        "needs_llm_text_enrichment",
        "routine_event_correctly_suppressed",
    ]:
        assert cause in ROOT_CAUSES, f"'{cause}' missing from ROOT_CAUSES"


def test_text_evidence_causes_are_text_related():
    """text_evidence_available_not_classified and needs_llm are in TEXT_RELATED_ROOT_CAUSES."""
    from missed.labels import TEXT_RELATED_ROOT_CAUSES
    assert "text_evidence_available_not_classified" in TEXT_RELATED_ROOT_CAUSES
    assert "needs_llm_text_enrichment" in TEXT_RELATED_ROOT_CAUSES


def test_no_public_catalyst_is_numeric():
    """no_public_catalyst_source_found is in NUMERIC_ROOT_CAUSES."""
    from missed.labels import NUMERIC_ROOT_CAUSES
    assert "no_public_catalyst_source_found" in NUMERIC_ROOT_CAUSES


# ── Investigation root cause assignment ──────────────────────────────────────

def test_material_filing_low_catalyst_gets_text_evidence_cause(conn):
    """8-K filing before event + low catalyst score → text_evidence_available_not_classified."""
    from missed.investigator import investigate_event
    ticker = "AAPL"
    event_date = date(2026, 3, 15)
    _seed_company(conn, ticker)
    _seed_prices(conn, ticker, 10, event_date - timedelta(days=1))
    _seed_score(conn, ticker, "run1", event_date - timedelta(days=1), catalyst_score=5.0)
    _seed_filing(conn, ticker, "8-K", event_date - timedelta(days=5))  # material filing
    event = _make_event(ticker, event_date, had_catalyst=True)
    inv = investigate_event(conn, event)
    assert "text_evidence_available_not_classified" in inv["root_causes"], \
        f"Expected text_evidence_available_not_classified, got: {inv['root_causes']}"


def test_only_routine_filings_gets_routine_suppressed(conn):
    """Only Form 4 / SC 13G filings before event → routine_event_correctly_suppressed."""
    from missed.investigator import investigate_event
    ticker = "MSFT"
    event_date = date(2026, 3, 15)
    _seed_company(conn, ticker)
    _seed_prices(conn, ticker, 10, event_date - timedelta(days=1))
    _seed_score(conn, ticker, "run1", event_date - timedelta(days=1), catalyst_score=5.0)
    _seed_filing(conn, ticker, "4", event_date - timedelta(days=3))      # Form 4 — insider trade
    _seed_filing(conn, ticker, "SC 13G", event_date - timedelta(days=7))  # passive ownership
    event = _make_event(ticker, event_date, had_catalyst=True)
    inv = investigate_event(conn, event)
    assert "routine_event_correctly_suppressed" in inv["root_causes"], \
        f"Expected routine_event_correctly_suppressed, got: {inv['root_causes']}"


def test_no_filings_no_score_gets_price_move_without_catalyst(conn):
    """No filings, not scored → price_move_without_known_catalyst."""
    from missed.investigator import investigate_event
    ticker = "NVDA"
    event_date = date(2026, 3, 15)
    _seed_company(conn, ticker)
    _seed_prices(conn, ticker, 10, event_date - timedelta(days=1))
    # No score seeded, no filings seeded
    event = _make_event(ticker, event_date, was_scored=False, had_catalyst=False)
    inv = investigate_event(conn, event)
    assert "price_move_without_known_catalyst" in inv["root_causes"], \
        f"Expected price_move_without_known_catalyst, got: {inv['root_causes']}"


def test_scored_no_filing_gets_no_public_catalyst(conn):
    """Scored ticker, no filings → no_public_catalyst_source_found (was: missing_catalyst_source)."""
    from missed.investigator import investigate_event
    ticker = "AIG"
    event_date = date(2026, 3, 15)
    _seed_company(conn, ticker)
    _seed_prices(conn, ticker, 10, event_date - timedelta(days=1))
    _seed_score(conn, ticker, "run1", event_date - timedelta(days=1), catalyst_score=5.0)
    # No filings
    event = _make_event(ticker, event_date, had_catalyst=False)
    inv = investigate_event(conn, event)
    assert "no_public_catalyst_source_found" in inv["root_causes"], \
        f"Expected no_public_catalyst_source_found, got: {inv['root_causes']}"


def test_catalyst_not_classified_no_longer_primary_for_material_filing(conn):
    """'catalyst_not_classified' is replaced — should NOT be the primary for material 8-K."""
    from missed.investigator import investigate_event
    ticker = "PFE"
    event_date = date(2026, 3, 15)
    _seed_company(conn, ticker)
    _seed_prices(conn, ticker, 10, event_date - timedelta(days=1))
    _seed_score(conn, ticker, "run1", event_date - timedelta(days=1), catalyst_score=5.0)
    _seed_filing(conn, ticker, "8-K", event_date - timedelta(days=3))
    event = _make_event(ticker, event_date, had_catalyst=True)
    inv = investigate_event(conn, event)
    assert inv["primary_root_cause"] != "catalyst_not_classified", \
        "catalyst_not_classified should be replaced by more specific cause"


def test_routine_suppressed_not_llm_eligible(conn):
    """routine_event_correctly_suppressed is not in TEXT_RELATED_ROOT_CAUSES."""
    from missed.labels import TEXT_RELATED_ROOT_CAUSES
    assert "routine_event_correctly_suppressed" not in TEXT_RELATED_ROOT_CAUSES
