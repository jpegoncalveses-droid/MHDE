from __future__ import annotations

import pytest

from universe.filters import classify_company, filter_non_equities


def make_company(ticker, name):
    return {"ticker": ticker, "cik": "0001234567", "company_name": name}


def test_classify_etf():
    c = classify_company(make_company("SPY", "SPDR S&P 500 ETF Trust"))
    assert c["is_etf"] is True


def test_classify_fund():
    c = classify_company(make_company("VFIAX", "Vanguard 500 Index Fund"))
    assert c["is_fund"] is True


def test_classify_normal():
    c = classify_company(make_company("AAPL", "Apple Inc"))
    assert c["is_etf"] is False
    assert c["is_fund"] is False


def test_filter_excludes_etf():
    companies = [
        make_company("AAPL", "Apple Inc"),
        make_company("SPY", "SPDR S&P 500 ETF Trust"),
    ]
    result = filter_non_equities(companies, {})
    tickers = [c["ticker"] for c in result]
    assert "AAPL" in tickers
    assert "SPY" not in tickers


def test_filter_excludes_dotted_tickers():
    companies = [make_company("BRK.B", "Berkshire B")]
    result = filter_non_equities(companies, {})
    assert len(result) == 0


def test_filter_excludes_long_tickers():
    companies = [make_company("TOOLONG", "Some Corp")]
    result = filter_non_equities(companies, {})
    assert len(result) == 0


def test_filter_keeps_normal():
    companies = [make_company("NVDA", "NVIDIA Corporation")]
    result = filter_non_equities(companies, {})
    assert len(result) == 1


def test_filter_excludes_note_due():
    companies = [make_company("XYZ", "ABC Corp Note Due 2028")]
    result = filter_non_equities(companies, {})
    assert len(result) == 0
