from __future__ import annotations


def assign_tier(
    total_score: float,
    catalyst_score: float | None,
    risk_penalty: float | None,
    missing_fields: bool = False,
) -> str:
    if missing_fields or total_score < 45 or (risk_penalty is not None and risk_penalty > 75):
        return "Reject"
    catalyst = catalyst_score or 0.0
    risk = risk_penalty or 0.0
    if total_score >= 75 and catalyst >= 50 and risk <= 50:
        return "A"
    if total_score >= 60:
        return "B"
    if total_score >= 45:
        return "C"
    return "Reject"
