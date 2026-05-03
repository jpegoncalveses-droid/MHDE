from __future__ import annotations

import logging
import uuid
from datetime import datetime, date, timedelta

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.events")

_NASDAQ_BASE = "https://api.nasdaq.com/api/calendar/earnings"
# Fetch calendar for these offsets from today (days).  6 requests total, not 6 per ticker.
_DATE_OFFSETS = [-30, 0, 7, 14, 30, 60]


class EventsIngestor(BaseIngestor):
    source_name = "events"
    source_status = "experimental"

    def _fetch_calendar_for_date(self, target_date: str) -> dict[str, list[dict]]:
        """Fetch entire Nasdaq earnings calendar for one date. Returns {ticker: [events]}."""
        try:
            r = requests.get(
                _NASDAQ_BASE,
                params={"date": target_date},
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if r.status_code != 200:
                return {}
            rows = r.json().get("data", {}).get("rows", []) or []
            results: dict[str, list[dict]] = {}
            for item in rows:
                sym = (item.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                results.setdefault(sym, []).append({
                    "event_type": "earnings",
                    "event_date": target_date,
                    "title": f"Earnings: {sym}",
                })
            return results
        except Exception:
            return {}

    def ingest(self, conn, run_id, tickers):
        started = datetime.utcnow()
        inserted = 0
        ticker_set = set(tickers)

        # Fetch once per date (6 requests total) instead of once per ticker per date
        ticker_events: dict[str, list[dict]] = {}
        today = date.today()
        for delta in _DATE_OFFSETS:
            target_date = (today + timedelta(days=delta)).strftime("%Y-%m-%d")
            calendar = self._fetch_calendar_for_date(target_date)
            for sym, evs in calendar.items():
                if sym in ticker_set:
                    for ev in evs:
                        ev["is_upcoming"] = delta >= 0
                    ticker_events.setdefault(sym, []).extend(evs)

        rows_batch = []
        for ticker, events in ticker_events.items():
            for ev in events:
                ev_date_str = ev.get("event_date", "")
                ev_date = datetime.strptime(ev_date_str, "%Y-%m-%d").date() if ev_date_str else None
                rows_batch.append([
                    uuid.uuid4().hex[:16], ticker,
                    ev.get("event_type", "earnings"),
                    ev_date, ev.get("title"),
                    ev.get("is_upcoming", False),
                    run_id, datetime.utcnow(),
                ])

        if rows_batch:
            try:
                conn.executemany(
                    """
                    INSERT INTO events
                        (id, ticker, event_type, event_date, title,
                         source, is_upcoming, run_id, created_at)
                    VALUES (?, ?, ?, ?, ?, 'nasdaq_earnings', ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    rows_batch,
                )
                inserted = len(rows_batch)
            except Exception as exc:
                logger.warning("Events batch insert failed: %s", exc)

        self.log_run(conn, run_id, "earnings_calendar", "experimental",
                     inserted, inserted, 0, started_at=started)
        self.logger.info("[EXPERIMENTAL] Events: %d inserted for %d tickers",
                         inserted, len(ticker_events))
        return {"source": self.source_name, "status": "experimental", "records": inserted}
