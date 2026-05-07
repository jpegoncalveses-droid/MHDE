import re
import pytest
import responses as rsps_lib
from datetime import date, timedelta

from adapters.cftc import CFTCAdapter

_TFF_BASE = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_DISAG_BASE = "https://publicreporting.cftc.gov/resource/kh3c-gbw2.json"
_TFF_RE = re.compile(re.escape(_TFF_BASE))
_DISAG_RE = re.compile(re.escape(_DISAG_BASE))

_RECENT = (date.today() - timedelta(days=7)).isoformat()
_LATE = (date.today() - timedelta(days=14)).isoformat()
_STALE = (date.today() - timedelta(days=21)).isoformat()

# Canonical CFTC market names (substrings the adapter must recognise)
_ES = "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE"
_NQ = "NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE"
_RTY = "RUSSELL E-MINI - CHICAGO MERCANTILE EXCHANGE"
_WTI = "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE"
_GOLD = "GOLD - COMMODITY EXCHANGE INC."


# ── row builders ──────────────────────────────────────────────────────────────

def _tff_row(market_name, report_date=_RECENT):
    return {
        "market_and_exchange_names": market_name,
        "report_date_as_yyyy_mm_dd": report_date,
        "dealer_positions_long_all": "500000",
        "dealer_positions_short_all": "600000",
        "asset_mgr_positions_long_all": "800000",
        "asset_mgr_positions_short_all": "700000",
        "lev_money_positions_long_all": "400000",
        "lev_money_positions_short_all": "500000",
        "other_rept_positions_long_all": "100000",
        "other_rept_positions_short_all": "90000",
        "nonrept_positions_long_all": "50000",
        "nonrept_positions_short_all": "60000",
        "open_interest_all": "1850000",
    }


def _disag_row(market_name, report_date=_RECENT):
    return {
        "market_and_exchange_names": market_name,
        "report_date_as_yyyy_mm_dd": report_date,
        "prod_merc_positions_long_all": "200000",
        "prod_merc_positions_short_all": "300000",
        "swap_positions_long_all": "150000",
        "swap_positions_short_all": "100000",
        "m_money_positions_long_all": "400000",
        "m_money_positions_short_all": "350000",
        "other_rept_positions_long_all": "80000",
        "other_rept_positions_short_all": "70000",
        "nonrept_positions_long_all": "50000",
        "nonrept_positions_short_all": "60000",
        "open_interest_all": "980000",
    }


def _full_index(report_date=_RECENT):
    return [_tff_row(m, report_date) for m in [_ES, _NQ, _RTY]]


def _full_commodity(report_date=_RECENT):
    return [_disag_row(m, report_date) for m in [_WTI, _GOLD]]


def _make_record(market_key, market_name, report_date=_RECENT):
    return {
        "market_name": market_name,
        "market_key": market_key,
        "report_date": report_date,
        "open_interest": 1850000,
        "categories": {
            "dealer": {"long": 500000, "short": 600000, "net": -100000},
            "asset_manager": {"long": 800000, "short": 700000, "net": 100000},
            "leveraged_funds": {"long": 400000, "short": 500000, "net": -100000},
            "other_reportable": {"long": 100000, "short": 90000, "net": 10000},
            "nonreportable": {"long": 50000, "short": 60000, "net": -10000},
        },
    }


def _register_tff(rows, status=200):
    rsps_lib.add(rsps_lib.GET, _TFF_RE, json=rows, status=status, match_querystring=False)


def _register_disag(rows, status=200):
    rsps_lib.add(rsps_lib.GET, _DISAG_RE, json=rows, status=status, match_querystring=False)


# ── test_access ───────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    _register_tff([_tff_row(_ES)])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"
    assert err is None


@rsps_lib.activate
def test_test_access_server_error(minimal_settings, sample_tickers):
    _register_tff([], status=503)
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "error"
    assert err is not None


# ── fetch_sample_data – index_positioning ─────────────────────────────────────

@rsps_lib.activate
def test_fetch_index_all_found(minimal_settings, sample_tickers):
    _register_tff(_full_index())
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    assert data is not None
    assert "es_sp500" in data["found_markets"]
    assert "nq_nasdaq100" in data["found_markets"]
    assert "rty_russell2000" in data["found_markets"]
    assert data["missing_markets"] == [] or "ust_10y" in data["missing_markets"]


@rsps_lib.activate
def test_fetch_index_partial(minimal_settings, sample_tickers):
    _register_tff([_tff_row(_ES)])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    assert data is not None
    assert "es_sp500" in data["found_markets"]
    assert "nq_nasdaq100" in data["missing_markets"]
    assert "rty_russell2000" in data["missing_markets"]


@rsps_lib.activate
def test_fetch_index_no_data(minimal_settings, sample_tickers):
    _register_tff([])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    assert data is None


@rsps_lib.activate
def test_fetch_returns_net_positions(minimal_settings, sample_tickers):
    _register_tff([_tff_row(_ES)])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    rec = data["records"][0]
    am = rec["categories"]["asset_manager"]
    assert am["net"] == am["long"] - am["short"]
    lf = rec["categories"]["leveraged_funds"]
    assert lf["net"] == lf["long"] - lf["short"]


