import re
import pytest
import responses as rsps_lib
from adapters.polygon import PolygonAdapter, _PLAN_LIMITED

AGGS_RESPONSE = {
    "ticker": "AAPL",
    "resultsCount": 3,
    "results": [
        {"t": 1704067200000, "o": 185.0, "h": 186.0, "l": 183.0, "c": 185.5, "v": 70000000},
        {"t": 1704153600000, "o": 185.5, "h": 187.0, "l": 184.0, "c": 186.0, "v": 65000000},
        {"t": 1704240000000, "o": 186.0, "h": 188.0, "l": 185.0, "c": 187.5, "v": 72000000},
    ],
    "status": "OK",
}

SNAPSHOT_RESPONSE = {
    "ticker": {
        "ticker": "AAPL",
        "day": {"o": 185.0, "h": 188.0, "l": 184.0, "c": 187.5, "v": 70000000},
        "prevDay": {"c": 185.5},
        "lastTrade": {"p": 187.5},
        "todaysChangePerc": 1.08,
    },
    "status": "OK",
}

_AGGS_RE = re.compile(r"https://api\.polygon\.io/v2/aggs/ticker/AAPL/range/1/day/")


@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET, _AGGS_RE, json=AGGS_RESPONSE, status=200, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"


@rsps_lib.activate
def test_test_access_auth_fail(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET, _AGGS_RE, json={"status": "AUTH_ERROR"}, status=401, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "auth_fail"


@rsps_lib.activate
def test_test_access_plan_limited(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET, _AGGS_RE, json={"status": "NOT_AUTHORIZED"}, status=403, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "plan_limited"


@rsps_lib.activate
def test_fetch_recent_daily_prices_returns_dict(minimal_settings, sample_tickers):
    for ticker in ["AAPL", "NVDA"]:
        rsps_lib.add(rsps_lib.GET,
                     re.compile(rf"https://api\.polygon\.io/v2/aggs/ticker/{ticker}/range/1/day/"),
                     json={**AGGS_RESPONSE, "ticker": ticker}, status=200)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers[:2], "recent_daily_prices")
    assert "AAPL" in data
    assert len(data["AAPL"]["results"]) == 3


@rsps_lib.activate
def test_fetch_deep_historical_plan_limited(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 re.compile(r"https://api\.polygon\.io/v2/aggs/ticker/AAPL/range/1/day/"),
                 json={"status": "NOT_AUTHORIZED"}, status=403)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data([sample_tickers[0]], "deep_historical_prices")
    assert data is _PLAN_LIMITED


@rsps_lib.activate
def test_fetch_snapshot_returns_dict(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/AAPL",
                 json=SNAPSHOT_RESPONSE, status=200, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data([sample_tickers[0]], "recent_snapshot")
    assert "AAPL" in data
    assert data["AAPL"]["ticker"]["day"]["c"] == 187.5


def test_validate_schema_recent_daily_passes(minimal_settings, sample_tickers):
    data = {"AAPL": AGGS_RESPONSE}
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "recent_daily_prices")
    assert ok is True
    assert missing == []


def test_validate_schema_snapshot_detects_missing(minimal_settings, sample_tickers):
    data = {"AAPL": {"ticker": {"day": {"o": 1}}, "status": "OK"}}  # missing c, v
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "recent_snapshot")
    assert ok is False


def test_evaluate_history_deep_historical(minimal_settings, sample_tickers):
    data = {"AAPL": AGGS_RESPONSE}
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_history(data, "deep_historical_prices") == "5y"


def test_evaluate_history_recent_daily(minimal_settings, sample_tickers):
    data = {"AAPL": AGGS_RESPONSE}
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_history(data, "recent_daily_prices") == "5d"


def test_summarize_result_plan_limited_rejects(minimal_settings, sample_tickers):
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(_PLAN_LIMITED, "deep_historical_prices", "ok")
    assert result.access_result == "plan_limited"
    assert result.final_status == "Reject for v1"
