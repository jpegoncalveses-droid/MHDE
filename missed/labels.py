"""Root-cause taxonomy and event type constants for missed-opportunity analysis."""
from __future__ import annotations

ROOT_CAUSES: list[str] = [
    "not_in_universe",
    "missing_price_data",
    "missing_fundamentals",
    # Precise catalyst root causes (replaces broad catalyst_not_classified)
    "text_evidence_available_not_classified",   # material filing exists, catalyst score low
    "needs_llm_text_enrichment",                # text present but requires LLM to extract signal
    "no_public_catalyst_source_found",          # scored ticker, no filings — no public source
    "price_move_without_known_catalyst",        # not scored, no filings — unexplained move
    "routine_event_correctly_suppressed",       # only routine filings (Form 4, SC 13G) before event
    # Legacy / kept for backward-compatibility with existing investigation records
    "missing_catalyst_source",
    "catalyst_not_classified",
    "routine_filing_misclassified",
    "threshold_too_strict",
    "score_weight_issue",
    "feature_missing",
    "feature_wrong",
    "source_latency",
    "sector_logic_missing",
    "foreign_filer_guard_too_strict",
    "data_quality_guard_too_strict",
    "llm_extraction_failure",
    "truly_unpredictable",
    "other",
]

# Text-related root causes that may trigger LLM enrichment
TEXT_RELATED_ROOT_CAUSES: set[str] = {
    "text_evidence_available_not_classified",
    "needs_llm_text_enrichment",
    "catalyst_not_classified",
    "routine_filing_misclassified",
    "llm_extraction_failure",
}

# Root causes that indicate missing_catalyst_source with filing evidence
FILING_RELATED_ROOT_CAUSES: set[str] = {
    "text_evidence_available_not_classified",
    "catalyst_not_classified",
    "routine_filing_misclassified",
    "missing_catalyst_source",
}

# Purely numeric/structural root causes — do NOT trigger LLM
NUMERIC_ROOT_CAUSES: set[str] = {
    "not_in_universe",
    "missing_price_data",
    "missing_fundamentals",
    "no_public_catalyst_source_found",
    "price_move_without_known_catalyst",
    "routine_event_correctly_suppressed",
    "threshold_too_strict",
    "score_weight_issue",
    "feature_missing",
    "feature_wrong",
    "source_latency",
    "sector_logic_missing",
    "foreign_filer_guard_too_strict",
    "data_quality_guard_too_strict",
    "truly_unpredictable",
    "other",
}

EVENT_TYPES: list[str] = [
    "gain_5d_10pct",
    "gain_20d_20pct",
    "gain_60d_30pct",
    "52wk_high_breakout",
    "gap_up",
]

# Thresholds for detection
GAIN_5D_THRESHOLD = 0.10
GAIN_20D_THRESHOLD = 0.20
GAIN_60D_THRESHOLD = 0.30

# Score threshold above which a Reject is considered threshold_too_strict
THRESHOLD_TOO_STRICT_MIN_SCORE = 38.0
