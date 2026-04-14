import json
import pytest
from dataclasses import asdict
from adapters.base import Scores, ValidationResult, BaseAdapter
from typing import Any, Optional


class ConcreteAdapter(BaseAdapter):
    source_name = "test_source"
    use_cases = ["use_a"]

    def test_access(self):
        return "ok", None

    def fetch_sample_data(self, tickers, use_case):
        return {"data": "sample"}

    def validate_schema(self, data, use_case):
        return True, []

    def evaluate_freshness(self, data, use_case):
        return "same-day"

    def evaluate_history(self, data, use_case):
        return "5y"

    def summarize_result(self, data, use_case, access_result):
        scores = Scores(5, 5, 5, 5, 5, 5, 5)
        return ValidationResult(
            source=self.source_name,
            use_case=use_case,
            tickers_tested=["AAPL"],
            access_result=access_result,
            access_error=None,
            required_fields_present=True,
            missing_fields=[],
            historical_depth="5y",
            freshness="same-day",
            parsing_difficulty="easy",
            rate_limit_notes="none",
            fallback_suggestion="none",
            final_status="Core",
            notes="test",
            scores=scores,
            raw_sample_path=None,
        )


def test_scores_total():
    s = Scores(access=4, completeness=3, freshness=5, reliability=4,
               parsing_ease=5, cost_efficiency=5, strategic_value=5)
    assert s.total() == 31


def test_scores_to_dict_includes_total():
    s = Scores(4, 3, 5, 4, 5, 5, 5)
    d = s.to_dict()
    assert d["total"] == 31
    assert d["access"] == 4


def test_validation_result_to_dict():
    scores = Scores(5, 5, 5, 5, 5, 5, 5)
    vr = ValidationResult(
        source="sec_edgar", use_case="filings", tickers_tested=["AAPL"],
        access_result="ok", access_error=None, required_fields_present=True,
        missing_fields=[], historical_depth="5y", freshness="1d",
        parsing_difficulty="easy", rate_limit_notes="none",
        fallback_suggestion="none", final_status="Core", notes="ok",
        scores=scores, raw_sample_path=None,
    )
    d = vr.to_dict()
    assert d["source"] == "sec_edgar"
    assert d["scores"]["total"] == 35
    # must be JSON-serializable
    json.dumps(d)


def test_base_adapter_run_calls_subclass(tmp_path, monkeypatch):
    settings = {"outputs": {"samples_dir": str(tmp_path)}}
    adapter = ConcreteAdapter(settings=settings, tickers_config=[{"ticker": "AAPL"}])
    results = adapter.run([{"ticker": "AAPL"}])
    assert len(results) == 1
    assert results[0].source == "test_source"
    assert results[0].access_result == "ok"


def test_base_adapter_skips_fetch_on_access_failure(tmp_path):
    class FailAdapter(ConcreteAdapter):
        def test_access(self):
            return "auth_fail", "bad key"

        def fetch_sample_data(self, tickers, use_case):
            raise AssertionError("should not be called")

    settings = {"outputs": {"samples_dir": str(tmp_path)}}
    adapter = FailAdapter(settings=settings, tickers_config=[])
    results = adapter.run([{"ticker": "AAPL"}])
    assert results[0].access_result == "auth_fail"
    assert results[0].access_error == "bad key"
