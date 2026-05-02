"""TDD tests for catalyst validation hardening (Task 4).

RED first — these fail until implementation is in place:
  - check_catalyst_sufficiency does not exist yet (ImportError)
  - model_should_affect_score field not on CatalystEnrichment (AttributeError)
  - validate_evidence_quote has no normalization (whitespace / curly quotes)
  - classifier does not force should_affect_score=False on invalid quote
"""
from __future__ import annotations

import pytest

from missed.catalyst_source_resolver import (
    check_catalyst_sufficiency,
    validate_evidence_quote,
)
from missed.catalyst_schema import CatalystEnrichment


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_enrichment(
    *,
    should_affect_score: bool = True,
    evidence_quote: str = "revenue increased 15%",
    catalyst_type: str = "earnings",
    reasoning_short: str = "Q4 earnings beat",
) -> CatalystEnrichment:
    return CatalystEnrichment(
        event_id="evt_test",
        ticker="TEST",
        event_date="2026-01-15",
        catalyst_type=catalyst_type,
        materiality="high",
        sentiment="bullish",
        confidence=0.9,
        evidence_quote=evidence_quote,
        reasoning_short=reasoning_short,
        should_affect_score=should_affect_score,
        provider="mock",
        enriched_at="2026-01-15T12:00:00+00:00",
    )


# ── 1. Invalid quote forces should_affect_score=False ────────────────────────

def test_invalid_quote_forces_should_affect_score_false():
    """When LLM says should_affect_score=True but quote not in source, final=False."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_providers import BaseCatalystProvider

    class AlwaysTrueMock(BaseCatalystProvider):
        name = "always_true_mock"

        def classify(self, event, prompt):
            return _make_enrichment(
                should_affect_score=True,
                evidence_quote="hallucinated quote not in source at all",
            )

    source_text = "General business overview and strategic direction for the company. " * 5
    event = {
        "event_id": "evt_invalid_q",
        "ticker": "TEST",
        "event_date": "2026-01-15",
        "source_text": source_text,
        "source_text_char_count": len(source_text),
        "source_text_origin": "sec_url",
        "source_text_error": None,
    }
    results = classify_events([event], _provider=AlwaysTrueMock(), cache_path=None)
    assert len(results) == 1
    r = results[0]
    assert r.should_affect_score is False, "invalid quote must force should_affect_score=False"


# ── 2. Weak "business updates" is not actionable (GLW pattern) ───────────────

def test_guidance_business_updates_is_weak_evidence():
    """'Will provide business updates' is not a real catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "guidance",
        "Mr. Schlesinger will be providing business updates.",
    )
    assert ok is False
    assert reason


def test_check_catalyst_sufficiency_returns_tuple():
    """check_catalyst_sufficiency always returns (bool, str)."""
    result = check_catalyst_sufficiency("unknown", "")
    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], bool) and isinstance(result[1], str)


# ── 3. "Issued a press release" alone is not earnings evidence ───────────────

def test_earnings_pr_wrapper_is_insufficient():
    """'Issued a press release announcing financial results' is not actual evidence."""
    ok, reason = check_catalyst_sufficiency(
        "earnings",
        "The company issued a press release announcing financial results for Q4.",
    )
    assert ok is False


# ── 4. Valid M&A agreement quote passes ──────────────────────────────────────

def test_valid_ma_quote_passes_sufficiency():
    """Definitive agreement language satisfies M&A catalyst sufficiency."""
    ok, reason = check_catalyst_sufficiency(
        "merger_acquisition",
        "entered into a definitive agreement to acquire XYZ Corp for $2.5 billion in cash.",
    )
    assert ok is True
    assert reason == ""


# ── 5. Whitespace-normalized quote passes ────────────────────────────────────

def test_whitespace_normalized_quote_passes():
    """Quote with single spaces matches source with collapsed extra whitespace."""
    source = "revenue  increased  15%  year  over  year"
    quote = "revenue increased 15% year over year"
    assert validate_evidence_quote(quote, source) is True


# ── 6. Curly apostrophe normalized quote passes ──────────────────────────────

def test_curly_apostrophe_normalized_quote_passes():
    """LLM curly apostrophe (U+2019) matches straight apostrophe in source."""
    source = "company's revenue grew significantly this quarter"
    quote = "company’s revenue grew significantly this quarter"
    assert validate_evidence_quote(quote, source) is True


