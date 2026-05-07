from __future__ import annotations

FALSE_POSITIVE_REASONS = [
    "bad_data",
    "stale_data",
    "cheap_for_good_reason",
    "weak_catalyst",
    "poor_quality_business",
    "macro_headwind",
    "llm_overstated_case",
    "missing_peer_context",
    "temporary_noise",
    "not_actionable",
    "overfit_score",
    "insufficient_liquidity",
    "missing_risk_factor",
    "source_failure",
    "other",
]

REVIEW_STATUSES = [
    "pending",
    "useful",
    "weak",
    "false_positive",
    "needs_more_evidence",
    "invalid_due_to_data_issue",
    "archived",
]

EXPERIMENT_STATUSES = [
    "proposed",
    "tested",
    "approved",
    "rejected",
    "applied",
    "archived",
]

SCORE_RANGE = (1, 5)
