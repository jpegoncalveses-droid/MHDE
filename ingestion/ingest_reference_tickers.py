"""Cross-asset reference ticker ingestor.

Fetches SPY, VIX, and sector ETFs from Yahoo Finance unconditionally —
bypasses the universe lookup that drives the per-ticker ingestors
(Polygon / Stooq / YahooHistorical). These tickers are consumed by
ml/features.py (`return_vs_spy_*`, `return_vs_sector_*`, `beta_60d`,
`vix_level`, `vix_change_5d`) but are NOT present in `companies`, so the
universe-driven path never sees them.

See data/processed/finding1_cross_asset_ingestion_root_cause.md.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

import requests

from ingestion.base_ingestor import BaseIngestor
from ingestion.ingest_yahoo_historical import _parse_yf_response

logger = logging.getLogger("mhde.ingestion.reference_tickers")

REFERENCE_TICKERS: tuple[str, ...] = (
    "SPY", "VIX",
    "XLK", "XLF", "XLV", "XLE", "XLY",
    "XLI", "XLP", "XLB", "XLU", "XLRE",
)

_YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MHDE-Engine)",
    "Accept": "application/json",
}
_REQUEST_DELAY = 0.3
_RETRY_DELAYS = (30, 60)


class ReferenceTickersIngestor(BaseIngestor):
    source_name = "reference_tickers"
    source_status = "active"

    def ingest(self, conn, run_id: str, tickers: list[str]) -> dict:
        started = datetime.utcnow()
        total_inserted = 0
        now = datetime.utcnow()

        for i, ticker in enumerate(REFERENCE_TICKERS):
            url = f"{_YF_BASE}/{ticker}?range=1y&interval=1d"
            try:
                r = requests.get(url, headers=_YF_HEADERS, timeout=20)
                for backoff in _RETRY_DELAYS:
                    if r.status_code != 429:
                        break
                    self.logger.warning("Yahoo 429 for %s — retrying in %ds", ticker, backoff)
                    time.sleep(backoff)
                    r = requests.get(url, headers=_YF_HEADERS, timeout=20)
                if r.status_code != 200:
                    self.logger.warning("Yahoo %s for reference ticker %s", r.status_code, ticker)
                    continue
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
                self.logger.warning("Reference ticker fetch failed for %s: %s", ticker, exc)

            if i < len(REFERENCE_TICKERS) - 1:
                time.sleep(_REQUEST_DELAY)

        self.log_run(conn, run_id, "prices_daily", "ok",
                     len(REFERENCE_TICKERS), total_inserted, 0, started_at=started)
        self.logger.info("Reference tickers: %d rows inserted across %d tickers",
                         total_inserted, len(REFERENCE_TICKERS))
        return {"source": self.source_name, "status": "ok", "records": total_inserted}
