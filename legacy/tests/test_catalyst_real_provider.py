"""TDD tests for real LLM provider path, cache, and pilot report.

RED state: catalyst_providers.py, catalyst_cache.py, catalyst_report.py
           do not exist yet; classifier and CLI lack new options.

Covers:
- provider interface (base, mock, openai)
- missing API key with --no-mock raises CatalystProviderError (no silent fallback)
- cache hit avoids provider call
- cache refresh forces provider call
- invalid JSON on first call → retry
- invalid JSON on both calls → error record (not mock substitution)
- schema-invalid response → error record
- report generation (markdown + CSV) sections and fields
- CLI options exist (provider, model, cache-path, refresh-cache, report)
- mock path remains deterministic (regression guard)
- no production scoring changes
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from storage.db import get_connection, init_schema


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _sample_event(event_id=None, ticker="AAPL", score=42.0, form="8-K"):
    return {
        "event_id": event_id or uuid.uuid4().hex[:16],
        "ticker": ticker,
        "event_date": "2026-03-15",
        "primary_root_cause": "text_evidence_available_not_classified",
        "root_causes_json": '["text_evidence_available_not_classified"]',
        "event_type": "gain_20d_20pct",
        "return_value": 22.0,
        "was_scored": True,
        "score_before_event": score,
        "filing_form_type": form,
        "filing_date": "2026-03-10",
        "filing_description": "form8k_earnings_release.htm",
    }


def _valid_llm_json(**overrides):
    base = {
        "catalyst_type": "earnings",
        "materiality": "high",
        "sentiment": "bullish",
        "confidence": 0.88,
        "evidence_quote": "Revenue increased 12% YoY",
        "reasoning_short": "Strong earnings beat drove the move.",
        "should_affect_score": True,
    }
    base.update(overrides)
    return json.dumps(base)


# ═════════════════════════════════════════════════════════════════════════════
# PART 1 — Provider abstraction
# ═════════════════════════════════════════════════════════════════════════════

def test_providers_importable():
    from missed.catalyst_providers import (  # noqa: F401
        BaseCatalystProvider,
        MockCatalystProvider,
        OpenAICatalystProvider,
        CatalystProviderError,
        get_provider,
    )


def test_mock_provider_is_base_subclass():
    from missed.catalyst_providers import MockCatalystProvider, BaseCatalystProvider
    p = MockCatalystProvider()
    assert isinstance(p, BaseCatalystProvider)
    assert p.name == "mock"


def test_openai_provider_is_base_subclass():
    from missed.catalyst_providers import OpenAICatalystProvider, BaseCatalystProvider
    p = OpenAICatalystProvider(api_key="sk-test", model="gpt-4o-mini")
    assert isinstance(p, BaseCatalystProvider)
    assert p.name == "openai"


def test_mock_provider_classify_returns_catalyst_enrichment():
    from missed.catalyst_providers import MockCatalystProvider
    from missed.catalyst_schema import CatalystEnrichment
    p = MockCatalystProvider()
    event = _sample_event()
    result = p.classify(event, prompt="test prompt")
    assert isinstance(result, CatalystEnrichment)
    assert result.provider == "mock"


def test_get_provider_returns_mock_when_use_mock_true():
    from missed.catalyst_providers import MockCatalystProvider, get_provider
    p = get_provider(use_mock=True, provider_name="openai", model="gpt-4o-mini", cfg={})
    assert isinstance(p, MockCatalystProvider)


def test_get_provider_raises_when_no_api_key_and_no_mock(monkeypatch):
    """--no-mock without OPENAI_API_KEY must raise CatalystProviderError, not silently mock."""
    from missed.catalyst_providers import get_provider, CatalystProviderError
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(CatalystProviderError, match="OPENAI_API_KEY"):
        get_provider(use_mock=False, provider_name="openai", model="gpt-4o-mini", cfg={})


def test_get_provider_uses_api_key_from_env(monkeypatch):
    from missed.catalyst_providers import OpenAICatalystProvider, get_provider
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testkey")
    p = get_provider(use_mock=False, provider_name="openai", model="gpt-4o-mini", cfg={})
    assert isinstance(p, OpenAICatalystProvider)


# ═════════════════════════════════════════════════════════════════════════════
# PART 2 — Cache behaviour
# ═════════════════════════════════════════════════════════════════════════════

def test_cache_importable():
    from missed.catalyst_cache import load_cache, save_cache, cache_key  # noqa: F401


def test_cache_key_is_deterministic():
    from missed.catalyst_cache import cache_key
    k1 = cache_key("event_id_abc", "openai", "gpt-4o-mini")
    k2 = cache_key("event_id_abc", "openai", "gpt-4o-mini")
    assert k1 == k2


def test_cache_key_differs_by_provider():
    from missed.catalyst_cache import cache_key
    k_mock = cache_key("e1", "mock", "mock")
    k_openai = cache_key("e1", "openai", "gpt-4o-mini")
    assert k_mock != k_openai


def test_load_cache_returns_empty_for_missing_file(tmp_path):
    from missed.catalyst_cache import load_cache
    assert load_cache(str(tmp_path / "nonexistent.jsonl")) == {}


def test_save_and_load_cache_round_trip(tmp_path):
    from missed.catalyst_cache import save_cache, load_cache, cache_key
    path = str(tmp_path / "cache.jsonl")
    k = cache_key("e1", "openai", "gpt-4o-mini")
    data = {"event_id": "e1", "ticker": "AAPL", "catalyst_type": "earnings"}
    save_cache(path, {k: data})
    loaded = load_cache(path)
    assert k in loaded
    assert loaded[k]["catalyst_type"] == "earnings"


def test_classify_events_cache_hit_avoids_provider_call(tmp_path):
    """Second run with same events uses cache; provider.classify not called again."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_providers import MockCatalystProvider
    from missed.catalyst_schema import CatalystEnrichment

    call_count = [0]

    class CountingProvider(MockCatalystProvider):
        def classify(self, event, prompt):
            call_count[0] += 1
            return super().classify(event, prompt)

    events = [_sample_event("fixed_id_001")]
    cache_path = str(tmp_path / "cache.jsonl")

    # First call — populates cache
    classify_events(events, _provider=CountingProvider(), cache_path=cache_path, refresh_cache=False)
    assert call_count[0] == 1

    # Second call — cache hit
    classify_events(events, _provider=CountingProvider(), cache_path=cache_path, refresh_cache=False)
    assert call_count[0] == 1, "Provider should not be called again on cache hit"