# ── 7. Old metadata-hallucination cases remain blocked ───────────────────────

def test_metadata_only_events_still_blocked():
    """Events with unavailable source text still produce skip records."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_providers import MockCatalystProvider

    event = {
        "event_id": "evt_meta_only",
        "ticker": "FAKE",
        "event_date": "2026-01-15",
        "source_text": "",
        "source_text_char_count": 0,
        "source_text_origin": "unavailable",
        "source_text_error": "no_doc_url",
    }
    results = classify_events([event], _provider=MockCatalystProvider(), cache_path=None)
    assert len(results) == 1
    r = results[0]
    assert r.should_affect_score is False
    assert "[SKIP]" in r.reasoning_short


# ── 8. model_should_affect_score preserved when quote is invalid ──────────────

def test_model_should_affect_score_preserved_after_invalid_quote():
    """model_should_affect_score captures the model's pre-validation answer."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_providers import BaseCatalystProvider

    class AlwaysTrueMock(BaseCatalystProvider):
        name = "preserve_model_mock"

        def classify(self, event, prompt):
            return _make_enrichment(
                should_affect_score=True,
                evidence_quote="hallucinated metric not present in source",
            )

    # Source must be >= MIN_SOURCE_TEXT_CHARS (200) to bypass the skip gate.
    source_text = "Completely different text about logistics and supply chain. " * 5
    event = {
        "event_id": "evt_preserve",
        "ticker": "PRSV",
        "event_date": "2026-01-15",
        "source_text": source_text,
        "source_text_char_count": len(source_text),
        "source_text_origin": "sec_url",
        "source_text_error": None,
    }
    results = classify_events([event], _provider=AlwaysTrueMock(), cache_path=None)
    assert len(results) == 1
    r = results[0]
    # Final overridden to False; model's original True is preserved
    assert r.should_affect_score is False
    assert r.model_should_affect_score is True


# ── 9. EIX-style investor conference boilerplate ──────────────────────────────

def test_eix_investor_conference_boilerplate_is_weak_evidence():
    """Management 'will use the information' at investor conference is not guidance."""
    ok, reason = check_catalyst_sufficiency(
        "guidance",
        "Members of Edison International management will use the information "
        "provided in this presentation at the upcoming investor conference.",
    )
    assert ok is False
    assert reason


# ── 10. KEYS/UI-style press-release wrapper with "its" ───────────────────────

def test_keys_pr_wrapper_with_its_is_insufficient():
    """'Issued its press release announcing financial results' is a PR wrapper, not evidence."""
    ok, reason = check_catalyst_sufficiency(
        "earnings",
        "Keysight Technologies issued its press release announcing financial results "
        "for the first quarter fiscal year 2026.",
    )
    assert ok is False
    assert reason


# ── 11. FLEX M&A remains actionable ──────────────────────────────────────────

def test_flex_definitive_agreement_remains_actionable():
    """Real M&A definitive-agreement quote is not rejected as boilerplate."""
    ok, reason = check_catalyst_sufficiency(
        "merger_acquisition",
        "Flex and Nextracker entered into a definitive agreement for the separation "
        "transaction valued at approximately $3 billion.",
    )
    assert ok is True
    assert reason == ""


# ── 12. EPD acquisition completion remains actionable ────────────────────────

