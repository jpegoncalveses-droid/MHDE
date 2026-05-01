from __future__ import annotations

import json
import logging
import os

from llm.provider import BaseLLMProvider
from llm.schemas import LLMOutput
from llm.local_provider import MockProvider

logger = logging.getLogger("mhde.llm.nvidia")

_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_PROMPT_TEMPLATE = """Analyze this stock candidate and respond in JSON only.

Ticker: {ticker} | Company: {company}
Score: {total_score:.0f}/100 (Tier: {tier})
Cheap: {cheap_score:.0f} | Quality: {quality_score:.0f} | Catalyst: {catalyst_score:.0f} | Risk: {risk_penalty:.0f}

JSON keys: thesis, why_now, cheap_evidence (list), quality_evidence (list),
catalyst_evidence (list), risks (list), missing_evidence (list),
confidence (low/medium/high), recommended_action (watch/research/reject).
"""


class NvidiaProvider(BaseLLMProvider):
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api_key = cfg.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY")
        llm_cfg = cfg.get("llm", {}).get("nvidia", {})
        self.model = llm_cfg.get("model", "meta/llama-3.1-70b-instruct")
        self.base_url = llm_cfg.get("base_url", _DEFAULT_BASE_URL)
        self._fallback = MockProvider()

    def generate(self, ticker: str, job_type: str, context: dict) -> LLMOutput:
        if not self.api_key:
            logger.warning("NVIDIA_API_KEY not set — falling back to mock")
            return self._fallback.generate(ticker, job_type, context)

        try:
            import openai
            client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
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
            )
            raw = response.choices[0].message.content
            # Strip markdown fences if present
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
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
                provider="nvidia",
                model=self.model,
            )
        except ImportError:
            logger.warning("openai package not installed (needed for NVIDIA NIM) — using mock")
            return self._fallback.generate(ticker, job_type, context)
        except Exception as exc:
            logger.error("NVIDIA call failed for %s: %s", ticker, exc)
            result = self._fallback.generate(ticker, job_type, context)
            result.error = str(exc)
            return result
