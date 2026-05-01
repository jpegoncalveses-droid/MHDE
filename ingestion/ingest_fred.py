from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.fred")

_BASE = "https://api.stlouisfed.org/fred"
_SERIES = {
    "FEDFUNDS": "Federal Funds Rate",
    "DGS10": "10-Year Treasury Yield",
    "CPIAUCSL": "CPI All Items",
    "UNRATE": "Unemployment Rate",
    "PAYEMS": "Nonfarm Payrolls",
    "GDP": "Real GDP",
}


class FREDIngestor(BaseIngestor):
    source_name = "fred"

    def _api_key(self) -> str | None:
        return self.cfg.get("fred_api_key") or os.environ.get("FRED_API_KEY")

    def ingest(self, conn, run_id, tickers):
        api_key = self._api_key()
        if not api_key:
            self.logger.warning("FRED_API_KEY not set — skipping macro ingestion")
            self.log_run(conn, run_id, "macro_series", "skip", 0, 0, 0,
                         error_message="No API key")
            return {"source": self.source_name, "status": "skip", "records": 0}

        started = datetime.utcnow()
        inserted = failed = 0

        for series_id, series_name in _SERIES.items():
            url = (
                f"{_BASE}/series/observations"
                f"?series_id={series_id}&api_key={api_key}&file_type=json"
                f"&sort_order=desc&limit=12"
            )
            try:
                r = requests.get(url, timeout=30)
                if r.status_code != 200:
                    failed += 1
                    continue
                obs = r.json().get("observations", [])
                for ob in obs:
                    val = ob.get("value")
                    if val == ".":
                        continue
                    try:
                        date_str = ob.get("date", "")
                        as_of = datetime.strptime(date_str, "%Y-%m-%d").date()
                        conn.execute(
                            """
                            INSERT INTO macro_series
                                (id, series_id, series_name, value, as_of_date,
                                 source, run_id, created_at)
                            VALUES (?, ?, ?, ?, ?, 'fred', ?, ?)
                            ON CONFLICT (series_id, as_of_date) DO NOTHING
                            """,
                            [
                                uuid.uuid4().hex[:16], series_id, series_name,
                                float(val), as_of, run_id, datetime.utcnow(),
                            ],
                        )
                        inserted += 1
                    except Exception:
                        failed += 1
            except Exception as exc:
                logger.warning("FRED %s fetch failed: %s", series_id, exc)
                failed += 1

        self.log_run(conn, run_id, "macro_series", "ok",
                     inserted + failed, inserted, failed, started_at=started)
        self.logger.info("FRED: %d observations inserted", inserted)
        return {"source": self.source_name, "status": "ok", "records": inserted}