def test_refresh_cache_forces_provider_call(tmp_path):
    """refresh_cache=True bypasses cache and calls provider even for cached events."""
    from missed.catalyst_classifier import classify_events
    from missed.catalyst_providers import MockCatalystProvider

    call_count = [0]

    class CountingProvider(MockCatalystProvider):
        def classify(self, event, prompt):
            call_count[0] += 1
            return super().classify(event, prompt)

    events = [_sample_event("fixed_id_002")]
    cache_path = str(tmp_path / "cache.jsonl")

    # First call
    classify_events(events, _provider=CountingProvider(), cache_path=cache_path)
    assert call_count[0] == 1

    # Second call with refresh_cache=True
    classify_events(events, _provider=CountingProvider(), cache_path=cache_path, refresh_cache=True)
    assert call_count[0] == 2, "Provider must be called again when refresh_cache=True"


def test_classify_no_cache_path_does_not_write_files(tmp_path):
    """cache_path=None means no file I/O."""
    from missed.catalyst_classifier import classify_events
    events = [_sample_event()]
    classify_events(events, use_mock=True, cache_path=None)
    assert list(tmp_path.iterdir()) == [], "No files should be written when cache_path=None"


# ═════════════════════════════════════════════════════════════════════════════
# PART 3 — Real provider: retry and error records
# ═════════════════════════════════════════════════════════════════════════════

def test_invalid_json_first_call_retries():
    """OpenAI returns bad JSON on first call → retries → valid result on second."""
    from missed.catalyst_providers import OpenAICatalystProvider

    provider = OpenAICatalystProvider(api_key="sk-test", model="gpt-4o-mini")
    event = _sample_event("retry_test")

    # First response: bad JSON. Second: good JSON.
    provider._call_api = MagicMock(
        side_effect=["this is not json", _valid_llm_json()]
    )

    result = provider.classify(event, prompt="test")
    assert result.catalyst_type == "earnings"
    assert result.provider == "openai"
    assert provider._call_api.call_count == 2


def test_invalid_json_both_calls_gives_error_record():
    """Both API calls return invalid JSON → error record, NOT mock output."""
    from missed.catalyst_providers import OpenAICatalystProvider

    provider = OpenAICatalystProvider(api_key="sk-test", model="gpt-4o-mini")
    event = _sample_event("double_fail")

    provider._call_api = MagicMock(return_value="not json at all")

    result = provider.classify(event, prompt="test")
    assert "[ERROR]" in result.reasoning_short, \
        "Failed classification must produce error record with [ERROR] in reasoning_short"
    assert result.catalyst_type == "unknown"
    assert result.confidence == 0.0
    # Must NOT be mock output (mock uses form-type based catalyst)
    assert result.provider != "mock", "Error record must not be silently substituted with mock"


