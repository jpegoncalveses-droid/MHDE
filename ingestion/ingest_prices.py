"""Polygon equity daily-bar ingestor.

Primary path uses the **grouped daily** endpoint
(`/v2/aggs/grouped/locale/us/market/stocks/{date}`) which returns
OHLCV for all US stocks in one call. Per-ticker fallback covers the
small set of universe tickers that aren't in the grouped feed (rare;
typically ADRs or thinly-traded names not on Polygon's main feed).

History: until 2026-05-09 this module looped per-ticker against
`/v2/aggs/ticker/{ticker}/range/1/day/...` once per call. With ~520
universe tickers and Polygon's free-tier 5 req/min limit, that took
~50 minutes per run and rate-limited heavily. KI-120 traced equity
volume thinning May 5-8 to that path. The grouped endpoint is one
call per date (~12k tickers in the response), inside free-tier
budget for both nightly use and multi-day backfill.

The fallback is bounded (`DEFAULT_FALLBACK_LIMIT` per date) to keep
overall API spend small even when a date has many universe tickers
missing from the grouped feed. Anything beyond the fallback budget
is left to the orchestrator's downstream Stooq/Yahoo stages.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.polygon")

_BASE = "https://api.polygon.io"
_GROUPED_URL_TMPL = (
    _BASE + "/v2/aggs/grouped/locale/us/market/stocks/{date}"
    "?adjusted=true&apiKey={key}"
)
_SINGLE_URL_TMPL = (
    _BASE + "/v2/aggs/ticker/{ticker}/range/1/day/{date}/{date}"
    "?adjusted=true&apiKey={key}"
)

_INSERT_SQL = """
INSERT INTO prices_daily
    (id, ticker, trade_date, open, high, low, close,
     volume, adjusted_close, source, run_id, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ticker, trade_date) DO NOTHING
