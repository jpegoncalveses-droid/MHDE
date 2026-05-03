from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import date, datetime, timedelta

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.finra")

_BASE = "https://cdn.finra.org/equity/otcmarket/biweekly"
_FRESHNESS_DAYS = 14
# FINRA probes are slow (one HTTP request per ticker per date candidate).
# Cap at this many tickers by default; configurable via sources.finra.max_tickers.
_DEFAULT_MAX_TICKERS = 50


def _candidate_dates(n: int = 6) -> list[str]:
    today = date.today()
    dates = []
    for weeks_back in range(n * 2):
        d = today - timedelta(days=15 * weeks_back)
        dates.append(d.strftime("%Y%m%d"))
    return dates[:n * 2]


def _probe_url(ticker: str, date_str: str, timeout: int = 4) -> tuple[bool, bytes | None]:
    url = f"{_BASE}/{date_str}/CNMSshvol{ticker}.txt"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200 and len(r.content) > 100:
            return True, r.content
        return False, None
    except Exception:
        return False, None


class FINRAIngestor(BaseIngestor):
    source_name = "finra"

    def _is_fresh(self, conn, ticker: str) -> bool:
        cutoff = date.today() - timedelta(days=_FRESHNESS_DAYS)
        row = conn.execute(
            "SELECT MAX(created_at) FROM short_interest WHERE ticker = ?", [ticker]
        ).fetchone()
        if not row or not row[0]:
            return False
        last = row[0]
        if hasattr(last, "date"):
            last = last.date()
        return last >= cutoff

    def ingest(self, conn, run_id, tickers):
        started = datetime.utcnow()
        inserted = failed = attempted = 0
        skipped_fresh = 0

        incremental = self.cfg.get("ingestion", {}).get("incremental", True)
        max_tickers = self.cfg.get("sources", {}).get("sources", {}).get(
            "finra", {}
        ).get("max_tickers", _DEFAULT_MAX_TICKERS)
        tickers_to_check = tickers[:max_tickers]

        date_candidates = _candidate_dates(n=4)
        rows_batch: list[list] = []

        for ticker in tickers_to_check:
            if incremental and self._is_fresh(conn, ticker):
                skipped_fresh += 1
                continue

            for date_str in date_candidates:
                ok, content = _probe_url(ticker, date_str)
                if not ok or not content:
                    continue
                try:
                    text = content.decode("latin-1")
                    reader = csv.DictReader(io.StringIO(text), delimiter="|")
                    for row in reader:
                        attempted += 1
                        settle_date_str = row.get("SettlementDate", "").strip()
                        if not settle_date_str:
                            continue
                        try:
                            settle_date = datetime.strptime(settle_date_str, "%Y%m%d").date()
                            short_int = int(row.get("ShortInterest", "0") or 0)
                            avg_vol = int(row.get("AverageDailyVolume", "0") or 0)
                            dtc_raw = row.get("DaysToCover", "0") or "0"
                            dtc = float(dtc_raw) if dtc_raw.strip() else None
                            rows_batch.append([
                                uuid.uuid4().hex[:16], ticker, settle_date,
                                short_int, avg_vol, dtc, run_id, datetime.utcnow(),
                            ])
                        except Exception:
                            failed += 1
                except Exception as exc:
                    logger.debug("FINRA parse error for %s: %s", ticker, exc)
                break  # found data for this ticker

        if rows_batch:
            try:
                conn.executemany(
                    """
                    INSERT INTO short_interest
                        (id, ticker, settlement_date, short_interest,
                         avg_daily_volume, days_to_cover, run_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (ticker, settlement_date) DO NOTHING
                    """,
                    rows_batch,
                )
                inserted = len(rows_batch)
            except Exception as exc:
                logger.warning("FINRA batch insert failed: %s", exc)
                failed += len(rows_batch)

        if skipped_fresh:
            logger.info("FINRA: skipped %d tickers with recent data (<%dd)", skipped_fresh, _FRESHNESS_DAYS)

        self.log_run(conn, run_id, "short_interest", "ok",
                     attempted, inserted, failed, started_at=started)
        self.logger.info("FINRA: %d records inserted for %d tickers checked (of %d universe)",
                         inserted, len(tickers_to_check) - skipped_fresh, len(tickers))
        return {"source": self.source_name, "status": "ok", "records": inserted}
