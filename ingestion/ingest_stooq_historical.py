from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.stooq_historical")

_HIST_URL = "https://stooq.com/q/d/l/"
_BOOTSTRAP_DAYS = 252
_INCREMENTAL_BUFFER = 5   # refetch N days before max_trade_date to catch late-arriving data
_REQUEST_DELAY = 0.3      # 300ms between per-ticker requests
_FRESHNESS_DAYS = 3       # skip ticker if count >= 20 AND max_date within this window


class StooqHistoricalIngestor(BaseIngestor):
    """
    Fetches full daily OHLCV history from Stooq's /q/d/l/ endpoint.
    Bootstrap mode: 252 days for tickers with no existing price data.
    Incremental mode: since max_trade_date - INCREMENTAL_BUFFER for tickers
    that have data but are stale or sparse (< 20 rows or stale max_date).
    Runs after StooqPricesIngestor in the orchestrator.
    """
    source_name = "stooq_historical"
    source_status = "experimental"

    def _tickers_needing_history(
        self, conn, tickers: list[str]
    ) -> list[tuple[str, date | None]]:
        """Return (ticker, since_date) for tickers that need historical data.

        since_date is None for bootstrap (no existing rows), or the incremental
        start date (max_trade_date - INCREMENTAL_BUFFER) for tickers with rows.
        Tickers with count >= 20 AND max_date within FRESHNESS_DAYS are skipped.
        """
        threshold = (date.today() - timedelta(days=_FRESHNESS_DAYS)).isoformat()
        result = []
        for ticker in tickers:
            row = conn.execute(
                "SELECT COUNT(*), MAX(trade_date) FROM prices_daily WHERE ticker = ?",
                [ticker],
            ).fetchone()
            count = row[0] if row else 0
            max_date = row[1] if row else None
            if count >= 20 and max_date and max_date.isoformat() >= threshold:
                continue  # fresh enough
            if max_date:
                since = max_date - timedelta(days=_INCREMENTAL_BUFFER)
            else:
                since = date.today() - timedelta(days=_BOOTSTRAP_DAYS)
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
            since_str = since_date.strftime("%Y%m%d")
            url = f"{_HIST_URL}?s={ticker.lower()}.us&i=d&d1={since_str}"
            try:
                r = requests.get(url, timeout=20)
                if r.status_code != 200:
                    logger.debug("Stooq hist %s for %s", r.status_code, ticker)
                else:
                    rows = _parse_hist_csv(r.text, ticker, run_id, now)
                    if rows:
                        conn.executemany(
                            """
                            INSERT INTO prices_daily
                                (id, ticker, trade_date, open, high, low, close,
                                 volume, adjusted_close, source, run_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (ticker, trade_date) DO NOTHING
                            """,
                            rows,
                        )
                        total_inserted += len(rows)
            except Exception as exc:
                logger.warning("Stooq historical fetch failed for %s: %s", ticker, exc)

            if i < len(work) - 1:
                time.sleep(_REQUEST_DELAY)

        self.log_run(conn, run_id, "prices_daily", "ok",
                     len(work), total_inserted, 0,
                     started_at=started)
        self.logger.info(
            "Stooq historical: %d rows inserted for %d tickers",
            total_inserted, len(work),
        )
        return {"source": self.source_name, "status": "ok", "records": total_inserted}


def _parse_hist_csv(text: str, ticker: str, run_id: str, now: datetime) -> list[list]:
    """Parse Stooq /q/d/l/ CSV: Date,Open,High,Low,Close,Volume"""
    rows = []
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return rows
    for line in lines[1:]:  # skip header
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            trade_date = parts[0].strip()
            if not trade_date or trade_date.upper() == "N/D":
                continue
            open_ = float(parts[1])
            high = float(parts[2])
            low = float(parts[3])
            close = float(parts[4])
            volume = int(float(parts[5]))
            rows.append([
                uuid.uuid4().hex[:16], ticker, trade_date,
                open_, high, low, close, volume, close,
                "stooq", run_id, now,
            ])
        except (ValueError, IndexError):
            continue
    return rows
