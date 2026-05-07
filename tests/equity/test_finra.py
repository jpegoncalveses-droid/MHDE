import io
import re
import pytest
import responses as rsps_lib

from adapters.finra import FINRAAdapter

_BASE = "https://cdn.finra.org/equity/otcmarket/biweekly"
_CSV_RE = re.compile(rf"{re.escape(_BASE)}/shrt\d{{8}}\.csv")

_BASKET = ["AAPL", "NVDA", "TSLA", "JPM", "UBER", "RKLB"]

_CSV_HEADER = (
    "accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode"
    "|marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity"
    "|stockSplitFlag|averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag"
    "|changePercent|changePreviousNumber|settlementDate\n"
)

_SETTLEMENT_DATE = "2026-04-15"
_OLDER_SETTLEMENT = "2026-04-01"


def _csv_row(symbol="AAPL", short_qty="1234567", prev_qty="1111111",
             settlement_date=_SETTLEMENT_DATE):
    return (
        f"202604|{symbol}|{symbol} Inc|Q|1|{short_qty}|{prev_qty}|N"
        f"|5000000|0.25|N|11.12|{int(short_qty)-int(prev_qty)}|{settlement_date}\n"
    )


def _make_csv(*symbols, settlement_date=_SETTLEMENT_DATE):
    rows = _CSV_HEADER
    for sym in symbols:
        rows += _csv_row(sym, settlement_date=settlement_date)
    return rows


def _full_csv(settlement_date=_SETTLEMENT_DATE):
    return _make_csv(*_BASKET, settlement_date=settlement_date)


def _register_csv(body, status=200, n=1):
    for _ in range(n):
        rsps_lib.add(
            rsps_lib.GET, _CSV_RE,
            body=body.encode("latin-1") if isinstance(body, str) else body,
            status=status,
            content_type="text/csv",
            match_querystring=False,
        )


def _register_head(status=200, n=1):
    for _ in range(n):
        rsps_lib.add(rsps_lib.HEAD, _CSV_RE, status=status, match_querystring=False)


# ── test_access ──────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    _register_head(status=200)
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"
    assert err is None


@rsps_lib.activate
def test_test_access_head_fallback_to_get(minimal_settings, sample_tickers):
    """HEAD returns 405; adapter must fall back to GET(stream=True) and succeed."""
    _register_head(status=405)
    _register_csv(_full_csv())
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"


@rsps_lib.activate
def test_test_access_all_404(minimal_settings, sample_tickers):
    for _ in range(20):
        rsps_lib.add(rsps_lib.HEAD, _CSV_RE, status=404, match_querystring=False)
        rsps_lib.add(rsps_lib.GET, _CSV_RE, status=404, match_querystring=False)
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "no_available_files"


@rsps_lib.activate
def test_test_access_auth_fail_401(minimal_settings, sample_tickers):
    _register_head(status=401)
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "auth_fail"


@rsps_lib.activate
def test_test_access_403_treated_as_not_found(minimal_settings, sample_tickers):
    """FINRA CDN returns 403 for unpublished dates (not an auth error); adapter skips them."""
    for _ in range(20):
        rsps_lib.add(rsps_lib.HEAD, _CSV_RE, status=403, match_querystring=False)
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "no_available_files"


# ── fetch_sample_data – short_interest ──────────────────────────────────────

@rsps_lib.activate
def test_fetch_short_interest_all_found(minimal_settings, sample_tickers):
    _register_head(status=200)
    _register_csv(_full_csv())
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "short_interest")
    assert data is not None
    assert data["found_symbols"] == set(_BASKET)
    assert data["missing_symbols"] == set()


@rsps_lib.activate
def test_fetch_short_interest_partial_symbols(minimal_settings, sample_tickers):
    partial = _make_csv("AAPL", "NVDA", "TSLA")
    _register_head(status=200)
    _register_csv(partial)
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "short_interest")
    assert {"AAPL", "NVDA", "TSLA"}.issubset(data["found_symbols"])
    assert {"JPM", "UBER", "RKLB"}.issubset(data["missing_symbols"])


@rsps_lib.activate
def test_fetch_short_interest_returns_rows(minimal_settings, sample_tickers):
    _register_head(status=200)
    _register_csv(_full_csv())
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "short_interest")
    assert "rows" in data
    assert len(data["rows"]) == len(_BASKET)


@rsps_lib.activate
def test_fetch_short_interest_no_file_found(minimal_settings, sample_tickers):
    for _ in range(20):
        rsps_lib.add(rsps_lib.HEAD, _CSV_RE, status=404, match_querystring=False)
        rsps_lib.add(rsps_lib.GET, _CSV_RE, status=404, match_querystring=False)
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "short_interest")
    assert data is None


# ── fetch_sample_data – short_interest_history ───────────────────────────────

@rsps_lib.activate
def test_fetch_history_returns_multiple_periods(minimal_settings, sample_tickers):
    for _ in range(4):
        _register_head(status=200)
        _register_csv(_full_csv())
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "short_interest_history")
    assert data is not None
    assert data["periods_found"] >= 1


@rsps_lib.activate
def test_fetch_history_records_settlement_dates(minimal_settings, sample_tickers):
    for _ in range(4):
        _register_head(status=200)
        _register_csv(_full_csv())
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    data = adapter.fetch_sample_data(sample_tickers, "short_interest_history")
    assert "settlement_dates" in data
    assert len(data["settlement_dates"]) >= 1


