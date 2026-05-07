"""TDD tests for YahooHistoricalIngestor — replaces broken Stooq /q/d/l/ endpoint.

RED state: ingest_yahoo_historical.py does not exist yet.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import responses as rsps_lib

from storage.db import get_connection, init_schema

_YF_URL_RE = re.compile(r"https://query1\.finance\.yahoo\.com/v8/finance/chart/")


def _make_yf_response(n_rows: int, start_date: date | None = None, base_close: float = 100.0) -> dict:
    if start_date is None:
        start_date = date.today() - timedelta(days=n_rows + 5)
    timestamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for i in range(n_rows):
        d = start_date + timedelta(days=i)
        ts = int(datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp())
        c = base_close + i * 0.5
        timestamps.append(ts)
        opens.append(c - 1)
        highs.append(c + 2)
        lows.append(c - 2)
        closes.append(c)
        volumes.append(1_000_000)
    return {
        "chart": {
            "result": [{
                "timestamp": timestamps,
                "indicators": {"quote": [{
                    "open": opens, "high": highs, "low": lows,
                    "close": closes, "volume": volumes,
                }]},
            }],
            "error": None,
        }
    }


def _make_yf_error_response():
    return {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data"}}}


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_prices(conn, ticker: str, n: int, max_date: date | None = None) -> None:
    if max_date is None:
        max_date = date.today()
    for i in range(n):
        d = (max_date - timedelta(days=n - 1 - i)).isoformat()
        conn.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, open, high, low, close, volume, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (ticker, trade_date) DO NOTHING",
            [uuid.uuid4().hex[:16], ticker, d, 100.0, 105.0, 95.0, 100.0, 1_000_000, "yahoo"],
        )


# ── Freshness skip ─────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_skips_tickers_with_fresh_history(conn):
    """count >= 65 and max_date within 3 days → zero HTTP calls."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    _seed_prices(conn, "AAPL", 70, max_date=date.today() - timedelta(days=1))
    result = YahooHistoricalIngestor({}).ingest(conn, "run_skip", ["AAPL"])
    assert len(rsps_lib.calls) == 0
    assert result["records"] == 0


# ── Bootstrap ─────────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_bootstrap_fetches_full_year_for_new_ticker(conn):
    """Ticker with no existing rows → request uses range=1y, ≥200 rows stored."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    payload = _make_yf_response(200)
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)

    result = YahooHistoricalIngestor({}).ingest(conn, "run_boot", ["AAPL"])

    assert result["records"] >= 200
    req_url = rsps_lib.calls[0].request.url
    assert "range=1y" in req_url, f"Expected range=1y in URL, got: {req_url}"


@rsps_lib.activate
def test_sparse_history_triggers_bootstrap_not_incremental(conn):
    """Ticker with <65 rows (sparse) → uses range=1y (bootstrap), not period1."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    # 10 rows from yesterday — has data but not enough for momentum
    _seed_prices(conn, "AAPL", 10, max_date=date.today() - timedelta(days=1))
    payload = _make_yf_response(200)
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)

    YahooHistoricalIngestor({}).ingest(conn, "run_sparse", ["AAPL"])

    req_url = rsps_lib.calls[0].request.url
    assert "range=1y" in req_url, f"Sparse history should bootstrap, got: {req_url}"
    assert "period1=" not in req_url


# ── Incremental ────────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_incremental_uses_period1_for_stale_ticker(conn):
    """Ticker with >=65 rows but stale max_date → request uses period1/period2 (not range=1y)."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    max_date = date.today() - timedelta(days=15)
    _seed_prices(conn, "MSFT", 70, max_date=max_date)

    payload = _make_yf_response(10, start_date=max_date - timedelta(days=5))
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)

    YahooHistoricalIngestor({}).ingest(conn, "run_incr", ["MSFT"])

    req_url = rsps_lib.calls[0].request.url
    assert "period1=" in req_url, f"Expected period1 in URL for incremental fetch, got: {req_url}"
    assert "range=" not in req_url


# ── Data parsing ──────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_inserts_correct_ohlcv_values(conn):
    """OHLCV values from the JSON response are stored accurately."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    ts = int(datetime(2026, 4, 30, tzinfo=timezone.utc).timestamp())
    payload = {"chart": {"result": [{
        "timestamp": [ts],
        "indicators": {"quote": [{"open": [185.5], "high": [187.0], "low": [184.0],
                                   "close": [186.0], "volume": [65_000_000]}]},
    }], "error": None}}
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)

    YahooHistoricalIngestor({}).ingest(conn, "run_ohlcv", ["AAPL"])

    row = conn.execute(
        "SELECT open, high, low, close, volume FROM prices_daily WHERE ticker='AAPL'"
    ).fetchone()
    assert row is not None
    assert abs(row[0] - 185.5) < 0.01
    assert abs(row[1] - 187.0) < 0.01
    assert abs(row[2] - 184.0) < 0.01
    assert abs(row[3] - 186.0) < 0.01
    assert row[4] == 65_000_000


