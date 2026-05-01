from __future__ import annotations

import logging
import uuid
from datetime import datetime, date, timedelta

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.events")

_NASDAQ_BASE = "https://api.nasdaq.com/api/calendar/earnings"


class EventsIngestor(BaseIngestor):
    source_name = "events"
    source_status = "experimental"

    def _fetch_earnings(self, ticker: str) -> list[dict]:
        results = []
        for delta in [-30, 0, 7, 14, 30, 60]:
            target_date = (date.today() + timedelta(days=delta)).strftime("%Y-%m-%d")
            try:
                r = requests.get(
                    _NASDAQ_BASE,
                    params={"date": target_date},
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                data = r.json().get("data", {}).get("rows", [])
                for item in data:
                    sym = (item.get("symbol") or "").upper().strip()
                    if sym == ticker:
                        results.append({
                            "ticker": ticker,
                            "event_type": "earnings",
                            "event_date": target_date,
                            "title": f"Earnings: {ticker}",
                            "is_upcoming": delta >= 0,
                        })
            except Exception:
                pass
        return results

    def ingest(self, conn, run_id, tickers):
        started = datetime.utcnow()
        inserted = 0
        max_tickers = min(len(tickers), 50)  # limit to avoid hammering

        for ticker in tickers[:max_tickers]:
            events = self._fetch_earnings(ticker)
            for ev in events:
                try:
                    ev_date_str = ev.get("event_date", "")
                    ev_date = datetime.strptime(ev_date_str, "%Y-%m-%d").date() if ev_date_str else None
                    conn.execute(
                        """
                        INSERT INTO events
                            (id, ticker, event_type, event_date, title,
                             source, is_upcoming, run_id, created_at)
                        VALUES (?, ?, ?, ?, ?, 'nasdaq_earnings', ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        """,
                        [
                            uuid.uuid4().hex[:16], ticker,
                            ev.get("event_type", "earnings"),
                            ev_date, ev.get("title"),
                            ev.get("is_upcoming", False),
                            run_id, datetime.utcnow(),
                        ],
                    )
                    inserted += 1
                except Exception:
                    pass

        self.log_run(conn, run_id, "earnings_calendar", "experimental",
                     inserted, inserted, 0, started_at=started)
        self.logger.info("[EXPERIMENTAL] Events: %d inserted for %d tickers",
                         inserted, max_tickers)
        return {"source": self.source_name, "status": "experimental", "records": inserted}
