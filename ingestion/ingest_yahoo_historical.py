"""Yahoo Finance historical OHLCV ingestor.

Replaces ingest_stooq_historical.py which broke when Stooq began requiring
API keys for their /q/d/l/ historical endpoint.

Bootstrap: fetches 1y of daily OHLCV for tickers with no price history.
Incremental: fetches since max_trade_date - INCREMENTAL_BUFFER for stale tickers.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.yahoo_historical")

_YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MHDE-Engine)",
    "Accept": "application/json",
}
_BOOTSTRAP_DAYS = 252
_INCREMENTAL_BUFFER = 5
_REQUEST_DELAY = 0.3
_FRESHNESS_DAYS = 3
_BOOTSTRAP_THRESHOLD = 65  # below this, always bootstrap (incremental on sparse history gives too few rows)


class YahooHistoricalIngestor(BaseIngestor):
    source_name = "yahoo_historical"
    source_status = "experimental"

    def _tickers_needing_history(self, conn, tickers: list[str]) -> list[tuple[str, date | None]]:
        threshold = (date.today() - timedelta(days=_FRESHNESS_DAYS)).isoformat()
        result = []
        for ticker in tickers:
            row = conn.execute(
                "SELECT COUNT(*), MAX(trade_date) FROM prices_daily WHERE ticker = ?",
                [ticker],
            ).fetchone()
            count = row[0] if row else 0
            max_date = row[1] if row else None
            if max_date and hasattr(max_date, "isoformat"):
                max_date_str = max_date.isoformat()
            else:
                max_date_str = str(max_date) if max_date else None
            if count >= _BOOTSTRAP_THRESHOLD and max_date_str and max_date_str >= threshold:
                continue
            if max_date and count >= _BOOTSTRAP_THRESHOLD:
                if not hasattr(max_date, "year"):
                    max_date = date.fromisoformat(str(max_date))
                since = max_date - timedelta(days=_INCREMENTAL_BUFFER)
            else:
                since = None  # bootstrap full year
            result.append((ticker, since))
        return result

    def ingest(self, conn, run_id: str, tickers: list[str]) -> dict:
        work = self._tickers_needing_history(conn, tickers)
        if not work:
            self.log_run(conn, run_id, "prices_daily", "skip", 0, 0, 0,
                         error_message="All tickers have sufficient recent price history")
            return {"source": self.source_name, "status": "skip", "records": 0}

        started = datetime.utcnow()
        total_inserted = 0
        now = datetime.utcnow()

        for i, (ticker, since_date) in enumerate(work):
            if since_date is None:
                url = f"{_YF_BASE}/{ticker}?range=1y&interval=1d"
            else:
                p1 = int(datetime.combine(since_date, datetime.min.time())
                         .replace(tzinfo=timezone.utc).timestamp())
                p2 = int(datetime.now(tz=timezone.utc).timestamp())
                url = f"{_YF_BASE}/{ticker}?period1={p1}&period2={p2}&interval=1d"

            try:
                r = requests.get(url, headers=_YF_HEADERS, timeout=20)
                if r.status_code != 200:
                    logger.debug("Yahoo hist %s for %s", r.status_code, ticker)
                else:
                    rows = _parse_yf_response(r.json(), ticker, run_id, now)
                    if rows:
                        conn.executemany(
                            """INSERT INTO prices_daily
                                (id, ticker, trade_date, open, high, low, close,
                                 volume, adjusted_close, source, run_id, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT (ticker, trade_date) DO NOTHING""",
                            rows,
                        )
                        total_inserted += len(rows)
            except Exception as exc:
                logger.warning("Yahoo historical fetch failed for %s: %s", ticker, exc)

            if i < len(work) - 1:
                time.sleep(_REQUEST_DELAY)

        self.log_run(conn, run_id, "prices_daily", "ok",
                     len(work), total_inserted, 0, started_at=started)
        self.logger.info("Yahoo historical: %d rows inserted for %d tickers",
                         total_inserted, len(work))
        return {"source": self.source_name, "status": "ok", "records": total_inserted}


def _parse_yf_response(data: dict, ticker: str, run_id: str, now: datetime) -> list[list]:
    rows = []
    try:
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return rows
        r = result[0]
        timestamps = r.get("timestamp") or []
        quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        for j, ts in enumerate(timestamps):
            try:
                close = closes[j] if j < len(closes) else None
                open_ = opens[j] if j < len(opens) else None
                high = highs[j] if j < len(highs) else None
                low = lows[j] if j < len(lows) else None
                vol = volumes[j] if j < len(volumes) else None
                if close is None:
                    continue
                trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                rows.append([
                    uuid.uuid4().hex[:16], ticker, trade_date,
                    open_, high, low, close,
                    int(vol) if vol is not None else None,
                    close, "yahoo", run_id, now,
                ])
            except (ValueError, TypeError, IndexError):
                continue
    except Exception as exc:
        logger.warning("Yahoo hist parse error for %s: %s", ticker, exc)
    return rows
