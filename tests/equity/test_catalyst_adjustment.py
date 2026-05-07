"""TDD tests for the scaled shadow catalyst adjustment model.

Rules from design spec (revised v0):
- No double-counting: priced_in suppresses time_decay, not both
- Smooth decay: max(0, 1 - days/90) only when no observable spread
- Risk adjustment: only from existing risk_penalty field, no computed ratios
- v0 simple: evidence_confidence, impact_estimate, static_adjustment,
  scaled_adjustment, scaled_shadow_score, adjustment_reason
- Final cap: +5 / -5
- Never mutates production scores
"""
from __future__ import annotations

import pytest


# ── Entry fixture ────────────────────────────────────────────────────────────

def _entry(
    catalyst_type="merger_acquisition",
    materiality="high",
    sentiment="bullish",
    confidence=0.9,
    evidence_quote="CTRA entered into a definitive merger agreement.",
    validation_status="valid",
    quote_validation_pass=True,
    original_score=43.5,
    shadow_score=48.5,
    risk_penalty=15.0,
    days_since_event=3,
    deal_spread_pct=None,
    **extra,
) -> dict:
    return {
        "ticker": extra.pop("ticker", "TEST"),
        "catalyst_type": catalyst_type,
        "materiality": materiality,
        "sentiment": sentiment,
        "confidence": confidence,
        "evidence_quote": evidence_quote,
        "validation_status": validation_status,
        "quote_validation_pass": quote_validation_pass,
        "original_score": original_score,
        "shadow_score": shadow_score,
        "risk_penalty": risk_penalty,
        "days_since_event": days_since_event,
        "deal_spread_pct": deal_spread_pct,
        **extra,
    }


# ── 1. Weighted model does not collapse from one weak factor ──────────────────

def test_weighted_model_does_not_collapse_to_zero():
    """A single weak factor (e.g. older event) doesn't zero out a strong catalyst."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(days_since_event=50, deal_spread_pct=None)  # no observable spread → time decay
    result = compute_scaled_catalyst_adjustment(e)
    # Time decay at day 50: max(0, 1 - 50/90) ≈ 0.44 — should still be positive
    assert result["scaled_adjustment"] > 0.0, "Decay at day 50 should not zero out adjustment"


# ── 2. Observable spread suppresses time decay ────────────────────────────────

def test_observable_spread_suppresses_time_decay():
    """When deal_spread_pct is set, time_decay is not applied."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    # Day 60, but spread is observable → no time decay
    e_spread = _entry(days_since_event=60, deal_spread_pct=8.0)
    e_nodeal = _entry(days_since_event=60, deal_spread_pct=None)
    r_spread = compute_scaled_catalyst_adjustment(e_spread)
    r_nodeal = compute_scaled_catalyst_adjustment(e_nodeal)
    # Entry with spread should not have time_decay applied
    assert r_spread.get("time_decay_applied") is False or r_spread.get("time_decay_applied") == False
    # Entry without spread should have time_decay applied
    assert r_nodeal.get("time_decay_applied") is True or r_nodeal.get("time_decay_applied") == True


# ── 3. No spread → smooth time decay (no cliff between day 14 and 15) ─────────

def test_smooth_time_decay_no_cliff():
    """time_decay_factor at day 14 vs day 15 has no cliff — smooth formula."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e14 = _entry(days_since_event=14, deal_spread_pct=None)
    e15 = _entry(days_since_event=15, deal_spread_pct=None)
    r14 = compute_scaled_catalyst_adjustment(e14)
    r15 = compute_scaled_catalyst_adjustment(e15)
    # Decay at 14: max(0, 1-14/90)=0.844; at 15: max(0, 1-15/90)=0.833
    # Difference should be tiny (< 0.1 in adjustment terms)
    diff = abs(r14["scaled_adjustment"] - r15["scaled_adjustment"])
    assert diff < 0.5, f"Day 14→15 cliff too large: diff={diff:.3f}"


# ── 4. Missing risk fields do not invent a haircut ────────────────────────────

def test_missing_risk_fields_no_haircut():
    """When risk_penalty is None, risk_adjustment is 'not_available' and no haircut."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(risk_penalty=None)
    result = compute_scaled_catalyst_adjustment(e)
    assert result["risk_adjustment"] == "not_available"


# ── 5. risk_penalty haircuts VG-style settlement ─────────────────────────────

