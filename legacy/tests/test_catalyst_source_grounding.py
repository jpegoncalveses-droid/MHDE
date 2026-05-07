"""TDD tests for source-text grounding in the LLM catalyst pilot.

RED first — these fail until the implementation is in place.
"""
from __future__ import annotations

import json

import pytest

from missed.catalyst_classifier import classify_events
from missed.catalyst_prompt import build_prompt
from missed.catalyst_providers import BaseCatalystProvider
from missed.catalyst_schema import CatalystEnrichment
from missed.catalyst_source_resolver import (
    MIN_SOURCE_TEXT_CHARS,
    enrich_events_with_source,
    resolve_source_text,
    validate_evidence_quote,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_event(event_id: str, ticker: str, *, filing_form_type: str = "8-K") -> dict:
    return {
        "event_id": event_id,
        "ticker": ticker,
        "event_date": "2026-01-01",
        "filing_form_type": filing_form_type,
        "filing_date": "2025-12-15",
        "filing_description": "test-document.htm",
        "accession_number": "0001234567-26-000001",
        "cik": "1234567",
        "return_value": 12.5,
    }


def _make_event_with_source(event_id: str, ticker: str, source_text: str) -> dict:
    """Return an event dict with source_text already resolved."""
    e = _make_event(event_id, ticker)
    e["source_text"] = source_text
    e["source_text_char_count"] = len(source_text)
    e["source_text_origin"] = "sec_url" if source_text else "unavailable"
    e["source_text_error"] = None if source_text else "no_doc_url"
    return e


class _RecordingProvider(BaseCatalystProvider):
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, event: dict, prompt: str) -> CatalystEnrichment:
        self.calls.append(event.get("event_id", ""))
        return CatalystEnrichment(
            event_id=event.get("event_id", ""),
            ticker=event.get("ticker", ""),
            event_date="2026-01-01",
            catalyst_type="management_change",
            materiality="high",
            sentiment="bullish",
            confidence=0.9,
            evidence_quote="some made-up quote",
            reasoning_short="executive appointed CEO",
            should_affect_score=True,
            provider="recording",
            enriched_at="2026-01-01T00:00:00+00:00",
        )


def _good_source_text() -> str:
    return "Q" * (MIN_SOURCE_TEXT_CHARS + 100)


# ── 1. Missing source text skips provider ─────────────────────────────────────

def test_missing_source_text_skips_provider_call():
    """Event with no source text (char_count=0) → provider.classify never called."""
    event = _make_event_with_source("e1", "AAPL", "")
    provider = _RecordingProvider()
    results = classify_events([event], _provider=provider, cache_path=None)

    assert len(provider.calls) == 0
    assert len(results) == 1
    assert results[0].catalyst_type == "unknown"
    assert "[SKIP]" in results[0].reasoning_short


# ── 2. Short source text skips provider ───────────────────────────────────────

def test_short_source_text_skips_provider_call():
    """Event with source_text below MIN threshold → provider skipped."""
    short_text = "X" * (MIN_SOURCE_TEXT_CHARS - 1)
    event = _make_event_with_source("e1", "AAPL", short_text)
    provider = _RecordingProvider()
    results = classify_events([event], _provider=provider, cache_path=None)

    assert len(provider.calls) == 0
    assert "[SKIP]" in results[0].reasoning_short


# ── 3. Verbatim quote passes validation ──────────────────────────────────────

def test_verbatim_quote_passes_validation():
    """Quote that appears verbatim in source_text → validate_evidence_quote returns True."""
    source = "We expect full-year revenue guidance of $10–11 billion."
    quote = "full-year revenue guidance of $10–11 billion"
    assert validate_evidence_quote(quote, source) is True


def test_verbatim_quote_case_insensitive():
    """Verbatim match is case-insensitive."""
    source = "QUARTERLY EARNINGS BEAT ESTIMATES by 15%."
    quote = "quarterly earnings beat estimates by 15%"
    assert validate_evidence_quote(quote, source) is True


# ── 4. Unsupported quote fails validation ────────────────────────────────────

def test_unsupported_quote_fails_validation():
    """Quote not present in source_text → validate_evidence_quote returns False."""
    source = "The company filed its annual report."
    quote = "record-breaking acquisition of $5 billion"
    assert validate_evidence_quote(quote, source) is False


def test_empty_quote_trivially_passes():
    """Empty evidence_quote → no claim to validate → True."""
    assert validate_evidence_quote("", "any source text") is True


# ── 5. Form 4 cannot become management_change ────────────────────────────────

