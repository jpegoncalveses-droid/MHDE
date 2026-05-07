"""TDD tests for the deterministic catalyst interpretation layer.

No LLM calls, no DB, no scoring changes.
"""
from __future__ import annotations

import csv
import os
import tempfile


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _entry(
    catalyst_type="merger_acquisition",
    sentiment="bullish",
    confidence=0.9,
    materiality="high",
    original_score=43.0,
    shadow_score=48.0,
    tier_move="Reject→C",
    evidence_quote="",
    final_should_affect_score=True,
    llm_adjustment=5.0,
    validation_status="valid",
    **extra,
) -> dict:
    return {
        "ticker": extra.pop("ticker", "TEST"),
        "event_date": "2026-05-02",
        "filing_form_type": "8-K",
        "constructed_url": None,
        "catalyst_type": catalyst_type,
        "materiality": materiality,
        "sentiment": sentiment,
        "confidence": confidence,
        "evidence_quote": evidence_quote,
        "validation_status": validation_status,
        "quote_validation_pass": True,
        "final_should_affect_score": final_should_affect_score,
        "original_score": original_score,
        "original_tier": "Reject",
        "llm_adjustment": llm_adjustment,
        "shadow_score": shadow_score,
        "shadow_tier": "C",
        "tier_move": tier_move,
        **extra,
    }


# ── 1. CTRA-style all-stock merger → event_dependent / watch ─────────────────

def test_ctra_all_stock_merger_gets_event_dependent_watch():
    """M&A entry with 'exchange ratio' in evidence → event_dependent direction, watch guidance."""
    from missed.catalyst_interpretation import interpret_catalyst
    e = _entry(
        catalyst_type="merger_acquisition",
        sentiment="bullish",
        evidence_quote="The all-stock deal uses an exchange ratio of 0.4 shares per target share.",
    )
    result = interpret_catalyst(e)
    assert result["expected_direction"] == "event_dependent"
    assert result["action_guidance"] == "watch"
    assert "exchange ratio" in result["key_checks"].lower() or "exchange" in " ".join(result.get("key_checks_list", [])).lower()


# ── 2. VG-style settlement/commercial delivery → bullish, accept or watch ────

def test_vg_settlement_commercial_delivery_gets_bullish_guidance():
    """Regulatory settlement with delivery language → bullish direction, accept or watch."""
    from missed.catalyst_interpretation import interpret_catalyst
    e = _entry(
        catalyst_type="regulatory_approval",
        sentiment="bullish",
        confidence=0.85,
        evidence_quote="VG completed cargo delivery following regulatory settlement.",
    )
    result = interpret_catalyst(e)
    assert result["expected_direction"] == "bullish"
    assert result["action_guidance"] in ("accept", "watch")
    assert "settlement" in result["key_checks"].lower() or "delivery" in result["key_checks"].lower()


# ── 3. Management change defaults to investigate ──────────────────────────────

def test_management_change_defaults_to_investigate():
    """Management change catalyst → action_guidance is investigate, not accept."""
    from missed.catalyst_interpretation import interpret_catalyst
    e = _entry(
        catalyst_type="management_change",
        sentiment="neutral",
        confidence=0.7,
        evidence_quote="CEO transition announced.",
    )
    result = interpret_catalyst(e)
    assert result["action_guidance"] == "investigate"
    assert result["expected_direction"] == "neutral"


# ── 4. Bearish catalyst produces bearish guidance ─────────────────────────────

def test_bearish_catalyst_produces_bearish_direction():
    """Bearish sentiment on earnings → bearish expected_direction, reject or watch."""
    from missed.catalyst_interpretation import interpret_catalyst
    e = _entry(
        catalyst_type="earnings",
        sentiment="bearish",
        confidence=0.8,
        evidence_quote="Revenue missed consensus by 12%. Guidance cut significantly.",
    )
    result = interpret_catalyst(e)
    assert result["expected_direction"] == "bearish"
    assert result["action_guidance"] in ("reject", "watch")


# ── 5. Guidance fields appear in CSV output ───────────────────────────────────

def test_guidance_fields_appear_in_csv(tmp_path):
    """generate_queue_report() includes interpretation columns in the CSV."""
    from missed.catalyst_queue import generate_queue_report, _enrich_with_interpretation
    entries = [_entry(ticker="CTRA")]
    _enrich_with_interpretation(entries)
    md, csv_path, jsonl = generate_queue_report(entries, [], str(tmp_path))
    rows = list(csv.DictReader(open(csv_path)))
    assert len(rows) > 0
    assert "action_guidance" in rows[0]
    assert "expected_direction" in rows[0]
    assert "expected_timeframe" in rows[0]


# ── 6. Guidance fields appear in HTML artifact ────────────────────────────────

def test_guidance_fields_appear_in_html(tmp_path):
    """generate_html_report() includes action_guidance / expected_direction in output."""
    from missed.catalyst_queue import generate_html_report, _enrich_with_interpretation
    entries = [_entry(ticker="CTRA")]
    _enrich_with_interpretation(entries)
    html_path = generate_html_report(entries, [], str(tmp_path))
    content = open(html_path).read().lower()
    assert "action" in content or "guidance" in content
    assert "direction" in content or "bullish" in content or "watch" in content


# ── 7. Guidance fields appear in markdown report ──────────────────────────────

def test_guidance_fields_appear_in_markdown(tmp_path):
    """generate_queue_report() markdown includes interpretation for crossing candidates."""
    from missed.catalyst_queue import generate_queue_report, _enrich_with_interpretation
    entries = [_entry(ticker="CTRA", tier_move="Reject→C")]
    _enrich_with_interpretation(entries)
    md_path, _, _ = generate_queue_report(entries, [], str(tmp_path))
    content = open(md_path).read().lower()
    assert "action" in content or "guidance" in content or "watch" in content or "accept" in content


# ── 8. Guidance fields appear in email digest ─────────────────────────────────

def test_guidance_fields_appear_in_digest():
    """generate_digest_txt() includes interpretation summary for crossing candidates."""
    from missed.catalyst_digest import generate_digest_txt, _enrich_with_interpretation as enrich_d
    from missed.catalyst_queue import _enrich_with_interpretation
    entries = [_entry(ticker="CTRA", tier_move="Reject→C")]
    _enrich_with_interpretation(entries)
    txt = generate_digest_txt(entries, [], {"run_date": "2026-05-02"})
    lower = txt.lower()
    assert "action" in lower or "guidance" in lower or "direction" in lower


# ── 9. No production score mutation from interpretation ───────────────────────

def test_no_production_score_mutation():
    """interpret_catalyst() returns a new dict; it never mutates original_score or shadow_score."""
    from missed.catalyst_interpretation import interpret_catalyst
    e = _entry(original_score=43.0, shadow_score=48.0)
    orig_score_before = e["original_score"]
    shadow_score_before = e["shadow_score"]
    result = interpret_catalyst(e)
    assert e["original_score"] == orig_score_before
    assert e["shadow_score"] == shadow_score_before
    assert "original_score" not in result
    assert "shadow_score" not in result
