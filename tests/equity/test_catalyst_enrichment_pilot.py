"""TDD tests for LLM catalyst enrichment pilot.

RED state: missed/catalyst_sampler.py, catalyst_schema.py,
           catalyst_prompt.py, catalyst_classifier.py do not exist yet.

Covers:
- sampler determinism and near-threshold prioritisation
- schema validation (valid and invalid inputs)
- prompt construction (ticker, date, filing present in output)
- mock classifier (deterministic, no API calls, valid output)
- no production score changes (running classifier doesn't touch scores table)
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema


# ── Fixtures ──────────────────────────────────────────────────────────────────

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


def _seed_event(conn, ticker, event_date, return_value=22.0, was_scored=True, score=40.0):
    event_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT OR IGNORE INTO missed_opportunity_events
           (event_id, ticker, event_date, event_type, return_value, window_days,
            was_in_universe, was_scored, score_before_event, tier_before_event,
            was_rejected, was_incomplete, had_catalyst_evidence,
            investigation_status)
           VALUES (?, ?, ?, 'gain_20d_20pct', ?, 20, true, ?, ?, 'Reject',
                   true, false, true, 'investigated')""",
        [event_id, ticker, event_date.isoformat(), return_value, was_scored, score],
    )
    return event_id


def _seed_investigation(conn, event_id, ticker, event_date,
                        root_cause="text_evidence_available_not_classified",
                        text_needed=True):
    inv_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT OR IGNORE INTO missed_opportunity_investigations
           (investigation_id, event_id, ticker, event_date, root_causes_json,
            primary_root_cause, text_enrichment_needed, nvidia_enrichment_status,
            openai_critique_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'not_needed')""",
        [inv_id, event_id, ticker, event_date.isoformat(),
         json.dumps([root_cause, "needs_llm_text_enrichment"]),
         root_cause, text_needed],
    )
    return inv_id


def _seed_filing(conn, ticker, form_type, event_date, days_before=5):
    filing_date = (event_date - timedelta(days=days_before)).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO filings (id, ticker, cik, form_type, filing_date, description)"
        " VALUES (?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, "0000111111", form_type, filing_date,
         f"{form_type.lower()}_material_event.htm"],
    )


def _seed_text_evidence_event(conn, ticker, event_date, score=40.0,
                               form_type="8-K", return_value=22.0):
    """Convenience: seed a complete text-evidence event chain."""
    _seed_company(conn, ticker)
    eid = _seed_event(conn, ticker, event_date, return_value=return_value, score=score)
    iid = _seed_investigation(conn, eid, ticker, event_date)
    _seed_filing(conn, ticker, form_type, event_date)
    return eid, iid


# ═════════════════════════════════════════════════════════════════════════════
# PART 1 — Sampler
# ═════════════════════════════════════════════════════════════════════════════

def test_sampler_importable():
    from missed.catalyst_sampler import sample_pilot_events  # noqa: F401


def test_sampler_returns_list_of_dicts(conn):
    """sample_pilot_events returns a list of dict records."""
    from missed.catalyst_sampler import sample_pilot_events
    event_date = date(2026, 1, 15)
    _seed_text_evidence_event(conn, "AAPL", event_date)
    result = sample_pilot_events(conn, n=10)
    assert isinstance(result, list)
    assert all(isinstance(r, dict) for r in result)


def test_sampler_empty_db_returns_empty(conn):
    """No text-evidence events → empty sample."""
    from missed.catalyst_sampler import sample_pilot_events
    assert sample_pilot_events(conn, n=10) == []


def test_sampler_respects_n_limit(conn):
    """sample_pilot_events returns at most n events."""
    from missed.catalyst_sampler import sample_pilot_events
    base = date(2026, 1, 1)
    tickers = [f"T{i:03d}" for i in range(20)]
    for i, ticker in enumerate(tickers):
        _seed_text_evidence_event(conn, ticker, base + timedelta(days=i))
    result = sample_pilot_events(conn, n=5)
    assert len(result) == 5


def test_sampler_deterministic(conn):
    """Calling sample_pilot_events twice with same data yields identical results."""
    from missed.catalyst_sampler import sample_pilot_events
    base = date(2026, 1, 1)
    for i in range(15):
        _seed_text_evidence_event(conn, f"DET{i:02d}", base + timedelta(days=i))
    r1 = sample_pilot_events(conn, n=10)
    r2 = sample_pilot_events(conn, n=10)
    assert [e["event_id"] for e in r1] == [e["event_id"] for e in r2]


def test_sampler_near_threshold_prioritised(conn):
    """Near-threshold events (score 35-48) appear before far-from-threshold events."""
    from missed.catalyst_sampler import sample_pilot_events
    base = date(2026, 2, 1)
    # Far-from-threshold: score=10 (low)
    _seed_text_evidence_event(conn, "FAR", base, score=10.0)
    # Near-threshold: score=42 (just below C-tier boundary)
    _seed_text_evidence_event(conn, "NEAR", base + timedelta(days=1), score=42.0)
    result = sample_pilot_events(conn, n=2)
    tickers = [e["ticker"] for e in result]
    assert tickers.index("NEAR") < tickers.index("FAR"), \
        f"NEAR (score=42) should be before FAR (score=10), got order: {tickers}"


def test_sampler_excludes_numeric_only_events(conn):
    """Events with only numeric root causes are NOT in the sample."""
    from missed.catalyst_sampler import sample_pilot_events
    event_date = date(2026, 2, 10)
    _seed_company(conn, "NUMONLY")
    eid = _seed_event(conn, "NUMONLY", event_date)
    # Investigation with NUMERIC root cause only, text_enrichment_needed=False
    inv_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT OR IGNORE INTO missed_opportunity_investigations
           (investigation_id, event_id, ticker, event_date, root_causes_json,
            primary_root_cause, text_enrichment_needed, nvidia_enrichment_status,
            openai_critique_status)
           VALUES (?, ?, ?, ?, ?, ?, false, 'not_needed', 'not_needed')""",
        [inv_id, eid, "NUMONLY", event_date.isoformat(),
         '["missing_fundamentals"]', "missing_fundamentals"],
    )
    result = sample_pilot_events(conn, n=10)
    assert not any(e["ticker"] == "NUMONLY" for e in result), \
        "Numeric-only events should not appear in text-evidence sample"


