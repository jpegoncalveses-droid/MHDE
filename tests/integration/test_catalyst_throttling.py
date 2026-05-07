"""TDD tests for OpenAI throttling, rate-limit handling, and provider preflight.

RED first — these fail until the implementation is in place.
"""
from __future__ import annotations

import json
import sys

import pytest

from missed.catalyst_classifier import classify_events
from missed.catalyst_providers import (
    BaseCatalystProvider,
    CatalystProviderError,
    OpenAICatalystProvider,
    preflight_check,
)
from missed.catalyst_schema import CatalystEnrichment


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_event(event_id: str, ticker: str) -> dict:
    return {
        "event_id": event_id,
        "ticker": ticker,
        "event_date": "2026-01-01",
        "filing_form_type": "8-K",
        "filing_description": "test filing",
        "return_value": 5.0,
    }


class _RecordingProvider(BaseCatalystProvider):
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, event: dict, prompt: str) -> CatalystEnrichment:
        self.calls.append(event.get("event_id", ""))
        return CatalystEnrichment(
            event_id=event.get("event_id", ""),
            ticker=event.get("ticker", ""),
            event_date="2026-01-01",
            catalyst_type="earnings",
            materiality="high",
            sentiment="bullish",
            confidence=0.8,
            evidence_quote="test",
            reasoning_short="ok",
            should_affect_score=True,
            provider="recording",
            enriched_at="2026-01-01T00:00:00+00:00",
        )


def _good_json_response() -> str:
    return json.dumps({
        "catalyst_type": "earnings",
        "materiality": "high",
        "sentiment": "bullish",
        "confidence": 0.9,
        "evidence_quote": "Q4 beat estimates",
        "reasoning_short": "Strong earnings",
        "should_affect_score": True,
    })


# ── throttling: sleep between uncached calls ──────────────────────────────────

