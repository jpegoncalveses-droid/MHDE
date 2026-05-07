import json
import pytest
import responses as rsps_lib
from adapters.sec_edgar import SECEdgarAdapter

SUBMISSIONS_AAPL = {
    "cik": "320193",
    "entityType": "operating",
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "form": ["10-K", "10-Q", "8-K", "8-K"],
            "accessionNumber": ["0000320193-24-000123", "0000320193-24-000100", "0000320193-24-000080", "0000320193-24-000060"],
            "filingDate": ["2024-11-01", "2024-08-02", "2024-05-02", "2024-03-01"],
            "primaryDocument": ["aapl-20240928.htm", "aapl-20240629.htm", "aapl-20240330.htm", "8k.htm"],
        }
    },
}

COMPANYFACTS_AAPL = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "NetIncomeLoss": {
                "label": "Net Income (Loss)",
                "units": {"USD": [
                    {"end": "2024-09-28", "val": 93736000000, "form": "10-K", "filed": "2024-11-01"},
                    {"end": "2023-09-30", "val": 96995000000, "form": "10-K", "filed": "2023-11-03"},
                ]}
            },
            "Revenues": {
                "label": "Revenues",
                "units": {"USD": [
                    {"end": "2024-09-28", "val": 391035000000, "form": "10-K", "filed": "2024-11-01"},
                ]}
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Shares Outstanding",
                "units": {"shares": [{"end": "2024-09-28", "val": 15204137000, "form": "10-K"}]}
            }
        }
    }
}


@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://data.sec.gov/submissions/CIK0000320193.json",
                 json=SUBMISSIONS_AAPL, status=200)
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"
    assert err is None


@rsps_lib.activate
def test_test_access_fails_on_non_200(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://data.sec.gov/submissions/CIK0000320193.json",
                 status=503)
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "error"
    assert err is not None


@rsps_lib.activate
def test_fetch_filings_returns_dict_keyed_by_ticker(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://data.sec.gov/submissions/CIK0000320193.json",
                 json=SUBMISSIONS_AAPL, status=200)
    rsps_lib.add(rsps_lib.GET,
                 "https://data.sec.gov/submissions/CIK0001045810.json",
                 json=SUBMISSIONS_AAPL, status=200)
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "filings")
    assert "AAPL" in data
    assert "IWM" not in data   # ETF without CIK skipped


@rsps_lib.activate
def test_validate_schema_filings_passes(minimal_settings, sample_tickers):
    data = {"AAPL": SUBMISSIONS_AAPL}
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "filings")
    assert ok is True
    assert missing == []


def test_validate_schema_filings_detects_missing(minimal_settings, sample_tickers):
    bad = {"AAPL": {"cik": "320193"}}   # no filings key
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(bad, "filings")
    assert ok is False
    assert "filings.recent.form" in missing


@rsps_lib.activate
def test_fetch_fundamentals_returns_data(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
                 json=COMPANYFACTS_AAPL, status=200)
    rsps_lib.add(rsps_lib.GET,
                 "https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json",
                 json=COMPANYFACTS_AAPL, status=200)
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "fundamentals")
    assert "AAPL" in data
    assert "facts" in data["AAPL"]


def test_evaluate_freshness_recent(minimal_settings, sample_tickers):
    data = {"AAPL": SUBMISSIONS_AAPL}
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    freshness = adapter.evaluate_freshness(data, "filings")
    assert freshness in ("1d", "1w", ">1mo", "same-day", "N/A")


def test_evaluate_history_fundamentals(minimal_settings, sample_tickers):
    data = {"AAPL": COMPANYFACTS_AAPL}
    adapter = SECEdgarAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    depth = adapter.evaluate_history(data, "fundamentals")
    assert "y" in depth or depth == "N/A"
