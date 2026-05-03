from __future__ import annotations

# Minimum number of major components that must be observed to assign a real tier.
# Major components: cheap, quality, catalyst, momentum, sentiment (5 total).
# 0 or 1 observed → Incomplete (not enough signal to rank).
# 2+ observed → score normally, apply missing-data penalty via confidence label.
# Valuation missing alone should NOT force Incomplete if quality+catalyst are present.
_MIN_OBSERVED_COMPONENTS = 2

# Kept for tests that still reference it via import
_COVERAGE_THRESHOLD = 0.50


def assign_tier(
    total_score: float,
    catalyst_score: float | None,
    risk_penalty: float | None,
    coverage: float = 1.0,
    observed_count: int | None = None,
) -> str:
    """
    Assign a tier to a candidate.

    observed_count: number of major components (cheap/quality/catalyst/momentum/sentiment)
    with non-null scores. When < _MIN_OBSERVED_COMPONENTS, return "Incomplete".
    Falls back to coverage-based rule when observed_count is None (backward compat).

    Tiers: A > B > C > Reject > Incomplete
    "Incomplete" means: interesting signals may exist but data coverage is too thin to rank.
    """
    risk = risk_penalty if risk_penalty is not None else 0.0
    catalyst = catalyst_score if catalyst_score is not None else 0.0

    # Catastrophic risk always rejects regardless of coverage
    if risk > 75:
        return "Reject"

    # Insufficient data — cannot rank
    if observed_count is not None:
        if observed_count < _MIN_OBSERVED_COMPONENTS:
            return "Incomplete"
    else:
        # Legacy: coverage-based fallback for callers that don't pass observed_count
        if coverage < _COVERAGE_THRESHOLD:
            return "Incomplete"

    # Full tier logic
    if total_score >= 75 and catalyst >= 50 and risk <= 50 and coverage >= 0.80:
        return "A"
    if total_score >= 60 and coverage >= 0.65:
        return "B"
    if total_score >= 45:
        return "C"
    return "Reject"