@rsps_lib.activate
def test_fetch_returns_report_date(minimal_settings, sample_tickers):
    _register_tff([_tff_row(_ES, _RECENT)])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    assert data["report_date"] == _RECENT


@rsps_lib.activate
def test_fetch_missing_markets_recorded(minimal_settings, sample_tickers):
    _register_tff([_tff_row(_ES)])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    assert "nq_nasdaq100" in data["missing_markets"]
    assert "rty_russell2000" in data["missing_markets"]


@rsps_lib.activate
def test_fetch_multi_week_history(minimal_settings, sample_tickers):
    rows = []
    for weeks_back in range(4):
        rd = (date.today() - timedelta(days=3 + 7 * weeks_back)).isoformat()
        rows.extend(_full_index(rd))
    _register_tff(rows)
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    assert data["weeks_found"] == 4


@rsps_lib.activate
def test_fetch_open_interest_parsed_as_int(minimal_settings, sample_tickers):
    _register_tff([_tff_row(_ES)])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "index_positioning")
    oi = data["records"][0]["open_interest"]
    assert isinstance(oi, int)
    assert oi == 1850000


# ── fetch_sample_data – commodity_macro_positioning ───────────────────────────

@rsps_lib.activate
def test_fetch_commodity_all_found(minimal_settings, sample_tickers):
    _register_disag(_full_commodity())
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "commodity_macro_positioning")
    assert data is not None
    assert "wti_crude" in data["found_markets"]
    assert "gold" in data["found_markets"]


@rsps_lib.activate
def test_fetch_commodity_partial(minimal_settings, sample_tickers):
    _register_disag([_disag_row(_WTI)])
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "commodity_macro_positioning")
    assert "wti_crude" in data["found_markets"]
    assert "gold" in data["missing_markets"]


# ── validate_schema ───────────────────────────────────────────────────────────

def test_validate_schema_passes(minimal_settings, sample_tickers):
    data = {
        "found_markets": ["es_sp500"],
        "missing_markets": ["nq_nasdaq100", "rty_russell2000"],
        "report_date": _RECENT,
        "weeks_found": 1,
        "records": [_make_record("es_sp500", _ES)],
    }
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "index_positioning")
    assert ok is True
    assert missing == []


def test_validate_schema_fails_missing_categories(minimal_settings, sample_tickers):
    data = {
        "found_markets": ["es_sp500"],
        "missing_markets": [],
        "report_date": _RECENT,
        "weeks_found": 1,
        "records": [
            {
                "market_name": _ES,
                "market_key": "es_sp500",
                "report_date": _RECENT,
                "open_interest": 1850000,
                # categories key absent
            }
        ],
    }
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "index_positioning")
    assert ok is False
    assert len(missing) > 0


def test_validate_schema_no_data(minimal_settings, sample_tickers):
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(None, "index_positioning")
    assert ok is False
    assert "no_data" in missing


# ── evaluate_freshness ────────────────────────────────────────────────────────

def test_evaluate_freshness_current(minimal_settings, sample_tickers):
    data = {"report_date": (date.today() - timedelta(days=7)).isoformat()}
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "index_positioning") == "weekly_current"


def test_evaluate_freshness_one_week_late(minimal_settings, sample_tickers):
    data = {"report_date": (date.today() - timedelta(days=14)).isoformat()}
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "index_positioning") == "one_week_late"


def test_evaluate_freshness_stale(minimal_settings, sample_tickers):
    data = {"report_date": (date.today() - timedelta(days=21)).isoformat()}
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    freshness = adapter.evaluate_freshness(data, "index_positioning")
    assert freshness.startswith("stale:")


def test_evaluate_freshness_no_data(minimal_settings, sample_tickers):
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(None, "index_positioning") == "N/A"


# ── evaluate_history ──────────────────────────────────────────────────────────

def test_evaluate_history(minimal_settings, sample_tickers):
    data = {"weeks_found": 4}
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_history(data, "index_positioning") == "4w"


# ── summarize_result ──────────────────────────────────────────────────────────

def test_summarize_result_full_coverage_useful(minimal_settings, sample_tickers):
    data = {
        "found_markets": ["es_sp500", "nq_nasdaq100", "rty_russell2000"],
        "missing_markets": [],
        "report_date": _RECENT,
        "weeks_found": 4,
        "records": [_make_record(k, n) for k, n in [
            ("es_sp500", _ES), ("nq_nasdaq100", _NQ), ("rty_russell2000", _RTY)
        ]],
    }
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(data, "index_positioning", "ok")
    assert result.final_status == "Useful but optional"


def test_summarize_result_partial_coverage_useful(minimal_settings, sample_tickers):
    data = {
        "found_markets": ["es_sp500", "nq_nasdaq100"],
        "missing_markets": ["rty_russell2000"],
        "report_date": _RECENT,
        "weeks_found": 4,
        "records": [_make_record(k, n) for k, n in [("es_sp500", _ES), ("nq_nasdaq100", _NQ)]],
    }
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(data, "index_positioning", "ok")
    assert result.final_status == "Useful but optional"


def test_summarize_result_no_data_fallback(minimal_settings, sample_tickers):
    adapter = CFTCAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(None, "index_positioning", "error")
    assert result.final_status == "Fallback only"