@rsps_lib.activate
def test_source_set_to_yahoo(conn):
    """All inserted rows have source='yahoo'."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    payload = _make_yf_response(3)
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)
    YahooHistoricalIngestor({}).ingest(conn, "run_src", ["AAPL"])
    sources = {r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM prices_daily WHERE ticker='AAPL'"
    ).fetchall()}
    assert sources == {"yahoo"}


@rsps_lib.activate
def test_duplicate_trade_date_not_crash(conn):
    """Re-inserting same date is silently skipped."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    payload = _make_yf_response(1, start_date=date(2026, 4, 30))
    for _ in range(2):
        rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)
    ingestor = YahooHistoricalIngestor({})
    ingestor.ingest(conn, "run_dup1", ["AAPL"])
    ingestor.ingest(conn, "run_dup2", ["AAPL"])
    count = conn.execute("SELECT COUNT(*) FROM prices_daily WHERE ticker='AAPL'").fetchone()[0]
    assert count == 1


@rsps_lib.activate
def test_handles_none_ohlcv_gracefully(conn):
    """Rows with None values are skipped; valid rows still stored."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    ts1 = int(datetime(2026, 4, 29, tzinfo=timezone.utc).timestamp())
    ts2 = int(datetime(2026, 4, 30, tzinfo=timezone.utc).timestamp())
    payload = {"chart": {"result": [{
        "timestamp": [ts1, ts2],
        "indicators": {"quote": [{
            "open": [None, 100.0], "high": [None, 105.0],
            "low": [None, 95.0], "close": [None, 102.0], "volume": [None, 500_000],
        }]},
    }], "error": None}}
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)
    result = YahooHistoricalIngestor({}).ingest(conn, "run_none", ["AAPL"])
    count = conn.execute("SELECT COUNT(*) FROM prices_daily WHERE ticker='AAPL'").fetchone()[0]
    assert count == 1


@rsps_lib.activate
def test_handles_http_error_gracefully(conn):
    """HTTP 429 → records=0, status='ok', no exception."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, body="Too Many Requests", status=429,
                 match_querystring=False)
    result = YahooHistoricalIngestor({}).ingest(conn, "run_429", ["AAPL"])
    assert result["records"] == 0
    assert result["status"] == "ok"


@rsps_lib.activate
def test_handles_error_response_gracefully(conn):
    """YF error JSON (no data) → records=0, no crash."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=_make_yf_error_response(), status=200,
                 match_querystring=False)
    result = YahooHistoricalIngestor({}).ingest(conn, "run_err", ["AAPL"])
    assert result["records"] == 0


@rsps_lib.activate
def test_multiple_tickers_separate_requests(conn):
    """3 tickers → 3 separate HTTP calls."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    payload = _make_yf_response(5)
    for _ in range(3):
        rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)
    YahooHistoricalIngestor({}).ingest(conn, "run_multi", ["AAPL", "MSFT", "NVDA"])
    assert len(rsps_lib.calls) == 3


@rsps_lib.activate
def test_logs_source_run(conn):
    """source_runs has a row with source_name='yahoo_historical'."""
    from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
    payload = _make_yf_response(3)
    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=payload, status=200, match_querystring=False)
    YahooHistoricalIngestor({}).ingest(conn, "run_log", ["AAPL"])
    row = conn.execute(
        "SELECT source_name, status FROM source_runs WHERE run_id='run_log'"
    ).fetchone()
    assert row is not None
    assert row[0] == "yahoo_historical"
    assert row[1] in ("ok", "skip")