def test_epd_acquisition_completion_remains_actionable():
    """Acquisition completion language is a real M&A catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "merger_acquisition",
        "Enterprise Products announced the completion of its acquisition "
        "of Navitas Midstream Partners.",
    )
    assert ok is True
    assert reason == ""


# ── 13. TAK revised guidance remains actionable ──────────────────────────────

def test_tak_revised_guidance_remains_actionable():
    """Raised full-year guidance with numeric target is real guidance evidence."""
    ok, reason = check_catalyst_sufficiency(
        "guidance",
        "Takeda raised its full-year guidance for core operating profit to "
        "¥420 billion, reflecting strong performance.",
    )
    assert ok is True
    assert reason == ""


# ── 14. classify_events wires sufficiency check ──────────────────────────────

def test_classify_events_applies_sufficiency_to_boilerplate_quote():
    """GLW-style quote passes verbatim validation but fails sufficiency → weak_evidence."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_providers import BaseCatalystProvider

    class GlwStyleMock(BaseCatalystProvider):
        name = "glw_style_mock"

        def classify(self, event, prompt):
            source_text = event.get("source_text", "")
            # Return a quote that IS verbatim in source but is boilerplate guidance
            return _make_enrichment(
                catalyst_type="guidance",
                should_affect_score=True,
                evidence_quote="Mr. Schlesinger will be providing business updates.",
            )

    # Source text contains the boilerplate verbatim (so quote validation passes)
    base_text = "Mr. Schlesinger will be providing business updates. " * 10
    event = {
        "event_id": "evt_glw",
        "ticker": "GLW",
        "event_date": "2026-01-15",
        "source_text": base_text,
        "source_text_char_count": len(base_text),
        "source_text_origin": "sec_url",
        "source_text_error": None,
    }
    results = classify_events([event], _provider=GlwStyleMock(), cache_path=None)
    assert len(results) == 1
    r = results[0]
    assert r.should_affect_score is False
    assert r.validation_status == "weak_evidence"
    assert r.invalid_reason  # non-empty reason


# ── 15. CSV contains new validation columns ───────────────────────────────────

def test_review_csv_contains_validation_columns(tmp_path):
    """CSV must include model_should_affect_score, final_should_affect_score, etc."""
    import csv as _csv
    from dataclasses import replace as dc_replace
    from missed.catalyst_report import generate_pilot_report

    sample = [{"event_id": "e1", "ticker": "TEST", "event_date": "2026-01-15",
               "event_type": "gain_20d_20pct", "primary_root_cause": "text_evidence",
               "filing_form_type": "8-K", "source_text_char_count": 300}]
    base = _make_enrichment(should_affect_score=False)
    enriched = [dc_replace(base, event_id="e1", model_should_affect_score=True,
                            validation_status="weak_evidence", quote_validation_pass=True,
                            invalid_reason="business_updates_boilerplate")]
    _, csv_path = generate_pilot_report(sample, enriched, str(tmp_path))
    with open(csv_path) as f:
        reader = _csv.DictReader(f)
        cols = reader.fieldnames or []
    for col in ("model_should_affect_score", "final_should_affect_score",
                "validation_status", "quote_validation_pass", "invalid_reason"):
        assert col in cols, f"CSV missing column: {col}"


# ── 16. Report counts use final_should_affect_score ──────────────────────────

def test_report_counts_exclude_weak_evidence(tmp_path):
    """Weak-evidence records are not counted in final_should_affect_score=True."""
    from dataclasses import replace as dc_replace
    from missed.catalyst_report import generate_pilot_report

    sample = [{"event_id": f"e{i}", "ticker": f"T{i}", "event_date": "2026-01-15",
               "event_type": "gain_20d_20pct", "primary_root_cause": "text_evidence",
               "filing_form_type": "8-K", "source_text_char_count": 300}
              for i in range(3)]
    base = _make_enrichment(should_affect_score=False)
    # e0: valid, should_affect_score=True
    e0 = dc_replace(base, event_id="e0", ticker="T0", should_affect_score=True,
                    validation_status="valid")
    # e1: weak_evidence, should_affect_score=False (model said True)
    e1 = dc_replace(base, event_id="e1", ticker="T1", should_affect_score=False,
                    validation_status="weak_evidence", model_should_affect_score=True,
                    invalid_reason="business_updates_boilerplate")
    # e2: valid, should_affect_score=False
    e2 = dc_replace(base, event_id="e2", ticker="T2", should_affect_score=False,
                    validation_status="valid")

    md_path, _ = generate_pilot_report(sample, [e0, e1, e2], str(tmp_path))
    content = open(md_path).read()
    # Only e0 has final should_affect_score=True; e1 is weak_evidence (not counted)
    assert "Weak evidence" in content
    assert "final_should_affect_score=True" in content


# ════════════════════════════════════════════════════════════════════════════
# Real-world pilot cases — management_change tightening
# ════════════════════════════════════════════════════════════════════════════

# ── 17. CVX CEO salary increase ──────────────────────────────────────────────

