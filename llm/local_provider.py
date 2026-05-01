from __future__ import annotations

import logging

from llm.provider import BaseLLMProvider
from llm.schemas import LLMOutput

logger = logging.getLogger("mhde.llm.mock")


class MockProvider(BaseLLMProvider):
    """Deterministic placeholder. Always works. Returns structured output."""

    def generate(self, ticker: str, job_type: str, context: dict) -> LLMOutput:
        company = context.get("company_name", ticker)
        score = context.get("total_score", 0)
        tier = context.get("tier", "?")

        logger.info("[MOCK LLM] %s / %s — returning placeholder", ticker, job_type)

        return LLMOutput(
            ticker=ticker,
            company=company,
            thesis=(
                f"[Mock] {company} ({ticker}) has a score of {score:.0f} (Tier {tier}). "
                "LLM briefs are in mock mode — configure OPENAI_API_KEY or NVIDIA_API_KEY "
                "to enable real LLM analysis."
            ),
            why_now="[Mock] No real LLM configured. Deterministic placeholder.",
            cheap_evidence=["[Mock] Deterministic ranking placeholder."],
            quality_evidence=["[Mock] Deterministic ranking placeholder."],
            catalyst_evidence=["[Mock] Deterministic ranking placeholder."],
            risks=["LLM not configured — analysis is placeholder only"],
            missing_evidence=["Real LLM output — configure API key"],
            confidence="low",
            recommended_action="watch",
            provider="mock",
            model="mock",
        )
