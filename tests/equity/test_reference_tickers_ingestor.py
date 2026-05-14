"""TDD tests for ReferenceTickersIngestor.

The cross-asset reference set (SPY, VIX, sector ETFs) is consumed by
ml/features.py but has no producer-side counterpart in the scheduled
ingestion chain (see data/processed/finding1_cross_asset_ingestion_root_cause.md).

This ingestor closes that gap: it fetches the hardcoded REFERENCE_TICKERS
from Yahoo and writes them to prices_daily, bypassing the universe lookup.
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


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_reference_tickers_constant_includes_spy_vix_and_all_sector_etfs():
    """REFERENCE_TICKERS must cover every ticker ml/features.py consumes."""
    from ingestion.ingest_reference_tickers import REFERENCE_TICKERS

    expected = {
        "SPY", "VIX",
        "XLK", "XLF", "XLV", "XLE", "XLY",
        "XLI", "XLP", "XLB", "XLU", "XLRE", "XLC",
    }
    assert expected.issubset(set(REFERENCE_TICKERS)), (
        f"Missing tickers from REFERENCE_TICKERS: {expected - set(REFERENCE_TICKERS)}"
    )


def test_ingestor_registered_in_orchestrator():
    """ReferenceTickersIngestor must be in _ALL_INGESTORS so the 23:15 chain picks it up."""
    from ingestion.orchestrator import _ALL_INGESTORS
    from ingestion.ingest_reference_tickers import ReferenceTickersIngestor

    assert ReferenceTickersIngestor in _ALL_INGESTORS


@rsps_lib.activate
def test_ingest_writes_prices_for_every_reference_ticker(conn):
    """A full ingest call should produce prices_daily rows for SPY, VIX, and all XL* tickers."""
    from ingestion.ingest_reference_tickers import ReferenceTickersIngestor, REFERENCE_TICKERS

    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=_make_yf_response(10),
                 status=200, match_querystring=False)

    result = ReferenceTickersIngestor({}).ingest(conn, "run_ref", tickers=[])

    assert result["status"] in ("ok", "experimental")
    written = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT ticker FROM prices_daily WHERE source='yahoo'"
        ).fetchall()
    }
    assert set(REFERENCE_TICKERS).issubset(written), (
        f"Missing prices_daily rows for: {set(REFERENCE_TICKERS) - written}"
    )


@rsps_lib.activate
def test_ingest_bypasses_universe_argument(conn):
    """The ingestor must NOT depend on the `tickers` argument (which is the universe list).

    Passing an empty tickers list still pulls the reference set.
    """
    from ingestion.ingest_reference_tickers import ReferenceTickersIngestor

    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=_make_yf_response(5),
                 status=200, match_querystring=False)

    ReferenceTickersIngestor({}).ingest(conn, "run_empty_universe", tickers=[])

    n_spy = conn.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker='SPY'"
    ).fetchone()[0]
    assert n_spy > 0, "SPY rows must be written even when universe tickers=[]"


@rsps_lib.activate
def test_ingest_records_source_yahoo(conn):
    """Reference rows must be written with source='yahoo' so they share the existing source column convention."""
    from ingestion.ingest_reference_tickers import ReferenceTickersIngestor

    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=_make_yf_response(5),
                 status=200, match_querystring=False)

    ReferenceTickersIngestor({}).ingest(conn, "run_src", tickers=[])

    sources = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT source FROM prices_daily WHERE ticker='SPY'"
        ).fetchall()
    }
    assert sources == {"yahoo"}, f"Expected only source='yahoo', got {sources}"


@rsps_lib.activate
def test_ingest_requests_vix_as_url_encoded_caret(conn):
    """VIX must be requested from Yahoo as ^VIX (URL-encoded %5EVIX).

    Bare 'VIX' resolves to a dormant Yahoo mutual-fund placeholder, not the
    CBOE VIX index. Storage stays as 'VIX'; only the API request is translated.
    """
    from ingestion.ingest_reference_tickers import ReferenceTickersIngestor

    rsps_lib.add(rsps_lib.GET, _YF_URL_RE, json=_make_yf_response(5),
                 status=200, match_querystring=False)

    ReferenceTickersIngestor({}).ingest(conn, "run_vix_url", tickers=[])

    requested_urls = [call.request.url for call in rsps_lib.calls]
    vix_calls = [u for u in requested_urls if "VIX" in u or "%5EVIX" in u]
    assert vix_calls, f"No VIX request found in {requested_urls!r}"
    assert all("%5EVIX" in u for u in vix_calls), (
        f"VIX must be URL-encoded as %5EVIX (^VIX), got: {vix_calls!r}"
    )

    stored = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT ticker FROM prices_daily WHERE ticker IN ('VIX', '^VIX')"
        ).fetchall()
    }
    assert stored == {"VIX"}, f"Storage must remain bare 'VIX', got {stored}"