def test_cvx_salary_increase_is_compensation_not_catalyst():
    """CEO salary increase is compensation governance, not a stock catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "the independent Directors of the Board approved an increase of $75,000 to "
        "Mr. Wirth's annual base salary, resulting in an annual salary of $1,975,000",
    )
    assert ok is False
    assert reason == "compensation_not_catalyst"


# ── 18. HUM routine board election ───────────────────────────────────────────

def test_hum_routine_board_election_is_governance():
    """Routine election of ten nominees to board is not a management catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "Each of the ten (10) nominees for director were elected to the Company's "
        "Board of Directors.",
    )
    assert ok is False
    assert reason == "routine_board_governance"


# ── 19. AXIA fiscal council nominee ──────────────────────────────────────────

def test_axia_fiscal_council_nominee_is_governance():
    """Fiscal council nominee review is routine compliance governance."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "the candidates nominated for membership on the Fiscal Council, listed below, "
        "have been reviewed and approved by the Company as to compliance with the "
        "eligibility requirements",
    )
    assert ok is False
    assert reason == "routine_board_governance"


# ── 20. VG debt issuance mislabeled as management_change ─────────────────────

def test_vg_debt_issuance_mislabeled_management_change():
    """Notes issuance labeled management_change is debt, not leadership action."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "Venture Global Calcasieu Pass, LLC issued $750,000,000 aggregate principal "
        "amount of 6.00% Senior Secured Notes due 2031",
    )
    assert ok is False
    assert reason == "debt_issuance_misclassified"


# ── 21. MDLN board appointment ────────────────────────────────────────────────

def test_mdln_board_appointment_is_governance():
    """Ordinary board director appointment is not a strategic leadership change."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "Effective December 16, 2025, following the effective time of the "
        "Registration Statement, Todd M. Bluedorn was appointed to the Board of "
        "Directors of the Company.",
    )
    assert ok is False
    assert reason == "routine_board_governance"


# ── 22. BHP Jansen strategy update ────────────────────────────────────────────

def test_bhp_jansen_strategy_is_weak_product_update():
    """'Long-term growth strategy' language is not a product launch."""
    ok, reason = check_catalyst_sufficiency(
        "product_launch",
        "Jansen is an important pillar in BHP's long-term growth strategy and is a "
        "long-life, low cost expandable asset that is expected to generate benefits "
        "for shareholders for decades.",
    )
    assert ok is False
    assert reason == "weak_product_or_project_update"


# ── 23. RVMD Phase 3 positive result ──────────────────────────────────────────

def test_rvmd_phase3_result_is_valid():
    """Topline Phase 3 results are a real product/clinical catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "product_launch",
        "Revolution Medicines, Inc. shared topline results from the Phase 3 "
        "TRANSCEND-1 study evaluating RMC-6236 in patients with KRAS-mutant "
        "pancreatic ductal adenocarcinoma.",
    )
    assert ok is True
    assert reason == ""


# ── 24. BNTX Phase 2 positive result ──────────────────────────────────────────