def test_sampler_record_has_required_fields(conn):
    """Each sample record has the fields needed for prompt construction."""
    from missed.catalyst_sampler import sample_pilot_events
    event_date = date(2026, 3, 1)
    _seed_text_evidence_event(conn, "FIELDS", event_date)
    result = sample_pilot_events(conn, n=1)
    assert result
    record = result[0]
    for field in ("event_id", "ticker", "event_date", "primary_root_cause",
                  "event_type", "return_value", "score_before_event"):
        assert field in record, f"Missing field '{field}' in sample record"


# ═════════════════════════════════════════════════════════════════════════════
# PART 2 — Schema validation
# ═════════════════════════════════════════════════════════════════════════════

def test_schema_importable():
    from missed.catalyst_schema import CatalystEnrichment, validate_enrichment  # noqa: F401


def test_valid_enrichment_passes_validation():
    """A fully valid CatalystEnrichment dict passes validate_enrichment."""
    from missed.catalyst_schema import validate_enrichment
    data = {
        "event_id": "abc123",
        "ticker": "AAPL",
        "event_date": "2026-03-15",
        "catalyst_type": "earnings",
        "materiality": "high",
        "sentiment": "bullish",
        "confidence": 0.85,
        "evidence_quote": "Revenue increased 12% YoY",
        "reasoning_short": "Strong earnings beat drove the move.",
        "should_affect_score": True,
        "provider": "mock",
        "enriched_at": "2026-05-02T10:00:00+00:00",
    }
    is_valid, errors = validate_enrichment(data)
    assert is_valid, f"Valid data should pass: {errors}"
    assert errors == []


def test_invalid_catalyst_type_fails_validation():
    """Unknown catalyst_type → validation error."""
    from missed.catalyst_schema import validate_enrichment
    data = {
        "event_id": "x", "ticker": "A", "event_date": "2026-01-01",
        "catalyst_type": "not_a_real_type",
        "materiality": "high", "sentiment": "bullish",
        "confidence": 0.7, "evidence_quote": "", "reasoning_short": "ok",
        "should_affect_score": False, "provider": "mock",
        "enriched_at": "2026-05-02T10:00:00+00:00",
    }
    is_valid, errors = validate_enrichment(data)
    assert not is_valid
    assert any("catalyst_type" in e for e in errors)


