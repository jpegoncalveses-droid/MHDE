import re
import pytest
import responses as rsps_lib
from datetime import date, timedelta

from adapters.fred import FREDAdapter

_BASE = "https://api.stlouisfed.org/fred"
_OBS_RE = re.compile(rf"{re.escape(_BASE)}/series/observations")
_REL_RE = re.compile(rf"{re.escape(_BASE)}/series/release")
_DATES_RE = re.compile(rf"{re.escape(_BASE)}/release/dates")

_ALL_SERIES = ["FEDFUNDS", "DGS10", "CPIAUCSL", "UNRATE", "PAYEMS", "GDP"]
_RECENT = (date.today() - timedelta(days=2)).isoformat()
_FUTURE = (date.today() + timedelta(days=30)).isoformat()
_PAST = (date.today() - timedelta(days=30)).isoformat()
_STALE_DAILY = (date.today() - timedelta(days=30)).isoformat()   # 30d > 7d daily tolerance
_FRESH_DAILY = (date.today() - timedelta(days=2)).isoformat()    # 2d ≤ 7d daily tolerance
_FRESH_QUARTERLY = (date.today() - timedelta(days=90)).isoformat()  # 90d ≤ 150d quarterly tolerance


def _obs_payload(obs_date=_RECENT, value="5.33"):
    return {
        "count": 1, "limit": 10, "offset": 0,
        "observations": [
            {"date": obs_date, "value": value,
             "realtime_start": obs_date, "realtime_end": "9999-12-31"},
        ],
    }


def _release_payload(release_id=18, name="H.15 Selected Interest Rates"):
    return {
        "releases": [{"id": release_id, "name": name, "press_release": False}]
    }


def _dates_payload(dates):
    return {
        "count": len(dates),
        "release_dates": [{"release_id": 18, "date": d} for d in dates],
    }


def _register_macro_obs(n=6, obs_date=_RECENT):
    for _ in range(n):
        rsps_lib.add(rsps_lib.GET, _OBS_RE,
                     json=_obs_payload(obs_date), status=200, match_querystring=False)


def _register_release_calendar(upcoming_dates, fallback_dates=None):
    """Register series/release + release/dates mocks for all 6 series."""
    for _ in range(6):
        rsps_lib.add(rsps_lib.GET, _REL_RE,
                     json=_release_payload(), status=200, match_querystring=False)
    for _ in range(6):
        rsps_lib.add(rsps_lib.GET, _DATES_RE,
                     json=_dates_payload(upcoming_dates), status=200, match_querystring=False)
        if fallback_dates is not None:
            rsps_lib.add(rsps_lib.GET, _DATES_RE,
                         json=_dates_payload(fallback_dates), status=200, match_querystring=False)


# ── test_access ─────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET, _OBS_RE, json=_obs_payload(), status=200, match_querystring=False)
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"
    assert err is None


@rsps_lib.activate
def test_test_access_auth_fail(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET, _OBS_RE,
                 json={"error_message": "Bad API Key"}, status=403, match_querystring=False)
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "auth_fail"


@rsps_lib.activate
def test_test_access_rate_limited(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET, _OBS_RE, json={}, status=429, match_querystring=False)
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "rate_limited"


def test_test_access_missing_api_key(minimal_settings, sample_tickers):
    settings = dict(minimal_settings)
    settings["fred"] = {"base_url": _BASE, "rate_limit_delay": 0}  # no api_key
    adapter = FREDAdapter(settings=settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "auth_fail"
    assert err is not None


@rsps_lib.activate
def test_test_access_bad_request_not_auth_fail(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET, _OBS_RE,
                 json={"error_message": "Bad Request: unknown param"}, status=400,
                 match_querystring=False)
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "error"
    assert "400" in err


# ── fetch_sample_data – macro_series ─────────────────────────────────────────

@rsps_lib.activate
def test_fetch_macro_series_returns_dict(minimal_settings, sample_tickers):
    _register_macro_obs(n=6)
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "macro_series")
    assert isinstance(data, dict)
    assert set(data.keys()) == set(_ALL_SERIES)
    assert "observations" in data["FEDFUNDS"]
    assert len(data["FEDFUNDS"]["observations"]) == 1


@rsps_lib.activate
def test_fetch_macro_series_partial_failure(minimal_settings, sample_tickers):
    for _ in range(4):
        rsps_lib.add(rsps_lib.GET, _OBS_RE, json=_obs_payload(), status=200, match_querystring=False)
    for _ in range(2):
        rsps_lib.add(rsps_lib.GET, _OBS_RE, json={}, status=500, match_querystring=False)
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "macro_series")
    assert data is not None
    assert len(data) == 4


