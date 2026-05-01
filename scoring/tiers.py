from __future__ import annotations

# Minimum fraction of positive-weight components that must be observed to assign a real tier.
# positive weights: cheap=0.30, quality=0.25, catalyst=0.25, momentum=0.10, sentiment=0.10 → sum=1.00
# With only quality+catalyst observed: coverage=0.50 → below threshold → Incomplete
# With cheap+quality+catalyst: coverage=0.80 → above threshold → can rank
_COVERAGE_THRESHOLD = 0.60


def assign_tier(
    total_score: float,
    catalyst_score: float | None,
    risk_penalty: float | None,
    coverage: float = 1.0,
) -> str:
    """
    Assign a tier to a candidate.

    coverage: fraction of positive-weight components with real observed data (0.0–1.0).
    When coverage < _COVERAGE_THRESHOLD, the data is insufficient to rank — return "Incomplete".

    Tiers: A > B > C > Reject > Incomplete
    "Incomplete" means: interesting signals exist but data coverage is too thin to rank.
    """
    risk = risk_penalty if risk_penalty is not None else 0.0
    catalyst = catalyst_score if catalyst_score is not None else 0.0

    # Catastrophic risk always rejects regardless of coverage
    if risk > 75:
        return "Reject"

    # Insufficient data coverage — cannot rank
    if coverage < _COVERAGE_THRESHOLD:
        return "Incomplete"

    # Full tier logic (requires coverage >= _COVERAGE_THRESHOLD)
    if total_score >= 75 and catalyst >= 50 and risk <= 50 and coverage >= 0.80:
        return "A"
    if total_score >= 60 and coverage >= 0.65:
        return "B"
    if total_score >= 45:
        return "C"
    return "Reject"