def test_schema_invalid_response_gives_error_record():
    """API returns JSON that fails validate_enrichment → error record."""
    from missed.catalyst_providers import OpenAICatalystProvider

    provider = OpenAICatalystProvider(api_key="sk-test", model="gpt-4o-mini")
    event = _sample_event("schema_fail")

    # Valid JSON but confidence=2.0 (out of range) on both calls
    bad_response = _valid_llm_json(confidence=2.0)
    provider._call_api = MagicMock(return_value=bad_response)

    result = provider.classify(event, prompt="test")
    assert "[ERROR]" in result.reasoning_short
    assert result.confidence == 0.0


def test_error_record_passes_schema_validation():
    """The error record itself must pass validate_enrichment (all fields valid)."""
    from missed.catalyst_providers import OpenAICatalystProvider
    from missed.catalyst_schema import validate_enrichment

    provider = OpenAICatalystProvider(api_key="sk-test", model="gpt-4o-mini")
    event = _sample_event("val_test")
    provider._call_api = MagicMock(return_value="garbage")

    result = provider.classify(event, prompt="test")
    is_valid, errors = validate_enrichment(result.to_dict())
    assert is_valid, f"Error record failed validation: {errors}"


# ═════════════════════════════════════════════════════════════════════════════
# PART 4 — Report generation
# ═════════════════════════════════════════════════════════════════════════════

def test_report_importable():
    from missed.catalyst_report import generate_pilot_report  # noqa: F401


def _make_enrichment(ticker, catalyst_type, materiality, sentiment, confidence,
                      should_affect_score=False, provider="mock", event_id=None):
    from missed.catalyst_schema import CatalystEnrichment
    return CatalystEnrichment(
        event_id=event_id or uuid.uuid4().hex[:16],
        ticker=ticker, event_date="2026-03-15",
        catalyst_type=catalyst_type, materiality=materiality,
        sentiment=sentiment, confidence=confidence,
        evidence_quote="Test quote.", reasoning_short="Test reasoning.",
        should_affect_score=should_affect_score,
        provider=provider, enriched_at="2026-05-02T10:00:00+00:00",
    )


def test_report_generates_markdown_file(tmp_path):
    from missed.catalyst_report import generate_pilot_report
    sample = [_sample_event(event_id=f"e{i}", ticker=f"T{i}") for i in range(5)]
    enriched = [_make_enrichment(f"T{i}", "earnings", "high", "bullish", 0.8,
                                  should_affect_score=True, event_id=f"e{i}") for i in range(5)]
    md_path, csv_path = generate_pilot_report(sample, enriched, str(tmp_path))
    assert md_path.endswith(".md")
    assert tmp_path.joinpath("catalyst_llm_pilot_report.md").exists()


def test_report_markdown_contains_required_sections(tmp_path):
    from missed.catalyst_report import generate_pilot_report
    sample = [_sample_event(event_id=f"e{i}", ticker=f"T{i}") for i in range(3)]
    enriched = [
        _make_enrichment("T0", "earnings", "high", "bullish", 0.9, True, event_id="e0"),
        _make_enrichment("T1", "unknown", "none", "neutral", 0.3, False, event_id="e1"),
        _make_enrichment("T2", "guidance", "medium", "bearish", 0.7, True, event_id="e2"),
    ]
    md_path, _ = generate_pilot_report(sample, enriched, str(tmp_path))
    content = open(md_path).read()
    for section in ("Catalyst Type", "Materiality", "Confidence", "Final Score Impact"):
        assert section in content, f"Report missing section: {section}"


def test_report_generates_csv_file(tmp_path):
    from missed.catalyst_report import generate_pilot_report
    sample = [_sample_event(event_id="e1", ticker="AAPL")]
    enriched = [_make_enrichment("AAPL", "earnings", "high", "bullish", 0.85,
                                  should_affect_score=True, event_id="e1")]
    _, csv_path = generate_pilot_report(sample, enriched, str(tmp_path))
    assert csv_path.endswith(".csv")
    assert tmp_path.joinpath("catalyst_llm_pilot_review.csv").exists()


def test_report_csv_has_required_columns(tmp_path):
    from missed.catalyst_report import generate_pilot_report
    sample = [_sample_event(event_id="e1", ticker="AAPL")]
    enriched = [_make_enrichment("AAPL", "earnings", "high", "bullish", 0.85,
                                  should_affect_score=True, event_id="e1")]
    _, csv_path = generate_pilot_report(sample, enriched, str(tmp_path))
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
    for col in ("ticker", "event_date", "event_type", "original_root_cause",
                "original_form_type", "catalyst_type", "materiality", "sentiment",
                "confidence", "model_should_affect_score", "final_should_affect_score",
                "validation_status", "quote_validation_pass", "invalid_reason",
                "evidence_quote", "reasoning_short"):
        assert col in cols, f"CSV missing column: {col}"