def test_throttle_sleeps_between_uncached_calls(monkeypatch):
    """RPM=3 → sleep of ~20.5s inserted between two uncached API calls."""
    sleep_calls: list[float] = []
    mono_seq = iter([1000.0, 1001.0, 1001.5, 1022.5])
    monkeypatch.setattr("missed.catalyst_classifier.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("missed.catalyst_classifier.time.monotonic", lambda: next(mono_seq))

    events = [_make_event("e1", "AAPL"), _make_event("e2", "MSFT")]
    provider = _RecordingProvider()
    classify_events(events, _provider=provider, rpm_limit=3, cache_path=None, refresh_cache=True)

    assert len(sleep_calls) == 1
    assert abs(sleep_calls[0] - 20.5) < 0.01  # min_spacing=21 - elapsed=0.5


def test_throttle_first_call_no_sleep(monkeypatch):
    """First uncached call never sleeps — no prior API call to space from."""
    sleep_calls: list[float] = []
    mono_seq = iter([1000.0, 1001.0])  # elapsed check + set last_api_call
    monkeypatch.setattr("missed.catalyst_classifier.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("missed.catalyst_classifier.time.monotonic", lambda: next(mono_seq))

    events = [_make_event("e1", "AAPL")]
    classify_events(events, _provider=_RecordingProvider(), rpm_limit=3,
                    cache_path=None, refresh_cache=True)

    assert sleep_calls == []


def test_throttle_skips_sleep_for_cache_hits(tmp_path, monkeypatch):
    """Cache hits do not trigger any sleep even when rpm_limit is set."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("missed.catalyst_classifier.time.sleep", lambda s: sleep_calls.append(s))

    cache_path = str(tmp_path / "cache.jsonl")
    events = [_make_event("e1", "AAPL"), _make_event("e2", "MSFT")]

    # First run: populate cache (no-op sleeps via mock)
    classify_events(events, _provider=_RecordingProvider(), rpm_limit=3,
                    cache_path=cache_path)
    sleep_calls.clear()

    # Second run: all cache hits — no sleep
    classify_events(events, _provider=_RecordingProvider(), rpm_limit=3,
                    cache_path=cache_path)

    assert sleep_calls == []


def test_throttle_no_sleep_when_rpm_limit_none(monkeypatch):
    """rpm_limit=None → time.sleep is never called."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("missed.catalyst_classifier.time.sleep", lambda s: sleep_calls.append(s))

    events = [_make_event("e1", "AAPL"), _make_event("e2", "MSFT")]
    classify_events(events, _provider=_RecordingProvider(), rpm_limit=None,
                    cache_path=None, refresh_cache=True)

    assert sleep_calls == []


def test_throttle_cache_miss_after_cache_hit_uses_no_prior_call_time(tmp_path, monkeypatch):
    """If first event is a cache hit and second is a miss, no sleep (no prior API call)."""
    sleep_calls: list[float] = []
    # monotonic only called for the one cache miss event
    mono_seq = iter([1000.0, 1001.0])
    monkeypatch.setattr("missed.catalyst_classifier.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("missed.catalyst_classifier.time.monotonic", lambda: next(mono_seq))

    cache_path = str(tmp_path / "cache.jsonl")
    e1 = _make_event("e1", "AAPL")
    e2 = _make_event("e2", "MSFT")

    # Pre-populate cache for e1 only
    classify_events([e1], _provider=_RecordingProvider(), rpm_limit=None,
                    cache_path=cache_path)
    sleep_calls.clear()

    # Now run [e1 (hit), e2 (miss)]: e2 is the first API call, so no sleep
    classify_events([e1, e2], _provider=_RecordingProvider(), rpm_limit=3,
                    cache_path=cache_path)

    assert sleep_calls == []


# ── rate-limit error handling ─────────────────────────────────────────────────

def test_rate_limit_exceeded_retried(monkeypatch):
    """rate_limit_exceeded → first attempt fails, second attempt succeeds."""

    class _FakeRateLimitError(Exception):
        code = "rate_limit_exceeded"

    call_count = [0]

    def fake_call_api(prompt: str) -> str:
        call_count[0] += 1
        if call_count[0] == 1:
            raise _FakeRateLimitError("429 rate limited")
        return _good_json_response()

    provider = OpenAICatalystProvider(api_key="sk-fake", model="gpt-4o-mini")
    monkeypatch.setattr(provider, "_call_api", fake_call_api)

    result = provider.classify(_make_event("e1", "AAPL"), "prompt")

    assert call_count[0] == 2
    assert result.catalyst_type == "earnings"
    assert "[ERROR]" not in result.reasoning_short


def test_insufficient_quota_fails_fast(monkeypatch):
    """insufficient_quota → CatalystProviderError raised on first attempt, no retry."""
    from missed.catalyst_providers import QuotaExceededError

    class _FakeQuotaError(Exception):
        code = "insufficient_quota"

    call_count = [0]

    def fake_call_api(prompt: str) -> str:
        call_count[0] += 1
        raise _FakeQuotaError("quota exceeded")

    provider = OpenAICatalystProvider(api_key="sk-fake", model="gpt-4o-mini")
    monkeypatch.setattr(provider, "_call_api", fake_call_api)

    with pytest.raises(CatalystProviderError, match="billing quota"):
        provider.classify(_make_event("e1", "AAPL"), "prompt")

    assert call_count[0] == 1  # no retry


# ── provider preflight ────────────────────────────────────────────────────────

def test_preflight_raises_if_openai_not_importable(monkeypatch):
    """preflight_check raises CatalystProviderError when openai package absent."""
    monkeypatch.setitem(sys.modules, "openai", None)  # makes import openai raise ImportError
    with pytest.raises(CatalystProviderError, match="openai package"):
        preflight_check("sk-fake")


def test_preflight_raises_if_no_api_key(monkeypatch):
    """preflight_check raises CatalystProviderError when api_key is empty."""
    import types
    monkeypatch.setitem(sys.modules, "openai", types.ModuleType("openai"))
    with pytest.raises(CatalystProviderError, match="OPENAI_API_KEY"):
        preflight_check("")


def test_preflight_passes_with_valid_inputs(monkeypatch):
    """preflight_check does not raise when openai is importable and key is set."""
    import types
    monkeypatch.setitem(sys.modules, "openai", types.ModuleType("openai"))
    preflight_check("sk-fake-key-for-testing")  # should not raise


# ── CLI option ────────────────────────────────────────────────────────────────

def test_cli_has_rpm_limit_option():
    """The missed pilot CLI must expose --rpm-limit."""
    from click.testing import CliRunner
    from main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "pilot", "--help"])
    assert "--rpm-limit" in result.output
