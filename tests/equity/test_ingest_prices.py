"""Unit tests for the Polygon equity prices ingestor.

The 2026-05-09 KI-120 fix replaced the per-ticker loop with a
grouped-daily primary path plus a bounded per-ticker fallback. These
tests pin that behavior:

  - Grouped path only inserts rows whose `T` is in the universe set.
  - Non-trading days (empty `results`) are silently fine.
  - Per-ticker fallback runs (only) for universe tickers absent from
    the grouped feed, capped by ``fallback_limit_per_date``.
  - Re-running the same call is idempotent thanks to the
    `prices_daily` PK + ON CONFLICT DO NOTHING.
  - Missing API key short-circuits with status="skip".
  - The default ``ingest()`` entry point fans out across the last
    ``DEFAULT_LOOKBACK_DAYS`` calendar days (one grouped call each).
"""
from __future__ import annotations

import re
from datetime import date

import pytest
import responses as rsps_lib

from ingestion.ingest_prices import (
    DEFAULT_LOOKBACK_DAYS,
    PricesIngestor,
)

_GROUPED_RE = re.compile(
    r"https://api\.polygon\.io/v2/aggs/grouped/locale/us/market/stocks/"
)
_SINGLE_RE = re.compile(
    r"https://api\.polygon\.io/v2/aggs/ticker/[^/]+/range/1/day/"
)


def _grouped_payload(*tickers: str, ts_ms: int = 1746662400000) -> dict:
    return {
        "queryCount": len(tickers),
        "resultsCount": len(tickers),
        "adjusted": True,
        "results": [
            {
                "T": t, "v": 1_000_000, "vw": 100.0,
                "o": 99.0, "c": 101.0, "h": 102.0, "l": 98.0,
                "t": ts_ms, "n": 5000,
            }
            for t in tickers
        ],
        "status": "OK",
    }


def _single_payload(ticker: str, ts_ms: int = 1746662400000) -> dict:
    return {
        "ticker": ticker,
        "queryCount": 1,
        "resultsCount": 1,
        "adjusted": True,
        "results": [
            {
                "v": 500_000, "vw": 50.0,
                "o": 49.5, "c": 50.5, "h": 51.0, "l": 49.0,
                "t": ts_ms, "n": 1000,
            }
        ],
        "status": "OK",
    }


@rsps_lib.activate
def test_ingest_dates_grouped_filters_to_universe(temp_db):
    """Grouped returns 5 tickers; universe is 2 of them; only those land."""
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json=_grouped_payload("AAPL", "MSFT", "GOOG", "AMZN", "NFLX"),
                 status=200, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,           # no throttling under test
        "polygon_retry_after_429_s": 0,    # no retry sleep under test
    })
    result = ing.ingest_dates(
        temp_db, run_id="r1",
        dates=[date(2026, 5, 8)],
        tickers=["AAPL", "MSFT"],
    )
    assert result["status"] == "ok"
    assert result["records"] == 2

    rows = temp_db.execute(
        "SELECT ticker, trade_date FROM prices_daily ORDER BY ticker"
    ).fetchall()
    assert [r[0] for r in rows] == ["AAPL", "MSFT"]


@rsps_lib.activate
def test_ingest_dates_grouped_non_trading_day_is_silent(temp_db):
    """Polygon returns 200 with empty results on weekends/holidays."""
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json={"resultsCount": 0, "results": [], "status": "OK"},
                 status=200, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,           # no throttling under test
        "polygon_retry_after_429_s": 0,    # no retry sleep under test
    })
    result = ing.ingest_dates(
        temp_db, run_id="r1",
        dates=[date(2026, 5, 9)],  # Saturday
        tickers=["AAPL"],
    )
    assert result["status"] == "ok"
    assert result["records"] == 0
    assert result["per_date"]["2026-05-09"]["grouped_status"] == 200
    assert result["per_date"]["2026-05-09"]["in_universe"] == 0


@rsps_lib.activate
def test_ingest_dates_fallback_runs_for_universe_tickers_missing_from_grouped(temp_db):
    """Grouped returns AAPL only; universe={AAPL, RKLB}; fallback fetches RKLB."""
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json=_grouped_payload("AAPL"),
                 status=200, match_querystring=False)
    rsps_lib.add(rsps_lib.GET, _SINGLE_RE,
                 json=_single_payload("RKLB"),
                 status=200, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,           # no throttling under test
        "polygon_retry_after_429_s": 0,    # no retry sleep under test
    })
    result = ing.ingest_dates(
        temp_db, run_id="r1",
        dates=[date(2026, 5, 8)],
        tickers=["AAPL", "RKLB"],
    )
    assert result["records"] == 2
    rows = temp_db.execute(
        "SELECT ticker FROM prices_daily ORDER BY ticker"
    ).fetchall()
    assert [r[0] for r in rows] == ["AAPL", "RKLB"]
    summary = result["per_date"]["2026-05-08"]
    assert summary["in_universe"] == 1
    assert summary["fallback_attempted"] == 1
    assert summary["fallback_inserted"] == 1