def test_bntx_phase2_result_is_valid():
    """Positive Phase 2 primary analysis results are a real clinical catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "product_launch",
        "BioNTech SE announced positive results from the primary analysis of the "
        "Phase 2 BIONTEGRA trial evaluating BNT321 in combination with docetaxel.",
    )
    assert ok is True
    assert reason == ""


# ── 25. ABBV EPS guidance range ───────────────────────────────────────────────

def test_abbv_eps_guidance_range_is_valid():
    """EPS guidance range with numeric values is real actionable guidance."""
    ok, reason = check_catalyst_sufficiency(
        "guidance",
        "AbbVie's full-year 2025 adjusted diluted earnings per share guidance range, "
        "including $12.12 to $12.32",
    )
    assert ok is True
    assert reason == ""


# ════════════════════════════════════════════════════════════════════════════
# revalidate_enrichments — offline re-validation without new LLM calls
# ════════════════════════════════════════════════════════════════════════════

# ── 26. revalidate marks old weak records correctly ───────────────────────────

def test_revalidate_enrichments_marks_compensation_as_weak():
    """revalidate_enrichments re-flags compensation management_change as weak."""
    from missed.catalyst_classifier import revalidate_enrichments

    old_record = {
        "event_id": "e_cvx", "ticker": "CVX", "event_date": "2026-01-15",
        "catalyst_type": "management_change", "materiality": "low",
        "sentiment": "bullish", "confidence": 0.7, "provider": "openai",
        "enriched_at": "2026-01-15T12:00:00+00:00",
        "evidence_quote": "the Board approved an increase to annual base salary of Mr. Wirth",
        "reasoning_short": "Salary increase signals confidence.",
        "should_affect_score": True,       # old value before validation
        "model_should_affect_score": True,
        "validation_status": "valid",
        "quote_validation_pass": True,
        "invalid_reason": "",
    }
    result = revalidate_enrichments([old_record])
    assert len(result) == 1
    r = result[0]
    assert r["should_affect_score"] is False
    assert r["validation_status"] == "weak_evidence"
    assert r["invalid_reason"] == "compensation_not_catalyst"


# ── 27. revalidate preserves skip and error records ───────────────────────────

def test_revalidate_enrichments_preserves_skip_records():
    """SKIP and ERROR records are passed through unchanged."""
    from missed.catalyst_classifier import revalidate_enrichments

    skip = {
        "event_id": "e_skip", "ticker": "XX", "event_date": "2026-01-15",
        "catalyst_type": "unknown", "materiality": "none",
        "sentiment": "neutral", "confidence": 0.0, "provider": "skip_no_source_text",
        "enriched_at": "2026-01-15T12:00:00+00:00",
        "evidence_quote": "", "reasoning_short": "[SKIP] pdf_not_supported",
        "should_affect_score": False, "model_should_affect_score": False,
        "validation_status": "valid", "quote_validation_pass": True, "invalid_reason": "",
    }
    result = revalidate_enrichments([skip])
    assert result[0]["should_affect_score"] is False
    assert "[SKIP]" in result[0]["reasoning_short"]


# ── 28. revalidate keeps valid M&A records actionable ────────────────────────

def test_revalidate_enrichments_keeps_valid_ma_actionable():
    """M&A acquisition agreement record stays actionable after revalidation."""
    from missed.catalyst_classifier import revalidate_enrichments

    rec = {
        "event_id": "e_flex", "ticker": "FLEX", "event_date": "2026-01-15",
        "catalyst_type": "merger_acquisition", "materiality": "high",
        "sentiment": "bullish", "confidence": 0.9, "provider": "openai",
        "enriched_at": "2026-01-15T12:00:00+00:00",
        "evidence_quote": "the Company has entered into an agreement to acquire Electrical Power Products, Inc.",
        "reasoning_short": "Acquisition agreement.",
        "should_affect_score": True, "model_should_affect_score": True,
        "validation_status": "valid", "quote_validation_pass": True, "invalid_reason": "",
    }
    result = revalidate_enrichments([rec])
    assert result[0]["should_affect_score"] is True
    assert result[0]["validation_status"] == "valid"


# ════════════════════════════════════════════════════════════════════════════
# Report: High Materiality table filter + Weak/Overridden table
# ════════════════════════════════════════════════════════════════════════════

# ── 29. High Materiality Bullish excludes weak_evidence rows ─────────────────

def test_high_materiality_bullish_excludes_weak_evidence(tmp_path):
    """High Materiality Bullish table must exclude weak_evidence records."""
    from dataclasses import replace as dc_replace
    from missed.catalyst_report import generate_pilot_report

    sample = [{"event_id": f"e{i}", "ticker": f"T{i}", "event_date": "2026-01-15",
               "event_type": "gain_20d_20pct", "primary_root_cause": "text_evidence",
               "filing_form_type": "8-K", "source_text_char_count": 300}
              for i in range(2)]
    base = _make_enrichment(should_affect_score=True,
                            catalyst_type="guidance",
                            evidence_quote="raised full-year revenue guidance to $5B")
    # e0: valid high-materiality bullish
    e0 = dc_replace(base, event_id="e0", ticker="T0", validation_status="valid")
    # e1: weak_evidence — should NOT appear in High Materiality table
    e1 = dc_replace(base, event_id="e1", ticker="T1",
                    should_affect_score=False, validation_status="weak_evidence",
                    model_should_affect_score=True,
                    invalid_reason="business_updates_boilerplate")

    md_path, _ = generate_pilot_report(sample, [e0, e1], str(tmp_path))
    content = open(md_path).read()

    # Count table rows mentioning each ticker in the High Materiality Bullish section
    hm_bullish_start = content.find("## High Materiality — Bullish")
    hm_bearish_start = content.find("## High Materiality — Bearish")
    assert hm_bullish_start >= 0
    hm_bullish_section = content[hm_bullish_start:hm_bearish_start]

    # T0 (valid) must appear; T1 (weak_evidence) must NOT appear in Bullish table
    assert "| T0 |" in hm_bullish_section
    assert "| T1 |" not in hm_bullish_section


# ── 30. Report includes Weak/Overridden table ─────────────────────────────────

def test_report_includes_weak_overridden_table(tmp_path):
    """Report must include a Weak / Overridden Candidates section."""
    from dataclasses import replace as dc_replace
    from missed.catalyst_report import generate_pilot_report

    sample = [{"event_id": "e1", "ticker": "CVX", "event_date": "2026-01-15",
               "event_type": "gain_20d_20pct", "primary_root_cause": "text_evidence",
               "filing_form_type": "8-K", "source_text_char_count": 300}]
    base = _make_enrichment(should_affect_score=False, catalyst_type="management_change",
                            evidence_quote="Board approved salary increase")
    e1 = dc_replace(base, event_id="e1", ticker="CVX",
                    should_affect_score=False, validation_status="weak_evidence",
                    model_should_affect_score=True,
                    invalid_reason="compensation_not_catalyst")

    md_path, _ = generate_pilot_report(sample, [e1], str(tmp_path))
    content = open(md_path).read()
    assert "Overridden" in content or "Weak" in content
    assert "CVX" in content
    assert "compensation_not_catalyst" in content


# ── 31. CLI has --report-only option ─────────────────────────────────────────

def test_cli_has_report_only_option():
    """missed pilot CLI must expose --report-only and --input-enriched."""
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "pilot", "--help"])
    assert "--report-only" in result.output
    assert "--input-enriched" in result.output


# ════════════════════════════════════════════════════════════════════════════
# Task 5 — Tighten final score-impact rules
# ════════════════════════════════════════════════════════════════════════════

# ── 32. Sentiment filter: neutral is not actionable ───────────────────────────

def test_neutral_sentiment_is_not_actionable():
    """check_sentiment_actionable returns False for neutral."""
    from missed.catalyst_source_resolver import check_sentiment_actionable
    ok, reason = check_sentiment_actionable("neutral")
    assert ok is False
    assert reason == "neutral_or_mixed_sentiment"


# ── 33. Sentiment filter: mixed is not actionable ─────────────────────────────

def test_mixed_sentiment_is_not_actionable():
    """check_sentiment_actionable returns False for mixed."""
    from missed.catalyst_source_resolver import check_sentiment_actionable
    ok, reason = check_sentiment_actionable("mixed")
    assert ok is False
    assert reason == "neutral_or_mixed_sentiment"


# ── 34. Sentiment filter: bullish is actionable ──────────────────────────────

def test_bullish_sentiment_is_actionable():
    """check_sentiment_actionable returns True for bullish."""
    from missed.catalyst_source_resolver import check_sentiment_actionable
    ok, reason = check_sentiment_actionable("bullish")
    assert ok is True
    assert reason == ""


# ── 35. Sentiment filter: bearish is actionable ──────────────────────────────

def test_bearish_sentiment_is_actionable():
    """check_sentiment_actionable returns True for bearish."""
    from missed.catalyst_source_resolver import check_sentiment_actionable
    ok, reason = check_sentiment_actionable("bearish")
    assert ok is True
    assert reason == ""


# ── 36. Neutral sentiment overrides should_affect_score via revalidate ────────

def test_neutral_sentiment_overrides_should_affect_score():
    """revalidate_enrichments forces should_affect_score=False for neutral sentiment."""
    from missed.catalyst_classifier import revalidate_enrichments

    rec = {
        "event_id": "e_neutral", "ticker": "XYZ", "event_date": "2026-01-15",
        "catalyst_type": "merger_acquisition", "materiality": "high",
        "sentiment": "neutral", "confidence": 0.8, "provider": "nvidia",
        "enriched_at": "2026-01-15T12:00:00+00:00",
        "evidence_quote": "entered into a definitive agreement to acquire ABC Corp for $2 billion",
        "reasoning_short": "Definitive merger agreement.",
        "should_affect_score": True, "model_should_affect_score": True,
        "validation_status": "valid", "quote_validation_pass": True, "invalid_reason": "",
    }
    result = revalidate_enrichments([rec])
    r = result[0]
    assert r["should_affect_score"] is False
    assert r["validation_status"] == "neutral_sentiment"
    assert r["invalid_reason"] == "neutral_or_mixed_sentiment"


# ── 37. DG chairman blocked by sufficiency (no turnaround context) ────────────

def test_dg_chairman_blocked_by_sufficiency():
    """Chairman appointment without turnaround context → routine_chair_appointment."""
    from missed.catalyst_classifier import revalidate_enrichments

    rec = {
        "event_id": "e_dg", "ticker": "DG", "event_date": "2026-01-15",
        "catalyst_type": "management_change", "materiality": "medium",
        "sentiment": "neutral", "confidence": 0.7, "provider": "nvidia",
        "enriched_at": "2026-01-15T12:00:00+00:00",
        "evidence_quote": "Todd J. Vasos has agreed to serve as the Company's Chairman of the Board",
        "reasoning_short": "Chairman appointment.",
        "should_affect_score": True, "model_should_affect_score": True,
        "validation_status": "valid", "quote_validation_pass": True, "invalid_reason": "",
    }
    result = revalidate_enrichments([rec])
    r = result[0]
    assert r["should_affect_score"] is False
    assert r["validation_status"] == "weak_evidence"
    assert r["invalid_reason"] == "routine_chair_appointment"


# ── 38. Chairman without turnaround context ───────────────────────────────────

def test_chairman_without_turnaround_is_routine_chair_appointment():
    """Chairman appointment quote with no turnaround context returns routine_chair_appointment."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "Susan M. Arnold was appointed as Executive Chairman of the Board.",
    )
    assert ok is False
    assert reason == "routine_chair_appointment"


