"""JSON schema and validation for LLM catalyst enrichment output."""
from __future__ import annotations

import json
from dataclasses import dataclass

CATALYST_TYPES = frozenset([
    "earnings", "merger_acquisition", "guidance", "product_launch",
    "regulatory", "management_change", "macro", "sector_rotation", "unknown",
])
MATERIALITY_VALUES = frozenset(["high", "medium", "low", "none"])
SENTIMENT_VALUES = frozenset(["bullish", "bearish", "neutral", "mixed"])

_REQUIRED_FIELDS = (
    "event_id", "ticker", "event_date", "catalyst_type", "materiality",
    "sentiment", "confidence", "evidence_quote", "reasoning_short",
    "should_affect_score", "provider", "enriched_at",
)


@dataclass
class CatalystEnrichment:
    event_id: str
    ticker: str
    event_date: str
    catalyst_type: str
    materiality: str
    sentiment: str
    confidence: float
    evidence_quote: str
    reasoning_short: str
    should_affect_score: bool
    provider: str
    enriched_at: str
    # Validation fields — populated by classify_events() post-processing
    model_should_affect_score: bool = False
    validation_status: str = "valid"
    quote_validation_pass: bool = True
    invalid_reason: str = ""
    source_text_char_count: int = 0

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "ticker": self.ticker,
            "event_date": self.event_date,
            "catalyst_type": self.catalyst_type,
            "materiality": self.materiality,
            "sentiment": self.sentiment,
            "confidence": self.confidence,
            "evidence_quote": self.evidence_quote,
            "reasoning_short": self.reasoning_short,
            "should_affect_score": self.should_affect_score,
            "provider": self.provider,
            "enriched_at": self.enriched_at,
            "model_should_affect_score": self.model_should_affect_score,
            "validation_status": self.validation_status,
            "quote_validation_pass": self.quote_validation_pass,
            "invalid_reason": self.invalid_reason,
            "source_text_char_count": self.source_text_char_count,
        }

    def to_jsonl_line(self) -> str:
        return json.dumps(self.to_dict())


def validate_enrichment(data: dict) -> tuple[bool, list[str]]:
    """Returns (is_valid, errors). Empty errors list means valid."""
    errors: list[str] = []

    for field in _REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"missing required field: {field}")

    if errors:
        return False, errors

    if data.get("catalyst_type") not in CATALYST_TYPES:
        errors.append(
            f"catalyst_type '{data.get('catalyst_type')}' not in {sorted(CATALYST_TYPES)}"
        )
    if data.get("materiality") not in MATERIALITY_VALUES:
        errors.append(
            f"materiality '{data.get('materiality')}' not in {sorted(MATERIALITY_VALUES)}"
        )
    if data.get("sentiment") not in SENTIMENT_VALUES:
        errors.append(
            f"sentiment '{data.get('sentiment')}' not in {sorted(SENTIMENT_VALUES)}"
        )
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        errors.append(f"confidence must be float in [0.0, 1.0], got {confidence!r}")

    return len(errors) == 0, errors