"""


class IngestionError(RuntimeError):
    """Distinct ingestion failure mode for monitor surfacing.

    KI-149: Polygon's free-tier grouped endpoint returns HTTP 403 for the
    current trading day (and often T-1). The prior behavior logged a
    WARNING and rolled this into a generic "1 failed" counter, making it
    invisible to dashboards and downstream gates. This exception names the
    affected dates so callers and monitors can surface "current-day data
    unavailable" as a distinct failure rather than a transient blip.
    """

    def __init__(self, blocked_dates: list[date], status_code: int = 403):
        self.blocked_dates = list(blocked_dates)
        self.status_code = status_code
        dates_str = ", ".join(d.isoformat() for d in blocked_dates)
        super().__init__(
            f"Polygon grouped HTTP {status_code} with no universe coverage "
            f"on dates: {dates_str}. Current-day data unavailable (free-tier "
            f"plan limit). Affected dates will need fallback or paid-tier."
        )

# Default lookback for the nightly entry point. 7 calendar days covers
# the most recent trading week plus weekend. Idempotent INSERT means
# re-fetching already-stored dates is cheap.
DEFAULT_LOOKBACK_DAYS = 7

# Per-date cap on per-ticker fallback calls. Free tier is ~5 req/min,
# so a small cap keeps fallback within seconds. Anything beyond this
# falls through to the orchestrator's Stooq / Yahoo stages.
DEFAULT_FALLBACK_LIMIT = 10

# Free-tier rate limit is ~5 req/min. We sleep this many seconds
# between consecutive Polygon HTTP calls (grouped or single) to stay
# safely under the limit. 13s × 5 = 65s window — gives a buffer.
DEFAULT_THROTTLE_S = 13.0

# 429 (Too Many Requests) retry policy. Wait this many seconds and
# retry once before giving up on a date. Polygon doesn't always send
# Retry-After; this is a fixed-budget fallback.
RETRY_AFTER_429_S = 65.0


class PricesIngestor(BaseIngestor):
    source_name = "polygon"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        # Throttle and retry timings can be overridden in cfg for tests.
        self._throttle_s = float(cfg.get("polygon_throttle_s", DEFAULT_THROTTLE_S))
        self._retry_after_429_s = float(
            cfg.get("polygon_retry_after_429_s", RETRY_AFTER_429_S)
        )
        # Tracks the wall-clock time of the last Polygon HTTP call so
        # back-to-back calls space themselves out.
        self._last_call_at: float = 0.0

    def _api_key(self) -> str | None:
        return self.cfg.get("polygon_api_key") or os.environ.get("POLYGON_API_KEY")

    # ── HTTP helpers ─────────────────────────────────────────────────

    def _throttle(self) -> None:
        """Sleep just long enough since the last call to keep under the
        free-tier 5 req/min budget."""
        if self._throttle_s <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self._throttle_s:
            time.sleep(self._throttle_s - elapsed)
        self._last_call_at = time.monotonic()

    def _get_with_429_retry(self, url: str, timeout: int) -> requests.Response | None:
        """GET that retries once on 429 after a fixed cooldown. Returns
        None on transport-level failure."""
        self._throttle()
        try:
            r = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            self.logger.debug("Polygon GET failed: %s", exc)
            return None
        if r.status_code == 429 and self._retry_after_429_s > 0:
            wait_s = self._retry_after_429_s
            # Honor Retry-After header when present.
            ra = r.headers.get("Retry-After")
            if ra:
                try:
                    wait_s = max(wait_s, float(ra))
                except ValueError:
                    pass
            self.logger.warning(
                "Polygon 429 — sleeping %.0fs and retrying once", wait_s
            )
            time.sleep(wait_s)
            self._last_call_at = time.monotonic()
            try:
                r = requests.get(url, timeout=timeout)
            except requests.RequestException as exc:
                self.logger.debug("Polygon retry failed: %s", exc)
                return None
        return r

    def _fetch_grouped(self, api_key: str, d: date) -> tuple[list[dict], int]:
        """Fetch all US stocks' daily bars for `d`. Returns (results, status).
        Empty list on a 200 with no data (e.g. weekends, market holidays)."""
        url = _GROUPED_URL_TMPL.format(date=d.isoformat(), key=api_key)
        r = self._get_with_429_retry(url, timeout=30)
        if r is None:
            return [], -1
        if r.status_code != 200:
            return [], r.status_code
        try:
            payload = r.json()
        except ValueError:
            return [], -2
        return list(payload.get("results") or []), 200

    def _fetch_single(self, api_key: str, ticker: str, d: date) -> tuple[list[dict], int]:
        """Fallback: fetch a single ticker's bar for one date."""
        url = _SINGLE_URL_TMPL.format(ticker=ticker, date=d.isoformat(), key=api_key)
        r = self._get_with_429_retry(url, timeout=15)
        if r is None:
            return [], -1
        if r.status_code != 200:
            return [], r.status_code
        try:
            payload = r.json()
        except ValueError:
            return [], -2
        return list(payload.get("results") or []), 200

    # ── Bar → row conversion ─────────────────────────────────────────

    def _bar_to_row(
        self,
        bar: dict[str, Any],
        ticker: str,
        run_id: str,
        now: datetime,
    ) -> list[Any] | None:
        """Convert a Polygon bar dict to a prices_daily row. Returns None
        if the bar is missing required fields."""
        try:
            t_ms = bar["t"]
        except KeyError:
            return None
        try:
            trade_date = datetime.utcfromtimestamp(t_ms / 1000).date()
        except (TypeError, ValueError, OSError):
            return None
        return [
            uuid.uuid4().hex[:16],
            ticker,
            trade_date,
            bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"),
            bar.get("v"),
            bar.get("c"),  # adjusted_close: grouped endpoint returns
                           # split-adjusted close in `c` when ?adjusted=true.
            "polygon",
            run_id,
            now,
        ]

    # ── Public ingestion entry points ────────────────────────────────

    def ingest_dates(
        self,
        conn,
        run_id: str,
        dates: Iterable[date],
        tickers: list[str],
        fallback_limit_per_date: int = DEFAULT_FALLBACK_LIMIT,
    ) -> dict[str, Any]:
        """Fetch prices for each date in ``dates`` for tickers in ``tickers``.

        Strategy: grouped-daily call per date (one HTTP call returns ~12k
        US tickers); filter to ``tickers``; bounded per-ticker fallback
        for universe tickers absent from the grouped feed.

        Returns a summary dict with totals and per-date breakdown.
        Idempotent: relies on `prices_daily` PK (ticker, trade_date).
        """
        api_key = self._api_key()
        if not api_key:
            self.logger.warning("POLYGON_API_KEY not set — skipping price ingestion")
            self.log_run(conn, run_id, "prices", "skip", 0, 0, 0,
                         error_message="No API key")
            return {"source": self.source_name, "status": "skip", "records": 0}

        universe = {t.upper() for t in tickers}
        now = datetime.utcnow()
        started = now
        all_rows: list[list[Any]] = []
        attempted = failed = 0
        per_date: dict[str, dict[str, Any]] = {}
        blocked_403_dates: list[date] = []

        for d in dates:
            results, status = self._fetch_grouped(api_key, d)
            date_summary: dict[str, Any] = {
                "grouped_status": status,
                "grouped_total": len(results),
                "in_universe": 0,
                "fallback_attempted": 0,
                "fallback_inserted": 0,
            }
            if status != 200:
                self.logger.warning("Polygon grouped %s: HTTP %d", d, status)
                failed += 1
                if status == 403:
                    # KI-149: 403 + in_universe=0 is the plan-limited
                    # current-day signature. Track for the post-loop raise.
                    date_summary["current_day_blocked"] = True
                    blocked_403_dates.append(d)
                per_date[d.isoformat()] = date_summary
                continue
            if not results:
                # Non-trading day or empty payload — fine, move on.
                per_date[d.isoformat()] = date_summary
                continue

            fetched: set[str] = set()
            for bar in results:
                ticker = (bar.get("T") or "").upper()
                if not ticker or ticker not in universe:
                    continue
                row = self._bar_to_row(bar, ticker, run_id, now)
                if row is None:
                    failed += 1
                    continue
                all_rows.append(row)
                fetched.add(ticker)
                attempted += 1
            date_summary["in_universe"] = len(fetched)

            # Bounded per-ticker fallback for universe tickers missing
            # from the grouped feed.
            missing = sorted(universe - fetched)
            date_summary["missing_after_grouped"] = len(missing)
            for ticker in missing[:fallback_limit_per_date]:
                fb_results, fb_status = self._fetch_single(api_key, ticker, d)
                date_summary["fallback_attempted"] += 1
                if fb_status != 200 or not fb_results:
                    continue
                for bar in fb_results:
                    row = self._bar_to_row(bar, ticker, run_id, now)
                    if row is None:
                        failed += 1
                        continue
                    all_rows.append(row)
                    date_summary["fallback_inserted"] += 1
                    attempted += 1

            per_date[d.isoformat()] = date_summary

        inserted = 0
        if all_rows:
            conn.executemany(_INSERT_SQL, all_rows)
            inserted = len(all_rows)

        self.log_run(
            conn, run_id, "prices_daily", "ok",
            attempted, inserted, failed,
            started_at=started,
        )
        self.logger.info(
            "Prices: %d inserted across %d dates (%d attempted, %d failed)",
            inserted, len(per_date), attempted, failed,
        )
        for date_str, summary in per_date.items():
            self.logger.info("  %s: %s", date_str, summary)

        if blocked_403_dates:
            # KI-149: any 403 date with zero universe coverage indicates the
            # plan-limited current-day path. Raise after persisting all
            # successful dates so they aren't lost.
            raise IngestionError(blocked_403_dates, status_code=403)

        return {
            "source": self.source_name,
            "status": "ok",
            "records": inserted,
            "per_date": per_date,
        }

    def ingest(self, conn, run_id: str, tickers: list[str]) -> dict[str, Any]:
        """Default entry point used by the orchestrator. Fetches today plus
        the prior `DEFAULT_LOOKBACK_DAYS - 1` calendar days via grouped-daily.

        Re-fetching dates that are already in the DB is cheap (one grouped
        call returns ~12k bars; INSERT...ON CONFLICT DO NOTHING handles dedup).
        """
        today = datetime.utcnow().date()
        dates = [today - timedelta(days=i) for i in range(DEFAULT_LOOKBACK_DAYS)]
        return self.ingest_dates(conn, run_id, dates, tickers)