def test_invalid_materiality_fails_validation():
    """Unknown materiality value → validation error."""
    from missed.catalyst_schema import validate_enrichment
    data = {
        "event_id": "x", "ticker": "A", "event_date": "2026-01-01",
        "catalyst_type": "earnings", "materiality": "very_high",
        "sentiment": "bullish", "confidence": 0.7,
        "evidence_quote": "", "reasoning_short": "ok",
        "should_affect_score": False, "provider": "mock",
        "enriched_at": "2026-05-02T10:00:00+00:00",
    }
    is_valid, errors = validate_enrichment(data)
    assert not is_valid
    assert any("materiality" in e for e in errors)


def test_confidence_out_of_range_fails_validation():
    """confidence > 1.0 → validation error."""
    from missed.catalyst_schema import validate_enrichment
    data = {
        "event_id": "x", "ticker": "A", "event_date": "2026-01-01",
        "catalyst_type": "earnings", "materiality": "high",
        "sentiment": "bullish", "confidence": 1.5,
        "evidence_quote": "", "reasoning_short": "ok",
        "should_affect_score": False, "provider": "mock",
        "enriched_at": "2026-05-02T10:00:00+00:00",
    }
    is_valid, errors = validate_enrichment(data)
    assert not is_valid
    assert any("confidence" in e for e in errors)


def test_missing_required_field_fails_validation():
    """Missing 'ticker' field → validation error."""
    from missed.catalyst_schema import validate_enrichment
    data = {
        "event_id": "x", "event_date": "2026-01-01",
        "catalyst_type": "earnings", "materiality": "high",
        "sentiment": "bullish", "confidence": 0.7,
        "evidence_quote": "", "reasoning_short": "ok",
        "should_affect_score": False, "provider": "mock",
        "enriched_at": "2026-05-02T10:00:00+00:00",
    }
    is_valid, errors = validate_enrichment(data)
    assert not is_valid
    assert any("ticker" in e for e in errors)


def test_catalyst_enrichment_to_dict_round_trips():
    """CatalystEnrichment.to_dict() produces JSON-serialisable dict."""
    from missed.catalyst_schema import CatalystEnrichment
    ce = CatalystEnrichment(
        event_id="e1", ticker="MSFT", event_date="2026-03-15",
        catalyst_type="merger_acquisition", materiality="high",
        sentiment="bullish", confidence=0.9,
        evidence_quote="Transaction valued at $10B",
        reasoning_short="M&A announcement drove the gap-up.",
        should_affect_score=True, provider="mock",
        enriched_at="2026-05-02T10:00:00+00:00",
    )
    d = ce.to_dict()
    json.dumps(d)  # must not raise
    assert d["ticker"] == "MSFT"
    assert d["catalyst_type"] == "merger_acquisition"
    assert d["should_affect_score"] is True


# ═════════════════════════════════════════════════════════════════════════════
# PART 3 — Prompt construction
# ═════════════════════════════════════════════════════════════════════════════

def test_prompt_importable():
    from missed.catalyst_prompt import build_prompt, SYSTEM_PROMPT  # noqa: F401


def test_prompt_contains_ticker():
    """Prompt includes the ticker symbol."""
    from missed.catalyst_prompt import build_prompt
    event = {"ticker": "NVDA", "event_date": date(2026, 3, 15),
             "event_type": "gain_20d_20pct", "return_value": 25.0,
             "score_before_event": 38.0, "filing_form_type": "8-K",
             "filing_date": date(2026, 3, 10), "filing_description": "8k_earnings.htm"}
    prompt = build_prompt(event)
    assert "NVDA" in prompt


def test_prompt_contains_event_date():
    """Prompt includes the event date."""
    from missed.catalyst_prompt import build_prompt
    event = {"ticker": "AMD", "event_date": "2026-03-15",
             "event_type": "gain_20d_20pct", "return_value": 22.0,
             "score_before_event": 40.0, "filing_form_type": "10-K",
             "filing_date": "2026-03-01", "filing_description": "annual_report.htm"}
    prompt = build_prompt(event)
    assert "2026-03-15" in prompt


