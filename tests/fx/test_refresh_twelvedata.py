"""Unit tests for fx/data/refresh_twelvedata.py.

The fetcher hits a live HTTP API in production. All tests here mock
`requests.get` so we never make real network calls. Set
TWELVEDATA_API_KEY in the test env where needed via monkeypatch.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from fx.data import refresh_twelvedata as rt


def _make_response(status_code: int = 200, json_payload=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_payload is not None:
        resp.json = lambda: json_payload
    else:
        resp.json = MagicMock(side_effect=ValueError("not JSON"))
    return resp


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    """Most tests assume a key is configured. Tests that need it
    unset can monkeypatch.delenv themselves."""
    monkeypatch.setenv("TWELVEDATA_API_KEY", "test_key_xxx")


# ──────────────────────────────────────────────────────────────────────
# fetch_latest_bar — happy path + error paths
# ──────────────────────────────────────────────────────────────────────


def test_fetch_happy_path_returns_ok_with_parsed_bar(monkeypatch):
    """API returns one bar → status=OK, bar dict fully populated."""
    payload = {
        "meta": {"symbol": "GBP/EUR", "interval": "1h", "exchange": "OTC",
                 "currency_base": "British Pound", "currency_quote": "Euro"},
        "values": [
            {"datetime": "2026-05-07 18:00:00",
             "open": "1.15700", "high": "1.15820", "low": "1.15640",
             "close": "1.15780"},
        ],
        "status": "ok",
    }
    monkeypatch.setattr(rt.requests, "get",
                        lambda url, **kw: _make_response(200, json_payload=payload))

    # Mid-week, not the FX weekend window.
    out = rt.fetch_latest_bar(now_utc=datetime(2026, 5, 7, 18, 30, 0))
    assert out["status"] == "OK"
    assert out["error"] is None
    bar = out["bar"]
    assert bar["datetime_utc"] == datetime(2026, 5, 7, 18, 0, 0)
    assert bar["date"].isoformat() == "2026-05-07"
    assert bar["weekday"] == "Thursday"
    assert bar["hour_utc"] == 18
    assert bar["gbpeur_open"] == pytest.approx(1.15700)
    assert bar["gbpeur_high"] == pytest.approx(1.15820)
    assert bar["gbpeur_low"] == pytest.approx(1.15640)
    assert bar["gbpeur_close"] == pytest.approx(1.15780)
    assert bar["data_quality"] == "OK"
    assert bar["tick_count"] is None


def test_fetch_no_data_when_payload_empty(monkeypatch):
    payload = {"meta": {}, "values": [], "status": "ok"}
    monkeypatch.setattr(rt.requests, "get",
                        lambda url, **kw: _make_response(200, json_payload=payload))
    out = rt.fetch_latest_bar(now_utc=datetime(2026, 5, 7, 18, 30, 0))
    assert out["status"] == "NO_DATA"
    assert out["bar"] is None


def test_fetch_error_on_5xx(monkeypatch):
    monkeypatch.setattr(
        rt.requests, "get",
        lambda url, **kw: _make_response(503, text="Service Unavailable"),
    )
    out = rt.fetch_latest_bar(now_utc=datetime(2026, 5, 7, 18, 30, 0))
    assert out["status"] == "ERROR"
    assert "503" in out["error"]
    assert out["bar"] is None


def test_fetch_error_on_request_exception(monkeypatch):
    import requests as _requests

    def _boom(url, **kw):
        raise _requests.ConnectTimeout("connection timed out")

    monkeypatch.setattr(rt.requests, "get", _boom)
    out = rt.fetch_latest_bar(now_utc=datetime(2026, 5, 7, 18, 30, 0))
    assert out["status"] == "ERROR"
    assert "timed out" in out["error"].lower()


def test_fetch_error_on_twelvedata_error_envelope(monkeypatch):
    """TwelveData returns 200 with status=error in the JSON envelope."""
    payload = {"status": "error", "code": 401,
               "message": "Invalid API key — check your subscription."}
    monkeypatch.setattr(rt.requests, "get",
                        lambda url, **kw: _make_response(200, json_payload=payload))
    out = rt.fetch_latest_bar(now_utc=datetime(2026, 5, 7, 18, 30, 0))
    assert out["status"] == "ERROR"
    assert "API key" in out["error"]


def test_fetch_closed_on_fx_weekend_no_http_call(monkeypatch):
    """Sat 22:00 UTC is inside the FX weekend window — fetcher must
    not even issue an HTTP call."""
    called = {"count": 0}

    def _spy(*args, **kwargs):
        called["count"] += 1
        return _make_response(200, json_payload={"values": []})

    monkeypatch.setattr(rt.requests, "get", _spy)
    out = rt.fetch_latest_bar(now_utc=datetime(2026, 5, 9, 22, 0, 0))  # Saturday
    assert out["status"] == "CLOSED"
    assert out["bar"] is None
    assert called["count"] == 0


def test_fetch_returns_error_when_api_key_missing(monkeypatch):
    """Removing the env var (and no engine config fallback) → ERROR
    status with a clear message; no HTTP call."""
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)

    # Stub load_engine_config to also return no key.
    import storage.config as scfg
    monkeypatch.setattr(scfg, "load_engine_config", lambda *a, **kw: {})

    called = {"count": 0}
    monkeypatch.setattr(rt.requests, "get",
                        lambda *a, **kw: called.update(count=called["count"] + 1))

    out = rt.fetch_latest_bar(now_utc=datetime(2026, 5, 7, 18, 30, 0))
    assert out["status"] == "ERROR"
    assert "TWELVEDATA_API_KEY" in out["error"]
    assert called["count"] == 0


# ──────────────────────────────────────────────────────────────────────
# upsert_new_bars — DB mechanics
# ──────────────────────────────────────────────────────────────────────


def _sample_bar(dt: datetime, close: float = 1.158) -> dict:
    return {
        "datetime_utc": dt,
        "date": dt.date(),
        "weekday": dt.strftime("%A"),
        "hour_utc": dt.hour,
        "gbpeur_open": close - 0.0005,
        "gbpeur_high": close + 0.0010,
        "gbpeur_low": close - 0.0010,
        "gbpeur_close": close,
        "tick_count": None,
        "data_quality": "OK",
    }


def test_upsert_writes_one_row(temp_db):
    bar = _sample_bar(datetime(2026, 5, 7, 18, 0, 0))
    n = rt.upsert_new_bars(temp_db, bar)
    assert n == 1
    row = temp_db.execute(
        "SELECT gbpeur_close FROM fx_prices_hourly_twelvedata WHERE datetime_utc = ?",
        [bar["datetime_utc"]],
    ).fetchone()
    assert row[0] == pytest.approx(bar["gbpeur_close"])


def test_upsert_duplicate_is_noop(temp_db):
    bar = _sample_bar(datetime(2026, 5, 7, 18, 0, 0))
    assert rt.upsert_new_bars(temp_db, bar) == 1
    assert rt.upsert_new_bars(temp_db, bar) == 0
    n_rows = temp_db.execute(
        "SELECT COUNT(*) FROM fx_prices_hourly_twelvedata"
    ).fetchone()[0]
    assert n_rows == 1


def test_upsert_handles_none_bar(temp_db):
    """When fetch_latest_bar returned NO_DATA / CLOSED / ERROR, the bar
    is None — upsert must noop, not crash."""
    assert rt.upsert_new_bars(temp_db, None) == 0


# ──────────────────────────────────────────────────────────────────────
# refresh_prices — full orchestration
# ──────────────────────────────────────────────────────────────────────


def test_refresh_prices_writes_on_ok(temp_db, monkeypatch):
    payload = {
        "values": [{"datetime": "2026-05-07 19:00:00",
                    "open": "1.158", "high": "1.159", "low": "1.157",
                    "close": "1.1585"}],
        "status": "ok",
    }
    monkeypatch.setattr(rt.requests, "get",
                        lambda url, **kw: _make_response(200, json_payload=payload))
    # Override the freshness clock to avoid weekend skip.
    monkeypatch.setattr(rt, "datetime", _PatchableDatetime(datetime(2026, 5, 7, 19, 30, 0)))

    out = rt.refresh_prices(temp_db)
    assert out["fetch_status"] == "OK"
    assert out["rows_inserted"] == 1


def test_refresh_prices_skips_on_closed(temp_db, monkeypatch):
    """When market is closed, no HTTP call, no row, fetch_status=CLOSED."""
    monkeypatch.setattr(rt, "datetime",
                        _PatchableDatetime(datetime(2026, 5, 9, 22, 0, 0)))  # Sat

    called = {"count": 0}
    monkeypatch.setattr(rt.requests, "get",
                        lambda *a, **kw: called.update(count=called["count"] + 1))

    out = rt.refresh_prices(temp_db)
    assert out["fetch_status"] == "CLOSED"
    assert out["rows_inserted"] == 0
    assert called["count"] == 0


# Helper to monkeypatch `datetime.now(...)` inside the module: replaces
# the imported `datetime` symbol with a class whose .now() returns a
# fixed value. Cheaper than patching the actual datetime class.
class _PatchableDatetime:
    def __init__(self, fixed_now: datetime):
        self._now = fixed_now

    def now(self, tz=None):
        return self._now if tz is None else self._now.replace(tzinfo=tz)

    def __getattr__(self, name):
        return getattr(datetime, name)
