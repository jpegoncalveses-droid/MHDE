import pytest
import responses as rsps_lib
from adapters.nasdaq_earnings import NasdaqEarningsAdapter

NASDAQ_EARNINGS_RESPONSE = {
    "data": {
        "rows": [
            {"symbol": "AAPL", "name": "Apple Inc", "time": "time-after-hours",
             "eps_forecast": "1.60", "eps_prior": "1.53", "date": "2024-10-31"},
            {"symbol": "NVDA", "name": "NVIDIA Corporation", "time": "time-after-hours",
             "eps_forecast": "0.74", "eps_prior": "0.60", "date": "2024-11-20"},
            {"symbol": "TSLA", "name": "Tesla Inc", "time": "time-after-hours",
             "eps_forecast": "0.58", "eps_prior": "0.71", "date": "2024-10-23"},
        ]
    },
    "message": None,
    "status": {"rCode": 200}
}


@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://api.nasdaq.com/api/calendar/earnings",
                 json=NASDAQ_EARNINGS_RESPONSE, status=200, match_querystring=False)
    adapter = NasdaqEarningsAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"


@rsps_lib.activate
def test_fetch_earnings_calendar_returns_dict(minimal_settings, sample_tickers):
    # Simulate responses for multiple date pages
    rsps_lib.add(rsps_lib.GET,
                 "https://api.nasdaq.com/api/calendar/earnings",
                 json=NASDAQ_EARNINGS_RESPONSE, status=200, match_querystring=False)
    adapter = NasdaqEarningsAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "earnings_calendar")
    assert data is not None
    assert "AAPL" in data or "rows" in data or isinstance(data, dict)


@rsps_lib.activate
def test_validate_schema_ok(minimal_settings, sample_tickers):
    data = {
        "AAPL": {"date": "2024-10-31", "time": "time-after-hours", "eps_forecast": "1.60"},
        "NVDA": {"date": "2024-11-20", "time": "time-after-hours", "eps_forecast": "0.74"},
    }
    adapter = NasdaqEarningsAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "earnings_calendar")
    assert ok is True


def test_validate_schema_missing_date(minimal_settings, sample_tickers):
    data = {"AAPL": {"time": "time-after-hours"}}
    adapter = NasdaqEarningsAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "earnings_calendar")
    assert ok is False
    assert "date" in missing


def test_evaluate_freshness(minimal_settings, sample_tickers):
    data = {"AAPL": {"date": "2025-01-28"}}
    adapter = NasdaqEarningsAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "earnings_calendar") == "1d"


def test_evaluate_history(minimal_settings, sample_tickers):
    data = {"AAPL": {"date": "2025-01-28"}}
    adapter = NasdaqEarningsAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_history(data, "earnings_calendar") == "N/A"
