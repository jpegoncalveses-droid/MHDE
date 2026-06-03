"""Read-only Binance USDT-M PUBLIC REST client for capture-core.

Wraps exactly the REST endpoints capture-core needs — ``exchangeInfo`` (to
resolve the TRADING USDT-M perp universe) and ``depth`` (order-book snapshot
seeding, used in PR-2) — with the same ``time.sleep`` pacing as the signal-probe
client, **plus explicit 429/418 handling** that honors ``Retry-After``. No auth,
no engine client.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

import requests

from crypto.research.capture_core.config import (
    BINANCE_FUTURES_BASE, DEPTH_SNAPSHOT_LIMIT, REQUEST_DELAY_S, REST_MAX_RETRIES,
)

logger = logging.getLogger("mhde.crypto.capture_core.client")

#: Status codes Binance uses for rate-limit / IP-ban backpressure.
_RATE_LIMIT_CODES = (429, 418)


class CaptureRestClient:
    """Public-endpoint client with request pacing and 429/418 backoff."""

    def __init__(
        self,
        *,
        delay: float = REQUEST_DELAY_S,
        session: Optional[requests.Session] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_retries: int = REST_MAX_RETRIES,
    ) -> None:
        self._delay = delay
        self._session = session or requests.Session()
        self._sleep = sleep_fn
        self._max_retries = max_retries
        self._session.headers.setdefault("User-Agent", "MHDE-capture-core/1.0")

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{BINANCE_FUTURES_BASE}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            if self._delay:
                self._sleep(self._delay)
            resp = self._session.get(url, params=params, timeout=30)
            if resp.status_code in _RATE_LIMIT_CODES:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning("capture-core REST %s -> %s; backing off %.1fs "
                               "(attempt %d/%d)", path, resp.status_code, wait,
                               attempt, self._max_retries)
                self._sleep(wait)
                last_exc = RuntimeError(f"rate-limited HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            return resp.json()
        raise last_exc or RuntimeError(f"GET {path} exhausted retries")

    def fetch_usdtm_perp_universe(self) -> list[str]:
        """Sorted symbols for every TRADING USDT-M PERPETUAL contract."""
        data = self._get("/fapi/v1/exchangeInfo")
        return sorted(
            s["symbol"] for s in data.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        )

    def fetch_depth_snapshot(self, symbol: str,
                             limit: int = DEPTH_SNAPSHOT_LIMIT) -> dict:
        """Order-book snapshot (``lastUpdateId`` + bids/asks) for seeding (PR-2)."""
        return self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
