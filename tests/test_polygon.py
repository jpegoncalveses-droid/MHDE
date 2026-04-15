import pytest
import responses as rsps_lib
from adapters.polygon import PolygonAdapter

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


@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/day/2024-01-02/2024-01-05",
                 json=AGGS_RESPONSE, status=200, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"


@rsps_lib.activate
def test_test_access_auth_fail(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/day/2024-01-02/2024-01-05",
                 json={"status": "AUTH_ERROR"}, status=403, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "auth_fail"


@rsps_lib.activate
def test_fetch_historical_returns_dict(minimal_settings, sample_tickers):
    for ticker in ["AAPL", "NVDA", "IWM"]:
        rsps_lib.add(rsps_lib.GET,
                     f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/2020-01-01/2026-04-15",
                     json={**AGGS_RESPONSE, "ticker": ticker}, status=200, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers[:2], "historical_prices")
    assert "AAPL" in data
    assert len(data["AAPL"]["results"]) == 3


@rsps_lib.activate
def test_fetch_snapshot_returns_dict(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/AAPL",
                 json=SNAPSHOT_RESPONSE, status=200, match_querystring=False)
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data([sample_tickers[0]], "recent_snapshot")
    assert "AAPL" in data
    assert data["AAPL"]["ticker"]["day"]["c"] == 187.5


def test_validate_schema_historical_passes(minimal_settings, sample_tickers):
    data = {"AAPL": AGGS_RESPONSE}
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "historical_prices")
    assert ok is True
    assert missing == []


def test_validate_schema_snapshot_detects_missing(minimal_settings, sample_tickers):
    data = {"AAPL": {"ticker": {"day": {"o": 1}}, "status": "OK"}}  # missing c, v
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "recent_snapshot")
    assert ok is False


def test_evaluate_history_five_years(minimal_settings, sample_tickers):
    data = {"AAPL": AGGS_RESPONSE}
    adapter = PolygonAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_history(data, "historical_prices") == "5y"