def test_form4_cannot_become_management_change():
    """Form 4 is a non-text filing: resolver returns unavailable, LLM never called."""
    event = _make_event("e1", "AAPL", filing_form_type="4")
    resolved = resolve_source_text(event)

    assert resolved["source_text_origin"] == "unavailable"
    assert resolved["source_text_char_count"] == 0
    assert "non_text_filing" in (resolved["source_text_error"] or "")

    enriched = dict(event, **resolved)
    provider = _RecordingProvider()
    results = classify_events([enriched], _provider=provider, cache_path=None)

    assert len(provider.calls) == 0
    assert results[0].catalyst_type == "unknown"


# ── 6. Prompt includes source_text ───────────────────────────────────────────

def test_prompt_includes_source_text():
    """build_prompt embeds source_text in the user message when present."""
    event = _make_event("e1", "AAPL")
    event["source_text"] = "We are raising our earnings per share guidance to $4.50."
    event["source_text_origin"] = "sec_url"
    event["source_text_char_count"] = len(event["source_text"])

    prompt = build_prompt(event)

    assert "We are raising our earnings per share guidance to $4.50." in prompt


def test_prompt_includes_verbatim_instruction():
    """build_prompt instructs model to copy evidence_quote verbatim from source text."""
    event = _make_event("e1", "AAPL")
    event["source_text"] = "Q" * 300
    event["source_text_origin"] = "sec_url"
    event["source_text_char_count"] = 300

    prompt = build_prompt(event)

    assert "verbatim" in prompt.lower()


# ── 7. No scoring changes ─────────────────────────────────────────────────────

def test_no_production_scoring_changes(tmp_path):
    """Running classify_events with source grounding does not modify the scores table."""
    import duckdb
    conn = duckdb.connect(str(tmp_path / "test.duckdb"))
    conn.execute(
        "CREATE TABLE scores (run_id VARCHAR, ticker VARCHAR, total_score DOUBLE)"
    )
    conn.execute("INSERT INTO scores VALUES ('r1', 'AAPL', 85.0)")
    before = conn.execute("SELECT total_score FROM scores").fetchall()

    event = _make_event_with_source("e1", "AAPL", _good_source_text())
    classify_events([event], _provider=_RecordingProvider(), cache_path=None)

    after = conn.execute("SELECT total_score FROM scores").fetchall()
    assert before == after
    conn.close()


# ── 8. Resolver marks non-text forms as unavailable ──────────────────────────

def test_resolver_marks_non_text_form_unavailable():
    """resolve_source_text returns unavailable for SC 13G."""
    event = _make_event("e1", "AAPL", filing_form_type="SC 13G")
    result = resolve_source_text(event)
    assert result["source_text_origin"] == "unavailable"
    assert result["source_text_char_count"] == 0


# ── 9. Invalid quote marked in enrichment ────────────────────────────────────

def test_invalid_quote_marked_in_enrichment():
    """When provider returns a quote not in source_text, enrichment gets [INVALID_QUOTE] tag."""
    source_text = _good_source_text()  # 300+ chars of 'Q's, no made-up quote
    event = _make_event_with_source("e1", "AAPL", source_text)

    provider = _RecordingProvider()  # returns evidence_quote="some made-up quote"
    results = classify_events([event], _provider=provider, cache_path=None)

    assert "[INVALID_QUOTE]" in results[0].reasoning_short


def test_valid_quote_not_marked_invalid():
    """When provider returns a quote found in source_text, no [INVALID_QUOTE] tag."""
    quote = "some made-up quote"
    source_text = f"Document text: {quote} end."
    source_text += "X" * 300  # ensure above threshold
    event = _make_event_with_source("e1", "AAPL", source_text)

    provider = _RecordingProvider()  # returns evidence_quote="some made-up quote"
    results = classify_events([event], _provider=provider, cache_path=None)

    assert "[INVALID_QUOTE]" not in results[0].reasoning_short


# ── 10. Report includes source text stats ────────────────────────────────────

def test_report_includes_source_text_stats(tmp_path):
    """generate_pilot_report emits source_text and quote validation sections."""
    from missed.catalyst_report import generate_pilot_report

    source_text = _good_source_text()
    sample = [_make_event_with_source("e1", "AAPL", source_text)]
    enriched = [
        CatalystEnrichment(
            event_id="e1", ticker="AAPL", event_date="2026-01-01",
            catalyst_type="earnings", materiality="high", sentiment="bullish",
            confidence=0.9, evidence_quote="QQQ", reasoning_short="[INVALID_QUOTE] original",
            should_affect_score=True, provider="openai",
            enriched_at="2026-01-01T00:00:00+00:00",
        )
    ]

    md_path, _ = generate_pilot_report(sample, enriched, str(tmp_path))
    md = open(md_path).read()

    assert "source_text" in md.lower() or "Source Text" in md
    assert "unsupported" in md.lower() or "invalid_quote" in md.lower() or "Invalid Quote" in md