@rsps_lib.activate
def test_ingest_dates_fallback_is_capped(temp_db):
    """fallback_limit_per_date bounds the number of single-ticker calls.

    Grouped returns a non-empty payload (so we know it's a trading day),
    but no result is in the universe — every universe ticker is therefore
    missing and a candidate for fallback. The cap clamps the actual
    number of single-ticker calls.
    """
    # Grouped returns 1 ticker outside the universe — proves it's a
    # trading day (not the empty-results short-circuit).
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json=_grouped_payload("OUT_OF_UNIVERSE"),
                 status=200, match_querystring=False)
    # Catch-all single endpoint returns empty so fallback inserts nothing
    # but each call still counts as attempted.
    rsps_lib.add(rsps_lib.GET, _SINGLE_RE,
                 json={"resultsCount": 0, "results": [], "status": "OK"},
                 status=200, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,           # no throttling under test
        "polygon_retry_after_429_s": 0,    # no retry sleep under test
    })
    result = ing.ingest_dates(
        temp_db, run_id="r1",
        dates=[date(2026, 5, 8)],
        tickers=[f"T{i:03d}" for i in range(50)],  # 50 missing
        fallback_limit_per_date=3,
    )
    assert result["per_date"]["2026-05-08"]["fallback_attempted"] == 3


@rsps_lib.activate
def test_ingest_dates_idempotent(temp_db):
    """Running the same ingest twice does not duplicate rows."""
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json=_grouped_payload("AAPL"),
                 status=200, match_querystring=False)
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json=_grouped_payload("AAPL"),
                 status=200, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,           # no throttling under test
        "polygon_retry_after_429_s": 0,    # no retry sleep under test
    })
    ing.ingest_dates(temp_db, "r1", [date(2026, 5, 8)], ["AAPL"])
    ing.ingest_dates(temp_db, "r2", [date(2026, 5, 8)], ["AAPL"])

    n = temp_db.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker = 'AAPL'"
    ).fetchone()[0]
    assert n == 1


def test_ingest_without_api_key_skips(temp_db, monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    ing = PricesIngestor(cfg={})
    result = ing.ingest_dates(temp_db, "r1", [date(2026, 5, 8)], ["AAPL"])
    assert result["status"] == "skip"
    assert result["records"] == 0


# ──────────────────────────────────────────────────────────────────────
# KI-149: 403 + in_universe=0 → IngestionError (current-day blocked)
# ──────────────────────────────────────────────────────────────────────


@rsps_lib.activate
def test_ingest_dates_403_with_no_universe_coverage_raises_ingestion_error(temp_db):
    """KI-149: Polygon grouped 403 + in_universe=0 must surface as a distinct
    IngestionError, not be rolled into the generic 'failed' counter. This is
    the current-day blocked path on Polygon free tier.
    """
    from ingestion.ingest_prices import IngestionError

    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json={"error": "plan_limited"},
                 status=403, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,
        "polygon_retry_after_429_s": 0,
    })
    with pytest.raises(IngestionError) as exc_info:
        ing.ingest_dates(
            temp_db, run_id="r1",
            dates=[date(2026, 5, 13)],
            tickers=["AAPL", "MSFT"],
        )

    err = exc_info.value
    assert "2026-05-13" in str(err), (
        f"IngestionError must name the affected date; got {err!r}"
    )
    assert hasattr(err, "blocked_dates")
    assert date(2026, 5, 13) in err.blocked_dates


@rsps_lib.activate
def test_ingest_dates_403_on_one_date_still_raises_with_mixed_dates(temp_db):
    """Mixed batch: one date 200 OK, one date 403. The 403 still triggers
    IngestionError after the loop completes; the 200 date's rows are written.
    """
    from ingestion.ingest_prices import IngestionError

    # First call (most recent date) → 403
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json={"error": "plan_limited"},
                 status=403, match_querystring=False)
    # Second call (older date) → 200 with AAPL
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json=_grouped_payload("AAPL"),
                 status=200, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,
        "polygon_retry_after_429_s": 0,
    })
    with pytest.raises(IngestionError) as exc_info:
        ing.ingest_dates(
            temp_db, run_id="r1",
            dates=[date(2026, 5, 13), date(2026, 5, 12)],
            tickers=["AAPL"],
        )

    # Older date's row should still have been written before the raise.
    n = temp_db.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker = 'AAPL'"
    ).fetchone()[0]
    assert n == 1, "Pre-403 dates' rows must be persisted before the raise"
    assert date(2026, 5, 13) in exc_info.value.blocked_dates


@rsps_lib.activate
def test_ingest_dates_403_does_not_raise_when_unrelated_to_universe(temp_db):
    """Sanity: a non-403 failure (e.g. 500) still goes through the generic
    failed-counter path, NOT the IngestionError path. IngestionError is
    specifically the 403+empty-universe signature.
    """
    from ingestion.ingest_prices import IngestionError

    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json={"error": "internal"},
                 status=500, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,
        "polygon_retry_after_429_s": 0,
    })
    # 500 → no raise; counted in `failed` per existing contract.
    result = ing.ingest_dates(
        temp_db, run_id="r1",
        dates=[date(2026, 5, 13)],
        tickers=["AAPL"],
    )
    assert result["status"] == "ok"
    assert result["per_date"]["2026-05-13"]["grouped_status"] == 500


@rsps_lib.activate
def test_ingest_default_uses_lookback_days(temp_db):
    """ingest() fans out to DEFAULT_LOOKBACK_DAYS grouped calls."""
    rsps_lib.add(rsps_lib.GET, _GROUPED_RE,
                 json={"resultsCount": 0, "results": [], "status": "OK"},
                 status=200, match_querystring=False)

    ing = PricesIngestor(cfg={
        "polygon_api_key": "TEST_KEY",
        "polygon_throttle_s": 0,           # no throttling under test
        "polygon_retry_after_429_s": 0,    # no retry sleep under test
    })
    result = ing.ingest(temp_db, "r1", ["AAPL"])
    assert result["status"] == "ok"
    # One per-date entry per lookback day.
    assert len(result["per_date"]) == DEFAULT_LOOKBACK_DAYS
