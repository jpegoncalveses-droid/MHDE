from __future__ import annotations

import pytest

from llm.local_provider import MockProvider
from llm.schemas import LLMOutput


def test_mock_provider_returns_llm_output():
    provider = MockProvider()
    output = provider.generate("AAPL", "hypothesis_generation", {"company_name": "Apple", "total_score": 72})
    assert isinstance(output, LLMOutput)
    assert output.ticker == "AAPL"
    assert output.provider == "mock"
    assert output.confidence == "low"
    assert output.recommended_action == "watch"
    assert "[Mock]" in output.thesis


def test_mock_provider_never_raises():
    provider = MockProvider()
    output = provider.generate("BAD", "any", {})
    assert output is not None


def test_openai_provider_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from llm.openai_provider import OpenAIProvider
    provider = OpenAIProvider({})
    output = provider.generate("NVDA", "hypothesis_generation", {})
    assert output.provider == "mock"


def test_nvidia_provider_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    from llm.nvidia_provider import NvidiaProvider
    provider = NvidiaProvider({})
    output = provider.generate("TSLA", "hypothesis_generation", {})
    assert output.provider == "mock"


def test_llm_output_to_dict():
    output = LLMOutput(
        ticker="TEST", company="Test", thesis="thesis", why_now="now",
        cheap_evidence=["e1"], quality_evidence=[], catalyst_evidence=[],
        risks=["r1"], missing_evidence=[], confidence="high",
        recommended_action="research", provider="mock", model="mock",
    )
    d = output.to_dict()
    assert d["ticker"] == "TEST"
    assert d["confidence"] == "high"
    assert "cheap_evidence" in d
