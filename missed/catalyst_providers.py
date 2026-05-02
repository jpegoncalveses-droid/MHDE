"""Provider abstraction for catalyst classification.

MockCatalystProvider  — deterministic, zero API calls (default/test path)
OpenAICatalystProvider — real OpenAI call; retries once on bad JSON;
                         returns error record (not mock) on double failure

get_provider() raises CatalystProviderError if --no-mock requested
but no API key is available. Never silently falls back to mock.

preflight_check()  — verify openai importable + API key set before starting.
QuotaExceededError — subclass of CatalystProviderError; insufficient_quota is
                     non-retryable and propagates immediately.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from missed.catalyst_schema import (
    CatalystEnrichment,
    CATALYST_TYPES,
    MATERIALITY_VALUES,
    SENTIMENT_VALUES,
    validate_enrichment,
)

logger = logging.getLogger("mhde.missed.catalyst_providers")


class CatalystProviderError(Exception):
    """Raised when a real provider is requested but cannot be configured."""


class QuotaExceededError(CatalystProviderError):
    """Raised when OpenAI billing quota is exhausted (non-retryable)."""


def preflight_check(api_key: str) -> None:
    """Verify openai package is importable and api_key is set.

    Call this before starting a real provider run so failures surface before
    any events are sampled or the cache is touched.

    Raises CatalystProviderError with a clear message on any failure.
    """
    try:
        import openai  # noqa: F401
    except ImportError:
        raise CatalystProviderError(
            "openai package not installed. Run: pip install openai"
        )
    if not api_key:
        raise CatalystProviderError(
            "OPENAI_API_KEY environment variable not set. "
            "Set it or use --mock for offline operation."
        )


def _is_quota_exceeded(exc: Exception) -> bool:
    """Return True when the exception signals billing quota exhausted."""
    return getattr(exc, "code", None) == "insufficient_quota"


class BaseCatalystProvider:
    name: str = "base"

    def classify(self, event: dict, prompt: str) -> CatalystEnrichment:
        raise NotImplementedError


# ── Mock provider ─────────────────────────────────────────────────────────────

import hashlib as _hashlib

_FORM_TO_CATALYST: dict[str, str] = {
    "8-K": "guidance", "10-K": "earnings", "10-Q": "earnings",
    "20-F": "earnings", "40-F": "earnings", "S-1": "product_launch",
    "S-4": "merger_acquisition", "SC TO-T": "merger_acquisition",
    "DEFM14A": "merger_acquisition", "DEF 14A": "management_change",
}
_SCORE_AFFECTING = frozenset(["earnings", "merger_acquisition", "guidance", "regulatory"])


class MockCatalystProvider(BaseCatalystProvider):
    name = "mock"

    def classify(self, event: dict, prompt: str) -> CatalystEnrichment:
        event_id = event.get("event_id") or ""
        h = int(_hashlib.sha256(f"{event_id}:catalyst_pilot_v1".encode()).hexdigest(), 16)
        filing_form = (event.get("filing_form_type") or "").split("/")[0].strip()
        catalyst_type = _FORM_TO_CATALYST.get(filing_form, "unknown")
        materiality = ["high", "medium", "low"][h % 3]
        return_value = event.get("return_value") or 0.0
        sentiment = "bullish" if return_value >= 0 else "bearish"
        confidence = round(0.40 + (h % 40) / 100.0, 2)
        filing_desc = event.get("filing_description") or ""
        event_date = event.get("event_date", "")
        if hasattr(event_date, "isoformat"):
            event_date = event_date.isoformat()
        return CatalystEnrichment(
            event_id=event_id, ticker=event.get("ticker", ""),
            event_date=str(event_date), catalyst_type=catalyst_type,
            materiality=materiality, sentiment=sentiment, confidence=confidence,
            evidence_quote=filing_desc[:120].strip(),
            reasoning_short=(
                f"[Mock] {filing_form or 'no filing'} before {return_value:.1f}% move. "
                "Deterministic placeholder — wire real LLM to replace."
            ),
            should_affect_score=catalyst_type in _SCORE_AFFECTING,
            provider="mock",
            enriched_at=datetime.now(tz=timezone.utc).isoformat(),
        )


# ── OpenAI provider ───────────────────────────────────────────────────────────

class OpenAICatalystProvider(BaseCatalystProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        self._api_key = api_key
        self.model = model

    def _call_api(self, prompt: str) -> str:
        """Make one API call; returns raw response text. Separated for testability."""
        import openai
        client = openai.OpenAI(api_key=self._api_key)
        parts = prompt.split("\n\nUSER:\n", 1)
        system_text = parts[0].replace("SYSTEM:\n", "").strip()
        user_text = parts[1].strip() if len(parts) > 1 else prompt
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        return response.choices[0].message.content or ""

    def _make_error_record(self, event: dict, reason: str) -> CatalystEnrichment:
        event_date = event.get("event_date", "")
        if hasattr(event_date, "isoformat"):
            event_date = event_date.isoformat()
        return CatalystEnrichment(
            event_id=event.get("event_id", ""),
            ticker=event.get("ticker", ""),
            event_date=str(event_date),
            catalyst_type="unknown",
            materiality="none",
            sentiment="neutral",
            confidence=0.0,
            evidence_quote="",
            reasoning_short=f"[ERROR] {reason}",
            should_affect_score=False,
            provider="openai_error",
            enriched_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    def _parse_and_validate(self, raw: str, event: dict) -> CatalystEnrichment | None:
        """Parse raw JSON response; return None on any failure."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        event_date = event.get("event_date", "")
        if hasattr(event_date, "isoformat"):
            event_date = event_date.isoformat()

        enrichment_dict = {
            "event_id": event.get("event_id", ""),
            "ticker": event.get("ticker", ""),
            "event_date": str(event_date),
            "catalyst_type": data.get("catalyst_type", "unknown"),
            "materiality": data.get("materiality", "none"),
            "sentiment": data.get("sentiment", "neutral"),
            "confidence": data.get("confidence", 0.0),
            "evidence_quote": data.get("evidence_quote", ""),
            "reasoning_short": data.get("reasoning_short", ""),
            "should_affect_score": bool(data.get("should_affect_score", False)),
            "provider": self.name,
            "enriched_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        is_valid, errors = validate_enrichment(enrichment_dict)
        if not is_valid:
            logger.debug("Schema validation failed: %s", errors)
            return None
        return CatalystEnrichment(**enrichment_dict)

    def classify(self, event: dict, prompt: str) -> CatalystEnrichment:
        """Classify one event; retry once on transient failure; fail-fast on quota error."""
        for attempt in range(2):
            try:
                raw = self._call_api(prompt)
                result = self._parse_and_validate(raw, event)
                if result is not None:
                    return result
                logger.warning(
                    "Attempt %d: invalid response for %s — %r",
                    attempt + 1, event.get("ticker"), raw[:100],
                )
            except Exception as exc:
                if _is_quota_exceeded(exc):
                    raise QuotaExceededError(
                        "OpenAI billing quota exceeded. Add credits or use --mock."
                    ) from exc
                logger.warning("Attempt %d: API error for %s: %s",
                               attempt + 1, event.get("ticker"), exc)
        return self._make_error_record(event, "JSON parse/validation failed after 2 attempts")


# ── Factory ───────────────────────────────────────────────────────────────────

def get_provider(
    use_mock: bool,
    provider_name: str,
    model: str,
    cfg: dict | None,
) -> BaseCatalystProvider:
    if use_mock:
        return MockCatalystProvider()

    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or (cfg or {}).get("llm", {}).get("openai_api_key")
    )
    if not api_key:
        raise CatalystProviderError(
            "OPENAI_API_KEY environment variable not set. "
            "Set it or use --mock for offline operation."
        )

    if provider_name == "openai":
        return OpenAICatalystProvider(api_key=api_key, model=model)

    raise CatalystProviderError(f"Unknown provider: {provider_name!r}. Supported: openai")
