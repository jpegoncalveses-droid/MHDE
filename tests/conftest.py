import pytest
from adapters.base import Scores, ValidationResult


@pytest.fixture
def minimal_settings(tmp_path):
    return {
        "outputs": {"samples_dir": str(tmp_path / "samples"), "dir": str(tmp_path / "outputs")},
        "http": {"timeout": 10, "retries": 1, "retry_delay": 0},
        "sec_edgar": {"base_url": "https://data.sec.gov", "user_agent": "test", "rate_limit_delay": 0},
        "polygon": {"base_url": "https://api.polygon.io", "api_key": "fake_key", "rate_limit_delay": 0},
        "alpha_vantage": {"base_url": "https://www.alphavantage.co", "api_key": "fake_key", "rate_limit_delay": 0},
        "company_ir": {"request_delay": 0, "request_timeout": 5},
        "nasdaq_earnings": {"base_url": "https://api.nasdaq.com", "rate_limit_delay": 0},
        "fred": {"base_url": "https://api.stlouisfed.org/fred", "api_key": "fake_key", "rate_limit_delay": 0},
        "finra": {"base_url": "https://cdn.finra.org/equity/otcmarket/biweekly", "rate_limit_delay": 0},
    }


@pytest.fixture
def sample_tickers():
    return [
        {"ticker": "AAPL", "name": "Apple Inc", "cik": "0000320193",
         "ir_press_url": "https://investor.apple.com/press-releases/default.aspx",
         "ir_events_url": "https://investor.apple.com/events/default.aspx", "type": "stock"},
        {"ticker": "NVDA", "name": "NVIDIA Corporation", "cik": "0001045810",
         "ir_press_url": "https://nvidianews.nvidia.com/releases",
         "ir_events_url": "https://investor.nvidia.com/events-presentations/events/default.aspx", "type": "stock"},
        {"ticker": "IWM", "name": "iShares Russell 2000 ETF", "cik": None,
         "ir_press_url": None, "ir_events_url": None, "type": "etf"},
    ]


def make_validation_result(source="test", use_case="uc", final_status="Core", **kwargs):
    defaults = dict(
        source=source, use_case=use_case, tickers_tested=["AAPL"],
        access_result="ok", access_error=None, required_fields_present=True,
        missing_fields=[], historical_depth="5y", freshness="1d",
        parsing_difficulty="easy", rate_limit_notes="none",
        fallback_suggestion="none", final_status=final_status,
        notes="", scores=Scores(5, 5, 5, 5, 5, 5, 5), raw_sample_path=None,
    )
    defaults.update(kwargs)
    return ValidationResult(**defaults)
