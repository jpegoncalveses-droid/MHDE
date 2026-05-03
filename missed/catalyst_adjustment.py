"""Scaled shadow catalyst adjustment v0.

Computes a scaled adjustment on top of the static shadow adjustment.
Shadow-only: never modifies production scores.

Design rules:
- Priced-in (observable spread) suppresses time_decay — no double-counting
- Smooth time decay: max(0, 1 - days/90) only when no observable spread
- Risk haircut from existing risk_penalty field only (no computed ratios)
- Cap: ±5 points
"""
from __future__ import annotations

# ── Static base adjustments by materiality × sentiment ───────────────────────

_STATIC_TABLE: dict[tuple[str, str], float] = {
    ("high", "bullish"): 5.0,
    ("high", "bearish"): -5.0,
    ("medium", "bullish"): 3.0,
    ("medium", "bearish"): -3.0,
    ("low", "bullish"): 1.5,
    ("low", "bearish"): -1.5,
}

# ── Catalyst type scope multiplier ───────────────────────────────────────────

_SCOPE_MULTIPLIER: dict[str, float] = {
    "merger_acquisition": 1.0,
    "earnings_surprise": 0.85,
    "regulatory": 0.75,
    "litigation_resolution": 0.70,
    "management_change": 0.50,
    "dividend": 0.60,
    "share_buyback": 0.55,
    "guidance": 0.80,
    "product_launch": 0.65,
    "partnership": 0.60,
}
_DEFAULT_SCOPE = 0.65


def compute_scaled_catalyst_adjustment(entry: dict) -> dict:
    """Compute deterministic scaled adjustment for a shadow catalyst entry.

    Returns a new dict — never mutates the input.
    Keys: static_adjustment, scaled_adjustment, evidence_confidence,
          impact_estimate, adjustment_reason, risk_adjustment,
          time_decay_applied, scaled_shadow_score.
    """
    catalyst_type = entry.get("catalyst_type", "")
    materiality = (entry.get("materiality") or "medium").lower()
    sentiment = (entry.get("sentiment") or "neutral").lower()
    confidence = float(entry.get("confidence") or 0.0)
    validation_status = entry.get("validation_status", "")
    quote_pass = entry.get("quote_validation_pass", True)
    original_score = float(entry.get("original_score") or 0.0)
    risk_penalty = entry.get("risk_penalty")
    days_since_event = int(entry.get("days_since_event") or 0)
    deal_spread_pct = entry.get("deal_spread_pct")

    # ── 1. Evidence validity gate ─────────────────────────────────────────────
    invalid_statuses = {"invalid", "invalid_quote", "weak", "rejected"}
    evidence_invalid = (
        validation_status in invalid_statuses
        or not quote_pass
    )
    evidence_confidence = 0.0 if evidence_invalid else min(1.0, max(0.0, confidence))

    if evidence_invalid:
        return {
            "static_adjustment": 0.0,
            "scaled_adjustment": 0.0,
            "evidence_confidence": 0.0,
            "impact_estimate": "none",
            "adjustment_reason": "invalid_evidence",
            "risk_adjustment": "not_available" if risk_penalty is None else 1.0,
            "time_decay_applied": False,
            "scaled_shadow_score": original_score,
        }

    # ── 2. Static base adjustment ─────────────────────────────────────────────
    static_adjustment = _STATIC_TABLE.get((materiality, sentiment), 0.0)

    # ── 3. Scope multiplier ───────────────────────────────────────────────────
    scope = _SCOPE_MULTIPLIER.get(catalyst_type, _DEFAULT_SCOPE)

    # ── 4. Priced-in vs time-decay (mutually exclusive) ───────────────────────
    if deal_spread_pct is not None:
        # Observable spread → priced-in penalty, time_decay suppressed
        time_decay_applied = False
        spread = float(deal_spread_pct)
        if spread < 2.0:
            # Nearly all priced in — strong haircut
            market_factor = 0.20
        elif spread < 5.0:
            # Partially priced in
            market_factor = 0.55
        else:
            # Wide spread — not priced in, minimal haircut
            market_factor = 0.90
        reason_suffix = f"spread={spread:.1f}%"
    else:
        # No observable spread → apply smooth time decay
        time_decay_applied = True
        decay = max(0.0, 1.0 - days_since_event / 90.0)
        market_factor = decay
        reason_suffix = f"days={days_since_event}"

    # ── 5. Risk haircut ───────────────────────────────────────────────────────
    if risk_penalty is None:
        risk_adjustment = "not_available"
        risk_factor = 1.0
    else:
        rp = float(risk_penalty)
        # Normalise: risk_penalty of 0 → 1.0, penalty of 100 → 0.5
        risk_factor = max(0.5, 1.0 - rp / 200.0)
        risk_adjustment = risk_factor

    # ── 6. Compute scaled adjustment ─────────────────────────────────────────
    raw = static_adjustment * evidence_confidence * scope * market_factor * risk_factor
    scaled_adjustment = max(-5.0, min(5.0, raw))

    # ── 7. Impact estimate label ──────────────────────────────────────────────
    abs_adj = abs(scaled_adjustment)
    if abs_adj >= 3.5:
        impact_estimate = "high"
    elif abs_adj >= 1.5:
        impact_estimate = "medium"
    elif abs_adj > 0:
        impact_estimate = "low"
    else:
        impact_estimate = "none"

    # ── 8. Reason string ──────────────────────────────────────────────────────
    adjustment_reason = (
        f"{catalyst_type}/{materiality}/{sentiment}; {reason_suffix}; "
        f"scope={scope:.2f}; conf={evidence_confidence:.2f}"
    )

    scaled_shadow_score = original_score + scaled_adjustment

    return {
        "static_adjustment": static_adjustment,
        "scaled_adjustment": round(scaled_adjustment, 4),
        "evidence_confidence": evidence_confidence,
        "impact_estimate": impact_estimate,
        "adjustment_reason": adjustment_reason,
        "risk_adjustment": risk_adjustment,
        "time_decay_applied": time_decay_applied,
        "scaled_shadow_score": round(scaled_shadow_score, 4),
    }
