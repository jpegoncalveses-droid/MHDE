"""Stooq historical OHLCV ingestor — Phase 1 data-quality TDD suite."""
from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from urllib.parse import urlparse, parse_qs

import pytest
import responses as rsps_lib

from storage.db import get_connection, init_schema
from features.momentum import compute_momentum

_HIST_URL_RE = re.compile(r"https://stooq\.com/q/d/l/")

# Historical CSV: Date,Open,High,Low,Close,Volume  (no Symbol, no Time column)
_HIST_HEADER = "Date,Open,High,Low,Close,Volume\n"


def _hist_row(dt, open_=100.0, high=105.0, low=95.0, close=100.0, volume=1_000_000):
    return f"{dt},{open_},{high},{low},{close},{volume}\n"


def _make_hist_csv(num_rows: int, start_date: date | None = None, base_close: float = 100.0) -> str:
    if start_date is None:
        start_date = date.today() - timedelta(days=num_rows)
    lines = [_HIST_HEADER]
    for i in range(num_rows):
        dt = (start_date + timedelta(days=i)).isoformat()
        close = base_close + i * 0.5
        lines.append(_hist_row(dt, open_=close - 1, high=close + 2, low=close - 2, close=close))
    return "".join(lines)


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_prices(conn, ticker: str, num_rows: int, max_date: date | None = None) -> None:
    if max_date is None:
        max_date = date.today()
    for i in range(num_rows):
        dt = (max_date - timedelta(days=num_rows - 1 - i)).isoformat()
        conn.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, open, high, low, close, volume, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (ticker, trade_date) DO NOTHING",
            [uuid.uuid4().hex[:16], ticker, dt, 100.0, 105.0, 95.0, 100.0, 1_000_000, "stooq"],
        )


def _parse_d1(url: str) -> date:
    params = parse_qs(urlparse(url).query)
    raw = params["d1"][0]  # YYYYMMDD
    return date.fromisoformat(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")


# ── Freshness skip ─────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_skips_fresh_tickers_with_enough_history(conn):
    """count >= 20 and max_date within FRESHNESS_DAYS → zero HTTP calls."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    _seed_prices(conn, "AAPL", 25, max_date=date.today() - timedelta(days=1))

    result = StooqHistoricalIngestor({}).ingest(conn, "run_skip", ["AAPL"])

    assert len(rsps_lib.calls) == 0
    assert result["records"] == 0


# ── Bootstrap ─────────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_bootstrap_fetches_252_days_of_history(conn):
    """Ticker with no existing rows → d1 param ≈ today-252, ≥200 rows stored."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    csv = _make_hist_csv(200, start_date=date.today() - timedelta(days=200))
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)

    result = StooqHistoricalIngestor({}).ingest(conn, "run_boot", ["AAPL"])

    assert result["records"] >= 200

    req_url = rsps_lib.calls[0].request.url
    d1 = _parse_d1(req_url)
    expected = date.today() - timedelta(days=252)
    assert abs((d1 - expected).days) <= 5, f"d1={d1} expected≈{expected}"


# ── Incremental ────────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_incremental_only_fetches_since_last_date(conn):
    """15 rows, max_date=today-10 → fetch with d1 ≈ max_date - INCREMENTAL_BUFFER."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    max_date = date.today() - timedelta(days=10)
    _seed_prices(conn, "MSFT", 15, max_date=max_date)

    csv = _make_hist_csv(10, start_date=max_date - timedelta(days=5))
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)

    StooqHistoricalIngestor({}).ingest(conn, "run_incr", ["MSFT"])

    d1 = _parse_d1(rsps_lib.calls[0].request.url)
    expected = max_date - timedelta(days=5)  # INCREMENTAL_BUFFER = 5
    assert abs((d1 - expected).days) <= 2, f"d1={d1} expected≈{expected}"


# ── Data parsing ──────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_inserts_parsed_ohlcv_correctly(conn):
    """OHLCV values from a known CSV row are stored accurately."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    csv = _HIST_HEADER + "2026-04-30,185.50,187.00,184.00,186.00,65000000\n"
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)

    StooqHistoricalIngestor({}).ingest(conn, "run_ohlcv", ["AAPL"])

    row = conn.execute(
        "SELECT open, high, low, close, volume FROM prices_daily"
        " WHERE ticker='AAPL' AND trade_date='2026-04-30'"
    ).fetchone()
    assert row is not None
    assert abs(row[0] - 185.50) < 0.01
    assert abs(row[1] - 187.00) < 0.01
    assert abs(row[2] - 184.00) < 0.01
    assert abs(row[3] - 186.00) < 0.01
    assert row[4] == 65_000_000


