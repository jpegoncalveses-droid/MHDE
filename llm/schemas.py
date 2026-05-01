from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class LLMOutput:
    ticker: str
    company: str
    thesis: str
    why_now: str
    cheap_evidence: List[str] = field(default_factory=list)
    quality_evidence: List[str] = field(default_factory=list)
    catalyst_evidence: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    confidence: str = "low"
    recommended_action: str = "watch"
    provider: str = "mock"
    model: str = "mock"
    prompt_version: str = "v1"
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "company": self.company,
            "thesis": self.thesis,
            "why_now": self.why_now,
            "cheap_evidence": self.cheap_evidence,
            "quality_evidence": self.quality_evidence,
            "catalyst_evidence": self.catalyst_evidence,
            "risks": self.risks,
            "missing_evidence": self.missing_evidence,
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
            "provider": self.provider,
            "model": self.model,
        }
