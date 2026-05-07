import pytest
import responses as rsps_lib
from adapters.alpha_vantage import AlphaVantageAdapter

TRANSCRIPT_RESPONSE = {
    "symbol": "AAPL",
    "quarter": "2024Q3",
    "transcript": [
        {"speaker": "CEO", "speech": "Good afternoon. We had a strong quarter..."},
        {"speaker": "CFO", "speech": "Revenue was $94.9 billion..."},
    ]
}

EARNINGS_RESPONSE = {
    "symbol": "AAPL",
    "annualEarnings": [
        {"fiscalDateEnding": "2024-09-28", "reportedEPS": "6.08"},
        {"fiscalDateEnding": "2023-09-30", "reportedEPS": "6.13"},
    ],
    "quarterlyEarnings": [
        {"fiscalDateEnding": "2024-09-28", "reportedDate": "2024-10-31",
         "reportedEPS": "1.64", "estimatedEPS": "1.60", "surprise": "0.04"},
    ]
}

RATE_LIMIT_RESPONSE = {
    "Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."
}


@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://www.alphavantage.co/query",
                 json={"bestMatches": [{"1. symbol": "AAPL"}]}, status=200,
                 match_querystring=False)
    adapter = AlphaVantageAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"


@rsps_lib.activate
def test_test_access_rate_limited(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://www.alphavantage.co/query",
                 json=RATE_LIMIT_RESPONSE, status=200, match_querystring=False)
    adapter = AlphaVantageAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "rate_limited"


@rsps_lib.activate
def test_fetch_transcripts_returns_dict(minimal_settings, sample_tickers):
    for ticker in ["AAPL", "NVDA"]:
        for quarter in ["2024Q3", "2024Q2"]:
            rsps_lib.add(rsps_lib.GET,
                         "https://www.alphavantage.co/query",
                         json={**TRANSCRIPT_RESPONSE, "symbol": ticker, "quarter": quarter},
                         status=200, match_querystring=False)
    adapter = AlphaVantageAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers[:2], "transcripts")
    assert "AAPL" in data
    assert len(data["AAPL"]) == 2   # 2 quarters


@rsps_lib.activate
def test_fetch_estimates_returns_dict(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://www.alphavantage.co/query",
                 json=EARNINGS_RESPONSE, status=200, match_querystring=False)
    rsps_lib.add(rsps_lib.GET,
                 "https://www.alphavantage.co/query",
                 json={**EARNINGS_RESPONSE, "symbol": "NVDA"}, status=200, match_querystring=False)
    adapter = AlphaVantageAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers[:2], "estimates")
    assert "AAPL" in data


def test_validate_schema_transcripts_ok(minimal_settings, sample_tickers):
    data = {"AAPL": [TRANSCRIPT_RESPONSE]}
    adapter = AlphaVantageAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "transcripts")
    assert ok is True


def test_validate_schema_estimates_ok(minimal_settings, sample_tickers):
    data = {"AAPL": EARNINGS_RESPONSE}
    adapter = AlphaVantageAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "estimates")
    assert ok is True


def test_validate_schema_estimates_missing_estimated_eps(minimal_settings, sample_tickers):
    bad = {"AAPL": {**EARNINGS_RESPONSE,
                    "quarterlyEarnings": [{"fiscalDateEnding": "2024-09-28", "reportedEPS": "1.64"}]}}
    adapter = AlphaVantageAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(bad, "estimates")
    assert ok is False
    assert "quarterlyEarnings[].estimatedEPS" in missing