@rsps_lib.activate
def test_source_set_to_stooq(conn):
    """All inserted rows have source='stooq'."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    csv = _HIST_HEADER + "2026-04-30,100.0,105.0,95.0,102.0,500000\n"
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)

    StooqHistoricalIngestor({}).ingest(conn, "run_src", ["AAPL"])

    sources = {r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM prices_daily WHERE ticker='AAPL'"
    ).fetchall()}
    assert sources == {"stooq"}


@rsps_lib.activate
def test_conflict_on_duplicate_trade_date_does_not_crash(conn):
    """Re-inserting the same date is silently ignored; only 1 row stored."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    csv = _HIST_HEADER + "2026-04-30,100.0,105.0,95.0,102.0,500000\n"
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqHistoricalIngestor({})
    ingestor.ingest(conn, "run_dup1", ["AAPL"])
    ingestor.ingest(conn, "run_dup2", ["AAPL"])  # same data again

    count = conn.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker='AAPL' AND trade_date='2026-04-30'"
    ).fetchone()[0]
    assert count == 1


# ── Edge cases ────────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_handles_nd_rows_gracefully(conn):
    """N/D rows are skipped; valid rows are still stored."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    csv = (
        _HIST_HEADER
        + "2026-04-30,N/D,N/D,N/D,N/D,N/D\n"
        + "2026-04-29,100.0,105.0,95.0,102.0,500000\n"
    )
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)

    result = StooqHistoricalIngestor({}).ingest(conn, "run_nd", ["AAPL"])

    assert result["records"] == 1


@rsps_lib.activate
def test_handles_empty_response(conn):
    """Header-only CSV → records=0, no crash."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=_HIST_HEADER, status=200,
                 content_type="text/csv", match_querystring=False)

    result = StooqHistoricalIngestor({}).ingest(conn, "run_empty", ["AAPL"])

    assert result["records"] == 0


@rsps_lib.activate
def test_handles_http_error_gracefully(conn):
    """HTTP 403 → records=0, status='ok', no exception raised."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body="Forbidden", status=403,
                 match_querystring=False)

    result = StooqHistoricalIngestor({}).ingest(conn, "run_403", ["AAPL"])

    assert result["records"] == 0
    assert result["status"] == "ok"


# ── Source run logging ────────────────────────────────────────────────────────

@rsps_lib.activate
def test_logs_source_run_on_completion(conn):
    """source_runs has a row with source_name='stooq_historical' after ingest."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    csv = _HIST_HEADER + "2026-04-30,100.0,105.0,95.0,102.0,500000\n"
    rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                 content_type="text/csv", match_querystring=False)

    StooqHistoricalIngestor({}).ingest(conn, "run_log", ["AAPL"])

    row = conn.execute(
        "SELECT source_name, status FROM source_runs WHERE run_id='run_log'"
    ).fetchone()
    assert row is not None
    assert row[0] == "stooq_historical"
    assert row[1] in ("ok", "skip")


# ── Multiple tickers ──────────────────────────────────────────────────────────

@rsps_lib.activate
def test_multiple_tickers_all_fetched(conn):
    """3 tickers needing history → 3 separate HTTP calls (one per ticker)."""
    from ingestion.ingest_stooq_historical import StooqHistoricalIngestor

    csv = _HIST_HEADER + "2026-04-30,100.0,105.0,95.0,102.0,500000\n"
    for _ in range(3):
        rsps_lib.add(rsps_lib.GET, _HIST_URL_RE, body=csv, status=200,
                     content_type="text/csv", match_querystring=False)

    StooqHistoricalIngestor({}).ingest(conn, "run_multi", ["AAPL", "MSFT", "NVDA"])

    assert len(rsps_lib.calls) == 3


# ── Momentum source label fix ─────────────────────────────────────────────────

def test_momentum_source_label_is_not_polygon(conn):
    """compute_momentum must not hardcode source='polygon' — should be 'prices_daily'."""
    _seed_prices(conn, "AAPL", 65)

    features = compute_momentum(conn, "run_mom", "AAPL", date.today())

    for f in features:
        assert f["source"] == "prices_daily", (
            f"Feature '{f['feature_name']}' has source='{f['source']}' "
            f"— should be 'prices_daily', not 'polygon'"
        )


def test_momentum_computes_non_null_with_65_stooq_rows(conn):
    """65 stooq-sourced price rows → non-null momentum scores with correct source label."""
    _seed_prices(conn, "AAPL", 65)

    features = compute_momentum(conn, "run_mom2", "AAPL", date.today())

    scored = [f for f in features if f["feature_score"] is not None]
    assert len(scored) >= 2, (
        f"Expected >=2 scored momentum features, got {len(scored)}: "
        f"{[(f['feature_name'], f['feature_score']) for f in features]}"
    )
    for f in scored:
        assert f["source"] == "prices_daily", (
            f"Scored feature '{f['feature_name']}' has source='{f['source']}'"
        )