# ── fetch_sample_data – release_calendar ─────────────────────────────────────

@rsps_lib.activate
def test_fetch_release_calendar_upcoming(minimal_settings, sample_tickers):
    _register_release_calendar(upcoming_dates=[_FUTURE])
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "release_calendar")
    assert isinstance(data, dict)
    assert set(data.keys()) == set(_ALL_SERIES)
    rec = data["FEDFUNDS"]
    assert rec["release_id"] == 18
    assert rec["release_name"] == "H.15 Selected Interest Rates"
    assert _FUTURE in rec["dates"]
    assert rec["source"] == "upcoming"


@rsps_lib.activate
def test_fetch_release_calendar_fallback(minimal_settings, sample_tickers):
    _register_release_calendar(upcoming_dates=[], fallback_dates=[_PAST])
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "release_calendar")
    assert data is not None
    assert all(r["source"] == "recent" for r in data.values())
    assert all(_PAST in r["dates"] for r in data.values())


# ── validate_schema ──────────────────────────────────────────────────────────

def test_validate_schema_macro_passes(minimal_settings, sample_tickers):
    data = {sid: _obs_payload() for sid in _ALL_SERIES}
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "macro_series")
    assert ok is True
    assert missing == []


def test_validate_schema_macro_missing_value(minimal_settings, sample_tickers):
    bad_obs = {"count": 1, "observations": [{"date": _RECENT}]}  # no "value"
    data = {"FEDFUNDS": bad_obs}
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "macro_series")
    assert ok is False
    assert any("value" in m for m in missing)


def test_validate_schema_release_passes(minimal_settings, sample_tickers):
    data = {
        sid: {"series_id": sid, "release_id": 18,
              "release_name": "H.15", "dates": [_FUTURE], "source": "upcoming"}
        for sid in _ALL_SERIES
    }
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "release_calendar")
    assert ok is True
    assert missing == []


def test_validate_schema_release_missing_dates(minimal_settings, sample_tickers):
    data = {
        "FEDFUNDS": {"series_id": "FEDFUNDS", "release_id": 18,
                     "release_name": "H.15", "dates": [], "source": "missing_dates"}
    }
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "release_calendar")
    assert ok is False
    assert any("dates_empty" in m for m in missing)


# ── evaluate_freshness ───────────────────────────────────────────────────────

def test_evaluate_freshness_daily_within_tolerance(minimal_settings, sample_tickers):
    data = {"DGS10": _obs_payload(obs_date=_FRESH_DAILY)}
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "macro_series") == "1d"


def test_evaluate_freshness_daily_stale(minimal_settings, sample_tickers):
    data = {"DGS10": _obs_payload(obs_date=_STALE_DAILY)}
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    freshness = adapter.evaluate_freshness(data, "macro_series")
    assert freshness.startswith("stale:")


def test_evaluate_freshness_quarterly_within_tolerance(minimal_settings, sample_tickers):
    data = {"GDP": _obs_payload(obs_date=_FRESH_QUARTERLY)}
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "macro_series") == "1q"


# ── summarize_result ─────────────────────────────────────────────────────────

def test_summarize_result_ok_data_valid_is_core(minimal_settings, sample_tickers):
    data = {sid: _obs_payload(obs_date=_FRESH_DAILY) for sid in _ALL_SERIES}
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(data, "macro_series", "ok")
    assert result.final_status == "Core"


def test_summarize_result_auth_fail_rejects(minimal_settings, sample_tickers):
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(None, "macro_series", "auth_fail")
    assert result.final_status == "Reject for v1"


def test_summarize_result_ok_no_data_fallback(minimal_settings, sample_tickers):
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(None, "macro_series", "ok")
    assert result.final_status in ("Fallback only", "Reject for v1")
    assert result.final_status != "Core"


def test_summarize_result_partial_macro_coverage_not_core(minimal_settings, sample_tickers):
    # Only 2 of 6 series — below _CORE_COVERAGE_MIN (5)
    data = {
        "FEDFUNDS": _obs_payload(obs_date=_FRESH_DAILY),
        "DGS10":    _obs_payload(obs_date=_FRESH_DAILY),
    }
    adapter = FREDAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(data, "macro_series", "ok")
    assert result.final_status != "Core"