# ── validate_schema ──────────────────────────────────────────────────────────

def test_validate_schema_passes_full_row(minimal_settings, sample_tickers):
    data = {
        "found_symbols": set(_BASKET),
        "missing_symbols": set(),
        "rows": [
            {
                "symbolCode": "AAPL",
                "currentShortPositionQuantity": "1234567",
                "previousShortPositionQuantity": "1111111",
                "settlementDate": _SETTLEMENT_DATE,
                "averageDailyVolumeQuantity": "5000000",
            }
        ],
        "settlement_date": _SETTLEMENT_DATE,
    }
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "short_interest")
    assert ok is True
    assert missing == []


def test_validate_schema_fails_missing_required_field(minimal_settings, sample_tickers):
    data = {
        "found_symbols": {"AAPL"},
        "missing_symbols": set(_BASKET) - {"AAPL"},
        "rows": [{"symbolCode": "AAPL", "settlementDate": _SETTLEMENT_DATE}],
        "settlement_date": _SETTLEMENT_DATE,
    }
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "short_interest")
    assert ok is False
    assert len(missing) > 0


def test_validate_schema_optional_fields_missing_still_passes(minimal_settings, sample_tickers):
    """Row with only required fields (no changePercent/daysToCover) should still pass."""
    data = {
        "found_symbols": set(_BASKET),
        "missing_symbols": set(),
        "rows": [
            {
                "symbolCode": "AAPL",
                "currentShortPositionQuantity": "1234567",
                "previousShortPositionQuantity": "1111111",
                "settlementDate": _SETTLEMENT_DATE,
                "averageDailyVolumeQuantity": "5000000",
            }
        ],
        "settlement_date": _SETTLEMENT_DATE,
    }
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "short_interest")
    assert ok is True


def test_validate_schema_no_data(minimal_settings, sample_tickers):
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(None, "short_interest")
    assert ok is False
    assert "no_data" in missing


# ── evaluate_freshness ───────────────────────────────────────────────────────

def test_evaluate_freshness_current(minimal_settings, sample_tickers):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=10)).isoformat()
    data = {"settlement_date": recent}
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "short_interest") == "biweekly_current"


def test_evaluate_freshness_one_cycle_late(minimal_settings, sample_tickers):
    from datetime import date, timedelta
    late = (date.today() - timedelta(days=30)).isoformat()
    data = {"settlement_date": late}
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "short_interest") == "one_cycle_late"


def test_evaluate_freshness_stale(minimal_settings, sample_tickers):
    from datetime import date, timedelta
    stale = (date.today() - timedelta(days=60)).isoformat()
    data = {"settlement_date": stale}
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    freshness = adapter.evaluate_freshness(data, "short_interest")
    assert freshness.startswith("stale:")


def test_evaluate_freshness_history_uses_most_recent_period(minimal_settings, sample_tickers):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=10)).isoformat()
    older = (date.today() - timedelta(days=24)).isoformat()
    data = {"settlement_dates": [recent, older], "settlement_date": recent}
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_freshness(data, "short_interest_history") == "biweekly_current"


# ── evaluate_history ─────────────────────────────────────────────────────────

def test_evaluate_history_four_periods(minimal_settings, sample_tickers):
    data = {"periods_found": 4}
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    assert adapter.evaluate_history(data, "short_interest_history") == "4p"


def test_evaluate_history_single_period(minimal_settings, sample_tickers):
    data = {"periods_found": 1}
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.evaluate_history(data, "short_interest")
    assert result in ("Np", "1p")


# ── summarize_result ─────────────────────────────────────────────────────────

def test_summarize_result_all_found_is_core(minimal_settings, sample_tickers):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=10)).isoformat()
    data = {
        "found_symbols": set(_BASKET),
        "missing_symbols": set(),
        "rows": [
            {
                "symbolCode": s,
                "currentShortPositionQuantity": "1000000",
                "previousShortPositionQuantity": "900000",
                "settlementDate": recent,
                "averageDailyVolumeQuantity": "5000000",
            }
            for s in _BASKET
        ],
        "settlement_date": recent,
        "periods_found": 1,
    }
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(data, "short_interest", "ok")
    assert result.final_status == "Core"


def test_summarize_result_auth_fail_rejects(minimal_settings, sample_tickers):
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(None, "short_interest", "auth_fail")
    assert result.final_status == "Reject for v1"


def test_summarize_result_no_files_fallback(minimal_settings, sample_tickers):
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(None, "short_interest", "no_available_files")
    assert result.final_status in ("Fallback only", "Reject for v1")


def test_summarize_result_partial_coverage_not_core(minimal_settings, sample_tickers):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=10)).isoformat()
    found = {"AAPL", "NVDA"}
    missing = set(_BASKET) - found
    data = {
        "found_symbols": found,
        "missing_symbols": missing,
        "rows": [
            {
                "symbolCode": s,
                "currentShortPositionQuantity": "1000000",
                "previousShortPositionQuantity": "900000",
                "settlementDate": recent,
                "averageDailyVolumeQuantity": "5000000",
            }
            for s in found
        ],
        "settlement_date": recent,
        "periods_found": 1,
    }
    adapter = FINRAAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result = adapter.summarize_result(data, "short_interest", "ok")
    assert result.final_status != "Core"