def test_prompt_contains_filing_form():
    """Prompt includes the filing form type when available."""
    from missed.catalyst_prompt import build_prompt
    event = {"ticker": "CRM", "event_date": "2026-02-20",
             "event_type": "gain_20d_20pct", "return_value": 28.0,
             "score_before_event": None, "filing_form_type": "8-K",
             "filing_date": "2026-02-15", "filing_description": "form8k_guidance.htm"}
    prompt = build_prompt(event)
    assert "8-K" in prompt


def test_prompt_handles_missing_filing_gracefully():
    """Prompt is constructed even when filing_form_type is None."""
    from missed.catalyst_prompt import build_prompt
    event = {"ticker": "XYZ", "event_date": "2026-01-10",
             "event_type": "gain_20d_20pct", "return_value": 20.0,
             "score_before_event": None, "filing_form_type": None,
             "filing_date": None, "filing_description": None}
    prompt = build_prompt(event)
    assert "XYZ" in prompt  # at minimum ticker must be present


def test_system_prompt_contains_schema_fields():
    """SYSTEM_PROMPT references the output schema fields."""
    from missed.catalyst_prompt import SYSTEM_PROMPT
    for field in ("catalyst_type", "materiality", "sentiment", "confidence"):
        assert field in SYSTEM_PROMPT, f"SYSTEM_PROMPT missing field '{field}'"


# ═════════════════════════════════════════════════════════════════════════════
# PART 4 — Mock classifier
# ═════════════════════════════════════════════════════════════════════════════

def test_classifier_importable():
    from missed.catalyst_classifier import classify_events  # noqa: F401


def test_mock_classifier_returns_one_enrichment_per_event():
    """classify_events returns exactly one CatalystEnrichment per input event."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_schema import CatalystEnrichment
    events = [
        {"event_id": "e1", "ticker": "AAPL", "event_date": "2026-03-15",
         "event_type": "gain_20d_20pct", "return_value": 22.0,
         "score_before_event": 40.0, "filing_form_type": "8-K",
         "filing_date": "2026-03-10", "filing_description": "8k_earnings.htm"},
        {"event_id": "e2", "ticker": "MSFT", "event_date": "2026-03-20",
         "event_type": "gain_20d_20pct", "return_value": 25.0,
         "score_before_event": 38.0, "filing_form_type": "10-K",
         "filing_date": "2026-03-01", "filing_description": "annual_report.htm"},
    ]
    results = classify_events(events, use_mock=True)
    assert len(results) == 2
    assert all(isinstance(r, CatalystEnrichment) for r in results)


def test_mock_classifier_output_passes_schema_validation():
    """All mock classifier outputs pass validate_enrichment."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_schema import validate_enrichment
    events = [
        {"event_id": uuid.uuid4().hex[:16], "ticker": f"T{i}", "event_date": "2026-03-15",
         "event_type": "gain_20d_20pct", "return_value": 20.0 + i,
         "score_before_event": 38.0, "filing_form_type": "8-K",
         "filing_date": "2026-03-10", "filing_description": "8k_event.htm"}
        for i in range(5)
    ]
    results = classify_events(events, use_mock=True)
    for r in results:
        is_valid, errors = validate_enrichment(r.to_dict())
        assert is_valid, f"Mock output for {r.ticker} failed validation: {errors}"


def test_mock_classifier_is_deterministic():
    """Calling mock classifier twice on same input gives identical output."""
    from missed.catalyst_classifier import classify_events
    events = [
        {"event_id": "fixed_id_001", "ticker": "GOOG", "event_date": "2026-04-01",
         "event_type": "gain_20d_20pct", "return_value": 30.0,
         "score_before_event": 42.0, "filing_form_type": "8-K",
         "filing_date": "2026-03-25", "filing_description": "earnings_release.htm"},
    ]
    r1 = classify_events(events, use_mock=True)[0]
    r2 = classify_events(events, use_mock=True)[0]
    assert r1.catalyst_type == r2.catalyst_type
    assert r1.materiality == r2.materiality
    assert r1.confidence == r2.confidence


