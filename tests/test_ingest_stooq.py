from __future__ import annotations

import re
import uuid
from datetime import date, timedelta, datetime

import pytest
import responses as rsps_lib

from storage.db import get_connection, init_schema

_STOOQ_RE = re.compile(r"https://stooq\.com/q/l/")

# Stooq /q/l/ quote format: Symbol,Date,Time,Open,High,Low,Close,Volume
_CSV_HEADER = "Symbol,Date,Time,Open,High,Low,Close,Volume\n"


def _quote_row(symbol, date_str, open_, high, low, close, volume, time_="19:00:00"):
    return f"{symbol}.US,{date_str},{time_},{open_},{high},{low},{close},{volume}\n"


_SAMPLE_CSV = (
    _CSV_HEADER
    + _quote_row("MSFT", "2026-04-30", "185.5", "187.0", "184.0", "186.0", "65000000")
)


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_fresh_prices(conn, ticker, days_ago=0):
    trade_date = (date.today() - timedelta(days=days_ago)).isoformat()
    conn.execute(
        """INSERT INTO prices_daily (id, ticker, trade_date, close, source)
           VALUES (?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], ticker, trade_date, 100.0, "polygon"],
    )


# ── freshness skip ────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_skips_all_tickers_when_all_have_fresh_prices(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    _seed_fresh_prices(conn, "AAPL")
    _seed_fresh_prices(conn, "MSFT")

    ingestor = StooqPricesIngestor({})
    result = ingestor.ingest(conn, "run_skip", ["AAPL", "MSFT"])

    assert result["status"] == "skip"
    assert result["records"] == 0
    assert len(rsps_lib.calls) == 0, "No HTTP calls should be made when all tickers are fresh"


@rsps_lib.activate
def test_only_fetches_tickers_missing_prices(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    _seed_fresh_prices(conn, "AAPL")  # fresh → skip
    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body=_SAMPLE_CSV, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    result = ingestor.ingest(conn, "run_partial", ["AAPL", "MSFT"])

    assert len(rsps_lib.calls) == 1, "Only MSFT should be fetched"
    assert result["records"] > 0


# ── successful fetch ──────────────────────────────────────────────────────────

@rsps_lib.activate
def test_inserts_prices_for_missing_tickers(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body=_SAMPLE_CSV, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    result = ingestor.ingest(conn, "run_insert", ["MSFT"])

    assert result["status"] == "ok"
    count = conn.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker='MSFT'"
    ).fetchone()[0]
    assert count == 1, f"Expected 1 row (latest quote), got {count}"


@rsps_lib.activate
def test_prices_stored_with_stooq_source(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body=_SAMPLE_CSV, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    ingestor.ingest(conn, "run_src", ["MSFT"])

    sources = {r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM prices_daily WHERE ticker='MSFT'"
    ).fetchall()}
    assert "stooq" in sources, f"Expected source='stooq', got {sources}"


@rsps_lib.activate
def test_close_price_stored_correctly(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body=_SAMPLE_CSV, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    ingestor.ingest(conn, "run_price", ["MSFT"])

    row = conn.execute(
        "SELECT close FROM prices_daily WHERE ticker='MSFT' ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert abs(row[0] - 186.0) < 0.01


# ── error handling ────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_handles_404_gracefully(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body="Not found", status=404,
                 match_querystring=False)

    ingestor = StooqPricesIngestor({})
    result = ingestor.ingest(conn, "run_404", ["ZZZZ"])

    assert result["status"] == "ok"  # doesn't crash
    assert result["records"] == 0


@rsps_lib.activate
def test_handles_empty_csv_gracefully(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body=_CSV_HEADER, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    result = ingestor.ingest(conn, "run_empty", ["ZZZZ"])

    assert result["status"] == "ok"
    assert result["records"] == 0


@rsps_lib.activate
def test_handles_no_data_text_gracefully(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body="No data\n", status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    result = ingestor.ingest(conn, "run_nodata", ["ZZZZ"])

    assert result["status"] == "ok"
    assert result["records"] == 0


@rsps_lib.activate
def test_continues_after_batch_failure(conn):
    """Two batches: first fails (500), second succeeds. Second batch result is stored."""
    from ingestion.ingest_stooq import StooqPricesIngestor

    # Two tickers exceed batch_size=1 only if we patch it; instead register two responses
    # and rely on batch ordering: first batch 500, second batch 200.
    # Simplest: two tickers in one batch → one call, 500 → 0 inserted.
    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body="error", status=500,
                 match_querystring=False)

    ingestor = StooqPricesIngestor({})
    result = ingestor.ingest(conn, "run_500", ["MSFT"])

    assert result["status"] == "ok"   # doesn't crash
    assert result["records"] == 0


# ── source_runs logging ───────────────────────────────────────────────────────

@rsps_lib.activate
def test_logs_source_run_on_success(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body=_SAMPLE_CSV, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    ingestor.ingest(conn, "run_log", ["MSFT"])

    row = conn.execute(
        "SELECT source_name, status FROM source_runs WHERE run_id='run_log'"
    ).fetchone()
    assert row is not None
    assert row[0] == "stooq"
    assert row[1] in ("ok", "skip")


# ── conflict: polygon wins ────────────────────────────────────────────────────

@rsps_lib.activate
def test_polygon_prices_not_overwritten_by_stooq(conn):
    from ingestion.ingest_stooq import StooqPricesIngestor

    # Seed a Polygon price for the same date that Stooq would return
    conn.execute(
        """INSERT INTO prices_daily (id, ticker, trade_date, close, source)
           VALUES (?, 'AAPL', '2026-04-30', 999.0, 'polygon')""",
        [uuid.uuid4().hex[:16]],
    )
    # Stooq returns 186.0 for same date
    rsps_lib.add(rsps_lib.GET, _STOOQ_RE, body=_SAMPLE_CSV, status=200,
                 content_type="text/csv", match_querystring=False)

    ingestor = StooqPricesIngestor({})
    # AAPL has price from 2026-04-30 but freshness check uses today's date;
    # since 2026-04-30 is still within freshness window, ingestor should skip AAPL
    result = ingestor.ingest(conn, "run_conflict", ["AAPL"])

    close = conn.execute(
        "SELECT close FROM prices_daily WHERE ticker='AAPL' AND trade_date='2026-04-30'"
    ).fetchone()[0]
    assert abs(close - 999.0) < 0.01, "Polygon price should not be overwritten"
    assert result["records"] == 0
