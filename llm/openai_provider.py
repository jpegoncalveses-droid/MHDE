from __future__ import annotations

import json
import logging
import os

from llm.provider import BaseLLMProvider
from llm.schemas import LLMOutput
from llm.local_provider import MockProvider

logger = logging.getLogger("mhde.llm.openai")

_PROMPT_TEMPLATE = """
You are an equity research assistant. Analyze the following stock candidate.

Ticker: {ticker}
Company: {company}
Score: {total_score:.0f}/100 (Tier: {tier})
Cheap score: {cheap_score:.0f}, Quality score: {quality_score:.0f}
Catalyst score: {catalyst_score:.0f}, Risk penalty: {risk_penalty:.0f}

Produce a concise analysis in JSON format with these keys:
thesis, why_now, cheap_evidence (list), quality_evidence (list),
catalyst_evidence (list), risks (list), missing_evidence (list),
confidence (low/medium/high), recommended_action (watch/research/reject).

Be concise. Max 3 bullet points per list. State all limitations clearly.
"""


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api_key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
        self.model = cfg.get("llm", {}).get("openai", {}).get("model", "gpt-4.1-mini")
        self._fallback = MockProvider()

    def generate(self, ticker: str, job_type: str, context: dict) -> LLMOutput:
        if not self.api_key:
            logger.warning("OPENAI_API_KEY not set — falling back to mock provider")
            return self._fallback.generate(ticker, job_type, context)

        try:
            import openai
            client = openai.OpenAI(api_key=self.api_key)
            prompt = _PROMPT_TEMPLATE.format(
                ticker=ticker,
                company=context.get("company_name", ticker),
                total_score=context.get("total_score", 0),
                tier=context.get("tier", "?"),
                cheap_score=context.get("cheap_score", 0),
                quality_score=context.get("quality_score", 0),
                catalyst_score=context.get("catalyst_score", 0),
                risk_penalty=context.get("risk_penalty", 0),
            )
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            data = json.loads(raw)
            return LLMOutput(
                ticker=ticker,
                company=context.get("company_name", ticker),
                thesis=data.get("thesis", ""),
                why_now=data.get("why_now", ""),
                cheap_evidence=data.get("cheap_evidence", []),
                quality_evidence=data.get("quality_evidence", []),
                catalyst_evidence=data.get("catalyst_evidence", []),
                risks=data.get("risks", []),
                missing_evidence=data.get("missing_evidence", []),
                confidence=data.get("confidence", "low"),
                recommended_action=data.get("recommended_action", "watch"),
                provider="openai",
                model=self.model,
            )
        except ImportError:
            logger.warning("openai package not installed — falling back to mock")
            return self._fallback.generate(ticker, job_type, context)
        except Exception as exc:
            logger.error("OpenAI call failed for %s: %s", ticker, exc)
            result = self._fallback.generate(ticker, job_type, context)
            result.error = str(exc)
            return result
