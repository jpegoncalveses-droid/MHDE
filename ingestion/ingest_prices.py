from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.polygon")

_BASE = "https://api.polygon.io"


class PricesIngestor(BaseIngestor):
    source_name = "polygon"

    def _api_key(self) -> str | None:
        return self.cfg.get("polygon_api_key") or os.environ.get("POLYGON_API_KEY")

    def ingest(self, conn, run_id, tickers):
        api_key = self._api_key()
        if not api_key:
            self.logger.warning("POLYGON_API_KEY not set — skipping price ingestion")
            self.log_run(conn, run_id, "prices", "skip", 0, 0, 0,
                         error_message="No API key")
            return {"source": self.source_name, "status": "skip", "records": 0}

        started = datetime.utcnow()
        date_to = datetime.utcnow().date()
        date_from = date_to - timedelta(days=90)
        inserted = failed = attempted = 0

        for ticker in tickers:
            url = (
                f"{_BASE}/v2/aggs/ticker/{ticker}/range/1/day"
                f"/{date_from}/{date_to}"
                f"?adjusted=true&sort=asc&limit=120&apiKey={api_key}"
            )
            try:
                r = requests.get(url, timeout=30)
                if r.status_code == 403:
                    self.logger.warning("Polygon 403 for %s (paid tier required)", ticker)
                    failed += 1
                    continue
                if r.status_code != 200:
                    failed += 1
                    continue
                data = r.json()
                for bar in data.get("results", []):
                    attempted += 1
                    try:
                        trade_date = datetime.utcfromtimestamp(bar["t"] / 1000).date()
                        conn.execute(
                            """
                            INSERT INTO prices_daily
                                (id, ticker, trade_date, open, high, low, close,
                                 volume, adjusted_close, run_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (ticker, trade_date) DO NOTHING
                            """,
                            [
                                uuid.uuid4().hex[:16], ticker, trade_date,
                                bar.get("o"), bar.get("h"), bar.get("l"),
                                bar.get("c"), bar.get("v"), bar.get("c"),
                                run_id, datetime.utcnow(),
                            ],
                        )
                        inserted += 1
                    except Exception:
                        failed += 1
            except Exception as exc:
                logger.warning("Price fetch failed for %s: %s", ticker, exc)
                failed += 1

        self.log_run(conn, run_id, "prices_daily", "ok",
                     attempted, inserted, failed, started_at=started)
        self.logger.info("Prices: %d inserted for %d tickers", inserted, len(tickers))
        return {"source": self.source_name, "status": "ok", "records": inserted}