def test_risk_penalty_can_haircut_adjustment():
    """High risk_penalty lowers the scaled adjustment compared to low risk."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e_low_risk = _entry(catalyst_type="litigation_resolution",
                        evidence_quote="VG completed cargo delivery settlement.",
                        risk_penalty=10.0)
    e_high_risk = _entry(catalyst_type="litigation_resolution",
                         evidence_quote="VG completed cargo delivery settlement.",
                         risk_penalty=60.0)
    r_low = compute_scaled_catalyst_adjustment(e_low_risk)
    r_high = compute_scaled_catalyst_adjustment(e_high_risk)
    assert r_low["scaled_adjustment"] >= r_high["scaled_adjustment"], (
        "High risk should not boost adjustment"
    )


# ── 6. CTRA tight spread < 2% → lower scaled adjustment than static +5 ────────

def test_ctra_tight_spread_lower_than_static():
    """M&A with tight spread (<2%) produces scaled_adjustment < static_adjustment."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(
        catalyst_type="merger_acquisition",
        evidence_quote="All-stock merger, exchange ratio 0.4 shares.",
        deal_spread_pct=0.8,  # spread nearly gone
    )
    result = compute_scaled_catalyst_adjustment(e)
    assert result["scaled_adjustment"] < result["static_adjustment"], (
        f"Tight spread: scaled={result['scaled_adjustment']:.2f} should be < static={result['static_adjustment']:.2f}"
    )


# ── 7. VG settlement lower than full-company M&A ─────────────────────────────

def test_vg_settlement_lower_than_full_company_mna():
    """Regulatory settlement gets lower scaled adjustment than full-company M&A."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    vg = _entry(
        ticker="VG",
        catalyst_type="litigation_resolution",
        materiality="medium",
        evidence_quote="VG completed cargo delivery following regulatory settlement.",
        deal_spread_pct=None,
        days_since_event=10,
    )
    ctra = _entry(
        ticker="CTRA",
        catalyst_type="merger_acquisition",
        materiality="high",
        evidence_quote="CTRA entered into a definitive merger agreement.",
        deal_spread_pct=7.0,  # wide spread — not priced in
        days_since_event=10,
    )
    r_vg = compute_scaled_catalyst_adjustment(vg)
    r_ctra = compute_scaled_catalyst_adjustment(ctra)
    assert r_vg["scaled_adjustment"] < r_ctra["scaled_adjustment"], (
        f"VG settlement ({r_vg['scaled_adjustment']:.2f}) should be < "
        f"CTRA full merger ({r_ctra['scaled_adjustment']:.2f})"
    )


# ── 8. Invalid/weak evidence → zero adjustment ────────────────────────────────

def test_invalid_evidence_gives_zero_adjustment():
    """validation_status=invalid yields scaled_adjustment=0."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(validation_status="invalid_quote", quote_validation_pass=False)
    result = compute_scaled_catalyst_adjustment(e)
    assert result["scaled_adjustment"] == 0.0
    assert result["static_adjustment"] == 0.0


# ── 9. Bearish regulatory catalyst remains negative ──────────────────────────

def test_bearish_regulatory_catalyst_remains_negative():
    """Bearish catalyst → negative scaled_adjustment (not zero or positive)."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(
        catalyst_type="regulatory",
        sentiment="bearish",
        confidence=0.85,
        evidence_quote="FDA rejected the drug application.",
    )
    result = compute_scaled_catalyst_adjustment(e)
    assert result["scaled_adjustment"] < 0.0


# ── 10. No production score mutation ─────────────────────────────────────────

def test_no_production_score_mutation():
    """compute_scaled_catalyst_adjustment never modifies original_score or shadow_score."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(original_score=43.5, shadow_score=48.5)
    before_orig = e["original_score"]
    before_shadow = e["shadow_score"]
    result = compute_scaled_catalyst_adjustment(e)
    assert e["original_score"] == before_orig
    assert e["shadow_score"] == before_shadow
    assert "original_score" not in result
    assert "shadow_score" not in result


# ── 11. scaled_shadow_score = original_score + scaled_adjustment ─────────────

def test_scaled_shadow_score_field():
    """Result includes scaled_shadow_score = original_score + scaled_adjustment."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(original_score=43.5)
    result = compute_scaled_catalyst_adjustment(e)
    expected = 43.5 + result["scaled_adjustment"]
    assert abs(result["scaled_shadow_score"] - expected) < 0.01


# ── 12. Final adjustment is capped at ±5 ─────────────────────────────────────

def test_adjustment_capped_at_plus_minus_5():
    """scaled_adjustment is clamped to [-5, +5]."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    e = _entry(confidence=1.0, materiality="high", days_since_event=1)
    result = compute_scaled_catalyst_adjustment(e)
    assert -5.0 <= result["scaled_adjustment"] <= 5.0