# ── 39. Chairman with activist context is valid ──────────────────────────────

def test_chairman_with_activist_context_is_valid():
    """Chairman appointment quote with activist context is actionable."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "Following the settlement with activist investor Starboard, Jane Doe was "
        "appointed Executive Chairman to lead the strategic transformation.",
    )
    assert ok is True
    assert reason == ""


# ── 40. Chairman with founder context is valid ───────────────────────────────

def test_chairman_with_founder_is_valid():
    """Founder returning as Executive Chairman is a meaningful leadership signal."""
    ok, reason = check_catalyst_sufficiency(
        "management_change",
        "Company founder John Smith will return as Executive Chairman to guide "
        "the company's strategic direction.",
    )
    assert ok is True
    assert reason == ""


# ── 41. Regulatory: material settlement is valid ─────────────────────────────

def test_regulatory_settlement_agreement_is_valid():
    """Settlement agreement is a material regulatory catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "regulatory",
        "Venture Global reached a settlement agreement with Shell regarding LNG "
        "offtake obligations worth $450 million.",
    )
    assert ok is True
    assert reason == ""


# ── 42. Regulatory: asset freeze request is valid ────────────────────────────

def test_regulatory_asset_freeze_is_valid():
    """Asset freeze request is a material regulatory catalyst (VALE pattern)."""
    ok, reason = check_catalyst_sufficiency(
        "regulatory",
        "Vale informs that the Brazilian Federal Attorney's Office filed a request "
        "for asset freeze of $5 billion related to the Mariana dam collapse.",
    )
    assert ok is True
    assert reason == ""


# ── 43. Regulatory: generic compliance disclosure is weak ────────────────────

def test_regulatory_generic_compliance_is_weak():
    """Generic regulatory disclosure without material terms is not a catalyst."""
    ok, reason = check_catalyst_sufficiency(
        "regulatory",
        "The company provided a routine compliance update in accordance with "
        "applicable disclosure requirements.",
    )
    assert ok is False
    assert reason  # non-empty reason


# ── 44. Regression: CTRA merger still actionable ─────────────────────────────

def test_ctra_merger_still_actionable():
    """M&A definitive merger agreement remains actionable after management_change changes."""
    ok, reason = check_catalyst_sufficiency(
        "merger_acquisition",
        "Catalent announced entry into a definitive merger agreement with Novo Holdings "
        "at $63.50 per share, representing a 16% premium.",
    )
    assert ok is True
    assert reason == ""
