import pytest
from unittest.mock import MagicMock, patch
from runner.runner import ValidationRunner
from adapters.base import Scores, ValidationResult
from tests.conftest import make_validation_result


def _mock_adapter(source_name, results):
    adapter = MagicMock()
    adapter.source_name = source_name
    adapter.run.return_value = results
    return adapter


def test_runner_collects_results_from_all_adapters(minimal_settings, sample_tickers):
    r1 = make_validation_result("sec_edgar", "filings")
    r2 = make_validation_result("polygon", "historical_prices")
    mock_adapters = [
        _mock_adapter("sec_edgar", [r1]),
        _mock_adapter("polygon", [r2]),
    ]
    runner = ValidationRunner(
        settings=minimal_settings,
        tickers=sample_tickers,
        adapters=mock_adapters,
    )
    results = runner.run()
    assert len(results) == 2
    assert results[0].source == "sec_edgar"
    assert results[1].source == "polygon"


def test_runner_continues_if_adapter_raises(minimal_settings, sample_tickers):
    failing_adapter = MagicMock()
    failing_adapter.source_name = "broken"
    failing_adapter.run.side_effect = RuntimeError("adapter exploded")
    ok_result = make_validation_result("polygon", "historical_prices")
    ok_adapter = _mock_adapter("polygon", [ok_result])

    runner = ValidationRunner(
        settings=minimal_settings,
        tickers=sample_tickers,
        adapters=[failing_adapter, ok_adapter],
    )
    results = runner.run()
    assert len(results) == 1
    assert results[0].source == "polygon"


def test_runner_can_filter_by_source(minimal_settings, sample_tickers):
    r1 = make_validation_result("sec_edgar", "filings")
    r2 = make_validation_result("polygon", "historical_prices")
    runner = ValidationRunner(
        settings=minimal_settings,
        tickers=sample_tickers,
        adapters=[_mock_adapter("sec_edgar", [r1]), _mock_adapter("polygon", [r2])],
        source_filter=["sec_edgar"],
    )
    results = runner.run()
    assert len(results) == 1
    assert results[0].source == "sec_edgar"