def test_report_counts_unknown_before_classified_after(tmp_path):
    """Report mentions conversion rate from unknown form to classified catalyst."""
    from missed.catalyst_report import generate_pilot_report
    # 2 events with unknown form type that got classified as earnings/guidance
    sample = [
        _sample_event(event_id="e1", ticker="A", form=None),
        _sample_event(event_id="e2", ticker="B", form=None),
    ]
    enriched = [
        _make_enrichment("A", "earnings", "high", "bullish", 0.8, event_id="e1"),
        _make_enrichment("B", "guidance", "medium", "bullish", 0.7, event_id="e2"),
    ]
    md_path, _ = generate_pilot_report(sample, enriched, str(tmp_path))
    content = open(md_path).read()
    assert "unknown" in content.lower() or "conversion" in content.lower() or \
           "classified" in content.lower(), "Report should mention unknown-to-classified conversion"


def test_report_includes_high_materiality_bullish_section(tmp_path):
    from missed.catalyst_report import generate_pilot_report
    sample = [_sample_event(event_id=f"e{i}", ticker=f"T{i}") for i in range(3)]
    enriched = [
        _make_enrichment("T0", "earnings", "high", "bullish", 0.9, True, event_id="e0"),
        _make_enrichment("T1", "merger_acquisition", "high", "bullish", 0.85, True, event_id="e1"),
        _make_enrichment("T2", "guidance", "medium", "bearish", 0.7, True, event_id="e2"),
    ]
    md_path, _ = generate_pilot_report(sample, enriched, str(tmp_path))
    content = open(md_path).read()
    assert "High Materiality" in content or "high" in content.lower()
    assert "T0" in content or "T1" in content  # bullish high-materiality tickers appear


def test_report_includes_error_records_section(tmp_path):
    from missed.catalyst_report import generate_pilot_report
    sample = [_sample_event(event_id="e1", ticker="ERR")]
    enriched = [_make_enrichment("ERR", "unknown", "none", "neutral", 0.0,
                                  provider="openai_error",
                                  event_id="e1")]
    # Force error record flag
    enriched[0].reasoning_short = "[ERROR] JSON parse failure"
    md_path, _ = generate_pilot_report(sample, enriched, str(tmp_path))
    content = open(md_path).read()
    assert "error" in content.lower() or "failed" in content.lower() or "ERR" in content


# ═════════════════════════════════════════════════════════════════════════════
# PART 5 — CLI options
# ═════════════════════════════════════════════════════════════════════════════

def test_pilot_cli_has_provider_option():
    """--provider option exists in 'missed pilot --help'."""
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "pilot", "--help"])
    assert "--provider" in result.output, f"--provider missing from help: {result.output}"


def test_pilot_cli_has_model_option():
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "pilot", "--help"])
    assert "--model" in result.output, f"--model missing from help: {result.output}"


def test_pilot_cli_has_cache_path_option():
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "pilot", "--help"])
    assert "cache" in result.output.lower(), f"cache option missing from help: {result.output}"


def test_pilot_cli_has_report_flag():
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "pilot", "--help"])
    assert "--report" in result.output, f"--report missing from help: {result.output}"


# ═════════════════════════════════════════════════════════════════════════════
# PART 6 — Regression guards
# ═════════════════════════════════════════════════════════════════════════════

def test_mock_path_still_deterministic_after_refactor():
    """Mock classify_events still returns same results for same input (regression guard)."""
    from missed.catalyst_classifier import classify_events
    events = [_sample_event("stable_id_xyz")]
    r1 = classify_events(events, use_mock=True, cache_path=None)
    r2 = classify_events(events, use_mock=True, cache_path=None)
    assert r1[0].catalyst_type == r2[0].catalyst_type
    assert r1[0].confidence == r2[0].confidence


def test_running_classify_does_not_modify_scores_table(conn):
    """classify_events does not touch the scores table."""
    from missed.catalyst_classifier import classify_events
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, cik, company_name) VALUES (?,?,?)",
        ["SAFE2", "0000000001", "Safe Corp 2"],
    )
    conn.execute(
        "INSERT INTO scores (id, run_id, ticker, as_of_date, total_score, tier)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [uuid.uuid4().hex[:16], "run1", "SAFE2", "2026-01-01", 42.0, "Reject"],
    )
    original = conn.execute(
        "SELECT total_score FROM scores WHERE ticker='SAFE2'"
    ).fetchone()[0]

    classify_events([_sample_event(ticker="SAFE2")], use_mock=True, cache_path=None)

    after = conn.execute(
        "SELECT total_score FROM scores WHERE ticker='SAFE2'"
    ).fetchone()[0]
    assert original == after
