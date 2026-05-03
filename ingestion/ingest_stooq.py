from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.stooq")

_QUOTE_URL = "https://stooq.com/q/l/"
_FRESHNESS_DAYS = 2
_BATCH_SIZE = 50
_REQUEST_DELAY = 0.15  # 150ms between batches — polite to Stooq


class StooqPricesIngestor(BaseIngestor):
    """
    Free latest-quote fallback using Stooq's /q/l/ endpoint.
    Returns today's OHLCV for each ticker — no API key required.
    Runs after Polygon to fill tickers that Polygon 403'd.
    Only provides single-day close; 52w-high and momentum features
    require accumulation of prices over multiple daily runs.
    """
    source_name = "stooq"
    source_status = "experimental"

    def _tickers_needing_prices(self, conn, tickers: list[str]) -> list[str]:
        threshold = (date.today() - timedelta(days=_FRESHNESS_DAYS)).isoformat()
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT ticker FROM prices_daily WHERE trade_date >= ?",
                [threshold],
            ).fetchall()
        }
        return [t for t in tickers if t not in existing]

    def ingest(self, conn, run_id: str, tickers: list[str]) -> dict:
        missing = self._tickers_needing_prices(conn, tickers)
        if not missing:
            self.log_run(conn, run_id, "prices_daily", "skip", 0, 0, 0,
                         error_message="All tickers have recent price data")
            return {"source": self.source_name, "status": "skip", "records": 0}

        started = datetime.utcnow()
        all_rows: list[list] = []
        failed = 0
        now = datetime.utcnow()

        for i in range(0, len(missing), _BATCH_SIZE):
            batch = missing[i:i + _BATCH_SIZE]
            symbols = " ".join(t.lower() + ".us" for t in batch)
            url = f"{_QUOTE_URL}?s={symbols}&f=sd2t2ohlcv&h&e=csv"
            try:
                r = requests.get(url, timeout=15)
                if r.status_code != 200:
                    logger.debug("Stooq %s for batch of %d", r.status_code, len(batch))
                    failed += len(batch)
                else:
                    rows = _parse_quote_csv(r.text, run_id, now)
                    all_rows.extend(rows)
                    failed += len(batch) - len(rows)
            except Exception as exc:
                logger.warning("Stooq batch failed: %s", exc)
                failed += len(batch)
            if i + _BATCH_SIZE < len(missing):
                time.sleep(_REQUEST_DELAY)

        inserted = 0
        if all_rows:
            conn.executemany(
                """
                INSERT INTO prices_daily
                    (id, ticker, trade_date, open, high, low, close,
                     volume, adjusted_close, source, run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticker, trade_date) DO NOTHING
                """,
                all_rows,
            )
            inserted = len(all_rows)

        self.log_run(conn, run_id, "prices_daily", "ok",
                     len(missing), inserted, failed,
                     started_at=started)
        self.logger.info(
            "Stooq: %d rows inserted for %d/%d tickers (failed=%d)",
            inserted, inserted, len(missing), failed,
        )
        return {"source": self.source_name, "status": "ok", "records": inserted}


def _parse_quote_csv(text: str, run_id: str, now: datetime) -> list[list]:
    """
    Parse Stooq /q/l/ CSV: Symbol,Date,Time,Open,High,Low,Close,Volume
    Symbol format: AAPL.US — strip .US suffix to get ticker.
    """
    rows = []
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return rows
    for line in lines[1:]:  # skip header
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            symbol = parts[0].strip()
            if not symbol or symbol.upper().endswith("N/D"):
                continue
            # Strip .US suffix
            ticker = symbol.upper()
            if ticker.endswith(".US"):
                ticker = ticker[:-3]
            trade_date = parts[1].strip()
            open_ = float(parts[3])
            high = float(parts[4])
            low = float(parts[5])
            close = float(parts[6])
            volume = int(float(parts[7]))
            rows.append([
                uuid.uuid4().hex[:16], ticker, trade_date,
                open_, high, low, close, volume, close,
                "stooq", run_id, now,
            ])
        except (ValueError, IndexError):
            continue
    return rows
