"""TDD tests for shadow scoring experiment — grounded LLM catalyst adjustments."""
from __future__ import annotations

import csv
import json
import os
import tempfile

import pytest

from missed.catalyst_shadow_scorer import (
    compute_shadow_scores,
    generate_shadow_report,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _enrichment(
    ticker="AAPL",
    materiality="high",
    sentiment="bullish",
    confidence=0.85,
    validation_status="valid",
    should_affect_score=True,
    quote_validation_pass=True,
    catalyst_type="merger_acquisition",
    event_id="evt-001",
    event_date="2026-01-15",
    reasoning_short="strong signal",
) -> dict:
    return {
        "event_id": event_id,
        "ticker": ticker,
        "event_date": event_date,
        "catalyst_type": catalyst_type,
        "materiality": materiality,
        "sentiment": sentiment,
        "confidence": confidence,
        "evidence_quote": "Company acquired XYZ Corp for $500M.",
        "reasoning_short": reasoning_short,
        "should_affect_score": should_affect_score,
        "model_should_affect_score": should_affect_score,
        "validation_status": validation_status,
        "quote_validation_pass": quote_validation_pass,
        "invalid_reason": "",
        "provider": "openai",
        "enriched_at": "2026-01-20T10:00:00+00:00",
    }


def _score(
    ticker="AAPL",
    total_score=50.0,
    catalyst_score=30.0,
    risk_penalty=20.0,
    tier="C",
    run_id="run-001",
) -> dict:
    return {
        "run_id": run_id,
        "ticker": ticker,
        "as_of_date": "2026-01-20",
        "cheap_score": 15.0,
        "quality_score": 20.0,
        "catalyst_score": catalyst_score,
        "momentum_score": 10.0,
        "sentiment_score": 5.0,
        "risk_penalty": risk_penalty,
        "total_score": total_score,
        "tier": tier,
        "confidence": "medium",
    }


# ── Test 1: invalid_quote records excluded ────────────────────────────────────

def test_invalid_quote_excluded_from_shadow():
    enrichments = [_enrichment(validation_status="invalid_quote", should_affect_score=False)]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    # No actionable enrichment → no shadow rows with adjustment
    adjustments = [r["llm_adjustment"] for r in rows if r["ticker"] == "AAPL"]
    assert all(a == 0.0 for a in adjustments), "invalid_quote should not produce non-zero adjustment"


# ── Test 2: weak_evidence excluded ───────────────────────────────────────────

def test_weak_evidence_excluded_from_shadow():
    enrichments = [_enrichment(validation_status="weak_evidence", should_affect_score=False)]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    adjustments = [r["llm_adjustment"] for r in rows if r["ticker"] == "AAPL"]
    assert all(a == 0.0 for a in adjustments)


# ── Test 3: should_affect_score=False excluded ───────────────────────────────

def test_should_affect_score_false_excluded():
    enrichments = [_enrichment(should_affect_score=False, validation_status="valid")]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    adjustments = [r["llm_adjustment"] for r in rows if r["ticker"] == "AAPL"]
    assert all(a == 0.0 for a in adjustments)


# ── Test 4: high bullish = +5 ─────────────────────────────────────────────────

def test_high_bullish_adjustment_is_plus_5():
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == 5.0
    assert row["shadow_total"] == 55.0


# ── Test 5: medium bullish = +3 ──────────────────────────────────────────────

def test_medium_bullish_adjustment_is_plus_3():
    enrichments = [_enrichment(materiality="medium", sentiment="bullish")]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == 3.0
    assert row["shadow_total"] == 53.0


# ── Test 6: high bearish = -5 ─────────────────────────────────────────────────

def test_high_bearish_adjustment_is_minus_5():
    enrichments = [_enrichment(materiality="high", sentiment="bearish")]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == -5.0
    assert row["shadow_total"] == 45.0


# ── Test 7: medium bearish = -3 ──────────────────────────────────────────────

def test_medium_bearish_adjustment_is_minus_3():
    enrichments = [_enrichment(materiality="medium", sentiment="bearish")]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == -3.0
    assert row["shadow_total"] == 47.0


# ── Test 8: low confidence (<0.5) → no adjustment ────────────────────────────

def test_low_confidence_produces_no_adjustment():
    enrichments = [_enrichment(materiality="high", sentiment="bullish", confidence=0.45)]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == 0.0
    assert row["shadow_total"] == 50.0


# ── Test 9: low materiality → no adjustment ──────────────────────────────────

def test_low_materiality_produces_no_adjustment():
    enrichments = [_enrichment(materiality="low", sentiment="bullish", confidence=0.9)]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == 0.0


# ── Test 10: none materiality → no adjustment ────────────────────────────────

def test_none_materiality_produces_no_adjustment():
    enrichments = [_enrichment(materiality="none", sentiment="bullish", confidence=0.9)]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == 0.0


# ── Test 11: cap per ticker (positive) ───────────────────────────────────────

def test_cap_per_ticker_positive_two_high_bullish():
    # Two high-bullish events for AAPL would be +10 uncapped → capped at +5
    enrichments = [
        _enrichment(event_id="evt-001", materiality="high", sentiment="bullish"),
        _enrichment(event_id="evt-002", materiality="high", sentiment="bullish"),
    ]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == 5.0  # capped at +5


# ── Test 12: cap per ticker (negative) ───────────────────────────────────────

def test_cap_per_ticker_negative_two_high_bearish():
    enrichments = [
        _enrichment(event_id="evt-001", materiality="high", sentiment="bearish"),
        _enrichment(event_id="evt-002", materiality="high", sentiment="bearish"),
    ]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == -5.0  # capped at -5


# ── Test 13: no production score mutation ────────────────────────────────────

def test_no_production_score_mutation():
    scores = [_score(total_score=50.0)]
    original_total = scores[0]["total_score"]
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    compute_shadow_scores(enrichments, scores)
    assert scores[0]["total_score"] == original_total


# ── Test 14: shadow rows have required fields ─────────────────────────────────

def test_shadow_rows_have_required_fields():
    enrichments = [_enrichment()]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    required = {
        "ticker", "event_date", "run_id", "original_total", "original_tier",
        "llm_adjustment", "shadow_total", "shadow_tier", "tier_move",
        "catalyst_type", "materiality", "sentiment", "confidence",
        "validation_status", "quote_validation_pass", "final_should_affect_score",
        "evidence_quote",
    }
    assert len(rows) == 1
    assert required.issubset(rows[0].keys())


# ── Test 15: tier crossing Reject → C ────────────────────────────────────────

def test_tier_crossing_reject_to_c():
    # total=43 + 5 = 48 → C (≥45)
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    scores = [_score(total_score=43.0, tier="Reject")]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["original_tier"] == "Reject"
    assert row["shadow_tier"] == "C"


# ── Test 16: tier crossing C → B ─────────────────────────────────────────────

def test_tier_crossing_c_to_b():
    # total=57 + 5 = 62 → B (≥60, coverage defaults to 1.0)
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    scores = [_score(total_score=57.0, tier="C")]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["original_tier"] == "C"
    assert row["shadow_tier"] == "B"


# ── Test 17: near-threshold no crossing ──────────────────────────────────────

def test_near_threshold_no_tier_crossing():
    # total=40 + 3 = 43 → still Reject (needs 45 for C)
    enrichments = [_enrichment(materiality="medium", sentiment="bullish")]
    scores = [_score(total_score=40.0, tier="Reject")]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["shadow_tier"] == "Reject"


# ── Test 18: ticker not in scores → no row ───────────────────────────────────

def test_ticker_not_in_scores_produces_no_row():
    enrichments = [_enrichment(ticker="ZZZZ")]
    scores = [_score(ticker="AAPL")]
    rows = compute_shadow_scores(enrichments, scores)
    tickers = [r["ticker"] for r in rows]
    assert "ZZZZ" not in tickers


# ── Test 19: report files generated ──────────────────────────────────────────

def test_report_files_generated():
    enrichments = [_enrichment()]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path, csv_path = generate_shadow_report(rows, tmpdir)
        assert os.path.exists(md_path)
        assert os.path.exists(csv_path)
        assert md_path.endswith(".md")
        assert csv_path.endswith(".csv")


# ── Test 20: report markdown sections ────────────────────────────────────────

def test_report_markdown_contains_required_sections():
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    scores = [_score(total_score=43.0, tier="Reject")]
    rows = compute_shadow_scores(enrichments, scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path, _ = generate_shadow_report(rows, tmpdir)
        content = open(md_path).read()
    assert "## Summary" in content
    assert "## Tier Movements" in content
    assert "## Adjusted Tickers" in content
    assert "## Near Misses" in content
    assert "## Bearish Downgrades" in content


# ── Test 21: CSV has required columns ────────────────────────────────────────

def test_report_csv_has_required_columns():
    enrichments = [_enrichment()]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        _, csv_path = generate_shadow_report(rows, tmpdir)
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            cols = set(reader.fieldnames)
    required_cols = {
        "ticker", "event_date", "run_id", "catalyst_type", "materiality",
        "sentiment", "confidence", "validation_status", "quote_validation_pass",
        "final_should_affect_score", "evidence_quote",
        "original_total", "original_tier", "llm_adjustment",
        "shadow_total", "shadow_tier", "tier_move",
    }
    assert required_cols.issubset(cols)


# ── Test 22: multiple tickers each get own row ────────────────────────────────

def test_multiple_tickers_each_get_own_row():
    enrichments = [
        _enrichment(ticker="AAPL", event_id="e1"),
        _enrichment(ticker="NVDA", event_id="e2"),
    ]
    scores = [
        _score(ticker="AAPL", total_score=50.0),
        _score(ticker="NVDA", total_score=55.0),
    ]
    rows = compute_shadow_scores(enrichments, scores)
    tickers = {r["ticker"] for r in rows}
    assert "AAPL" in tickers
    assert "NVDA" in tickers


# ── Test 23: skip record excluded ─────────────────────────────────────────────

def test_skip_record_excluded():
    enrichments = [
        _enrichment(
            should_affect_score=False,
            validation_status="valid",
            reasoning_short="[SKIP] no source text",
        )
    ]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    adjustments = [r["llm_adjustment"] for r in rows if r["ticker"] == "AAPL"]
    assert all(a == 0.0 for a in adjustments)


# ── Test 24: neutral sentiment no adjustment ─────────────────────────────────

def test_neutral_sentiment_no_adjustment():
    enrichments = [_enrichment(materiality="high", sentiment="neutral", confidence=0.9)]
    scores = [_score(total_score=50.0)]
    rows = compute_shadow_scores(enrichments, scores)
    row = next(r for r in rows if r["ticker"] == "AAPL")
    assert row["llm_adjustment"] == 0.0


# ── Test 25: event_date carried through to row ───────────────────────────────

def test_shadow_row_includes_event_date():
    enrichments = [_enrichment(event_date="2026-03-10")]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    assert rows[0]["event_date"] == "2026-03-10"


# ── Test 26: evidence_quote carried through to row ───────────────────────────

def test_shadow_row_includes_evidence_quote():
    e = _enrichment()
    e["evidence_quote"] = "Acquired XYZ for $500M in an all-cash deal."
    scores = [_score()]
    rows = compute_shadow_scores([e], scores)
    assert rows[0]["evidence_quote"] == "Acquired XYZ for $500M in an all-cash deal."


# ── Test 27: validation_status carried through to row ────────────────────────

def test_shadow_row_includes_validation_status():
    enrichments = [_enrichment()]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    assert rows[0]["validation_status"] == "valid"


# ── Test 28: final_should_affect_score carried through ───────────────────────

def test_shadow_row_includes_final_should_affect_score():
    enrichments = [_enrichment(should_affect_score=True)]
    scores = [_score()]
    rows = compute_shadow_scores(enrichments, scores)
    assert rows[0]["final_should_affect_score"] is True


# ── Test 29: tier_move set on crossing ───────────────────────────────────────

def test_tier_move_set_on_crossing():
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    scores = [_score(total_score=43.0, tier="Reject")]
    rows = compute_shadow_scores(enrichments, scores)
    # 43+5=48 → C
    assert rows[0]["tier_move"] == "Reject→C"


# ── Test 30: tier_move empty when no crossing ────────────────────────────────

def test_tier_move_empty_when_no_crossing():
    # 50+5=55, catalyst=30 → still C (not ≥60 for B)
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    scores = [_score(total_score=50.0, tier="C")]
    rows = compute_shadow_scores(enrichments, scores)
    assert rows[0]["tier_move"] == ""


# ── Test 31: Adjusted Tickers section shows tickers with non-zero adjustment ─

def test_adjusted_tickers_section_shows_actionable_tickers():
    enrichments = [_enrichment(materiality="high", sentiment="bullish")]
    scores = [_score(total_score=50.0, tier="C")]
    rows = compute_shadow_scores(enrichments, scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path, _ = generate_shadow_report(rows, tmpdir)
        content = open(md_path).read()
    assert "## Adjusted Tickers" in content
    adj_start = content.index("## Adjusted Tickers")
    # AAPL should appear after the Adjusted Tickers header
    section = content[adj_start:]
    assert "AAPL" in section


# ── Test 32: Near Misses section in report ───────────────────────────────────

def test_report_has_near_misses_section():
    enrichments = [_enrichment(materiality="medium", sentiment="bullish")]
    scores = [_score(total_score=38.0, tier="Reject")]
    rows = compute_shadow_scores(enrichments, scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path, _ = generate_shadow_report(rows, tmpdir)
        content = open(md_path).read()
    assert "## Near Misses" in content


# ── Test 33: Near Misses shows Reject tickers with shadow_total 40–44.9 ──────

def test_near_misses_shows_reject_ticker_near_c_threshold():
    # 38 + 3 (medium bullish) = 41 → Reject, shadow_total in [40, 44.9] → near miss
    enrichments = [_enrichment(materiality="medium", sentiment="bullish")]
    scores = [_score(total_score=38.0, tier="Reject")]
    rows = compute_shadow_scores(enrichments, scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path, _ = generate_shadow_report(rows, tmpdir)
        content = open(md_path).read()
    near_miss_start = content.index("## Near Misses")
    section = content[near_miss_start:]
    assert "AAPL" in section


# ── Test 34: pipe in evidence is escaped in report tables ────────────────────

def test_evidence_pipe_escaped_in_report():
    e = _enrichment()
    e["evidence_quote"] = "Revenue | profit margin expanded"
    scores = [_score()]
    rows = compute_shadow_scores([e], scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path, _ = generate_shadow_report(rows, tmpdir)
        content = open(md_path).read()
    # Raw unescaped pipe inside cell content must not appear
    assert "Revenue | profit margin expanded" not in content
    # Escaped form must appear instead
    assert "Revenue \\| profit margin expanded" in content


# ── Test 35: newline in evidence is stripped in report tables ─────────────────

def test_evidence_newline_stripped_in_report():
    e = _enrichment()
    e["evidence_quote"] = "First sentence.\nSecond sentence."
    scores = [_score()]
    rows = compute_shadow_scores([e], scores)
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path, _ = generate_shadow_report(rows, tmpdir)
        content = open(md_path).read()
    # Both halves should appear on the same line (no raw \n in a table cell)
    lines_with_first = [l for l in content.split("\n") if "First sentence." in l]
    assert len(lines_with_first) >= 1
    assert all("Second sentence." in l for l in lines_with_first)