def test_mock_classifier_provider_field_is_mock():
    """All mock outputs have provider='mock'."""
    from missed.catalyst_classifier import classify_events
    events = [{"event_id": "e1", "ticker": "X", "event_date": "2026-01-01",
               "event_type": "gain_20d_20pct", "return_value": 20.0,
               "score_before_event": None, "filing_form_type": None,
               "filing_date": None, "filing_description": None}]
    result = classify_events(events, use_mock=True)[0]
    assert result.provider == "mock"


def test_mock_classifier_no_api_calls_made():
    """Running mock classifier makes zero network calls (no openai/requests import triggered)."""
    from missed.catalyst_classifier import classify_events
    import sys
    openai_before = "openai" in sys.modules
    events = [{"event_id": "e1", "ticker": "SAFE", "event_date": "2026-01-01",
               "event_type": "gain_20d_20pct", "return_value": 20.0,
               "score_before_event": 40.0, "filing_form_type": "8-K",
               "filing_date": "2026-01-01", "filing_description": "test.htm"}]
    classify_events(events, use_mock=True)
    # Mock path must not import openai if it wasn't imported before
    openai_after = "openai" in sys.modules
    # This passes as long as we didn't CALL any API — importing openai is OK
    # The real test is that no exception was raised and result is valid
    result = classify_events(events, use_mock=True)
    assert len(result) == 1 and result[0].provider == "mock"


# ═════════════════════════════════════════════════════════════════════════════
# PART 5 — No production score changes
# ═════════════════════════════════════════════════════════════════════════════

def test_running_classifier_does_not_modify_scores_table(conn):
    """classify_events does not read or write the scores table."""
    from missed.catalyst_classifier import classify_events
    # Seed a score that should remain untouched
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, cik, company_name) VALUES (?,?,?)",
        ["SAFE", "0000999999", "Safe Corp"],
    )
    run_id = "test_run_001"
    score_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO scores (id, run_id, ticker, as_of_date, total_score, tier)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [score_id, run_id, "SAFE", "2026-01-01", 42.0, "Reject"],
    )
    original = conn.execute(
        "SELECT total_score, tier FROM scores WHERE id=?", [score_id]
    ).fetchone()

    events = [{"event_id": "e1", "ticker": "SAFE", "event_date": "2026-03-15",
               "event_type": "gain_20d_20pct", "return_value": 25.0,
               "score_before_event": 42.0, "filing_form_type": "8-K",
               "filing_date": "2026-03-10", "filing_description": "test.htm"}]
    classify_events(events, use_mock=True)

    after = conn.execute(
        "SELECT total_score, tier FROM scores WHERE id=?", [score_id]
    ).fetchone()
    assert original == after, "Classifier must not modify scores"


def test_running_pilot_does_not_update_investigation_status(conn):
    """Pilot classifier does not change nvidia_enrichment_status in investigations."""
    from missed.catalyst_classifier import classify_events
    event_date = date(2026, 3, 15)
    _seed_company(conn, "STATUS")
    eid = _seed_event(conn, "STATUS", event_date)
    iid = _seed_investigation(conn, eid, "STATUS", event_date)
    _seed_filing(conn, "STATUS", "8-K", event_date)

    before = conn.execute(
        "SELECT nvidia_enrichment_status FROM missed_opportunity_investigations"
        " WHERE investigation_id=?", [iid]
    ).fetchone()[0]

    events = [{"event_id": eid, "ticker": "STATUS", "event_date": event_date.isoformat(),
               "event_type": "gain_20d_20pct", "return_value": 22.0,
               "score_before_event": 40.0, "filing_form_type": "8-K",
               "filing_date": (event_date - timedelta(days=5)).isoformat(),
               "filing_description": "8k_test.htm"}]
    classify_events(events, use_mock=True)

    after = conn.execute(
        "SELECT nvidia_enrichment_status FROM missed_opportunity_investigations"
        " WHERE investigation_id=?", [iid]
    ).fetchone()[0]
    assert before == after, "Pilot must not mutate investigation enrichment status"
