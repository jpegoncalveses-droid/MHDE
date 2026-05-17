"""Fear & Greed Index client (alternative.me public API).

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Sentiment
ingestion". Daily values, ~9 years of history available. No auth.

Endpoint: https://api.alternative.me/fng/?limit=N&format=json
  limit=0 returns full available history (oldest to newest? actually
  newest first — see test fixture). Response shape:
    {"data": [{"value": "53", "value_classification": "Neutral",
               "timestamp": "1735689600", "time_until_update": "..."},
              ...],
     "metadata": {"error": null | str}}
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import requests

logger = logging.getLogger("mhde.crypto.fear_greed_client")

ALTERNATIVE_ME_FNG_URL = "https://api.alternative.me/fng/"
REQUEST_DELAY_S = 0.5  # alternative.me asks for politeness on free tier


def parse_fng_row(raw: dict) -> dict:
    """Parse one alternative.me F&G row → MHDE-shape dict.

    Output keys: date (datetime.date UTC), value (int), value_classification
    (str | None).
    """
    ts = int(raw["timestamp"])
    return {
        "date": datetime.fromtimestamp(ts, tz=timezone.utc).date(),
        "value": int(raw["value"]),
        "value_classification": raw.get("value_classification"),
    }


class FearGreedClient:
    def __init__(self, delay: float = REQUEST_DELAY_S):
        self._delay = delay
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "MHDE/1.0"

    def _get(self, url: str, params: dict | None = None) -> dict:
        time.sleep(self._delay)
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _params(self, limit: int = 0) -> dict:
        return {"limit": str(limit), "format": "json"}

    def fetch_history(self, limit: int = 0) -> list[dict]:
        """Fetch F&G history. limit=0 = full available depth.

        Returns a list of parsed-row dicts, newest first (matches API order).
        """
        payload = self._get(ALTERNATIVE_ME_FNG_URL, params=self._params(limit))
        err = (payload.get("metadata") or {}).get("error")
        if err:
            raise RuntimeError(f"alternative.me F&G API error: {err}")
        data = payload.get("data", [])
        return [parse_fng_row(r) for r in data]
