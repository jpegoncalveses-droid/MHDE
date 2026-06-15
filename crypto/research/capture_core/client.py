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
    BINANCE_FUTURES_BASE, DEPTH_SNAPSHOT_LIMIT, FAPI_WEIGHT_LIMIT, REQUEST_DELAY_S,
    REST_MAX_RETRIES,
)

logger = logging.getLogger("mhde.crypto.capture_core.client")

#: Status codes Binance uses for rate-limit / IP-ban backpressure.
_RATE_LIMIT_CODES = (429, 418)


class RateLimited(Exception):
    """Raised by :meth:`CaptureRestClient.get_with_weight` on 429/418 so the
    caller (the present-state collector) can DEGRADE rather than block-retry."""

    def __init__(self, status: int, retry_after: float):
        super().__init__(f"rate-limited HTTP {status}")
        self.status = status
        self.retry_after = retry_after


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

    def get_with_weight(self, path: str,
                        params: Optional[dict] = None) -> tuple[Any, Optional[int]]:
        """GET returning ``(json, used_weight)``.

        ``used_weight`` is the live ``X-MBX-USED-WEIGHT-1M`` for /fapi calls, or
        ``None`` for /futures/data (a separate pool that exposes no weight
        header). On 429/418 this raises :class:`RateLimited` immediately (no
        internal retry) so the present-state collector can degrade by priority
        instead of blocking.
        """
        if self._delay:
            self._sleep(self._delay)
        resp = self._session.get(f"{BINANCE_FUTURES_BASE}{path}",
                                 params=params, timeout=30)
        if resp.status_code in _RATE_LIMIT_CODES:
            wait = float(resp.headers.get("Retry-After", 1.0))
            raise RateLimited(resp.status_code, wait)
        resp.raise_for_status()
        used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        return resp.json(), (int(used) if used is not None else None)

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

    def fetch_request_weight_limit(self, *, fallback: int = FAPI_WEIGHT_LIMIT) -> int:
        """The live REQUEST_WEIGHT per-minute cap from ``exchangeInfo.rateLimits``.

        Used by the snapshot-owner (ADR-039) to size its weight budget. Returns
        ``fallback`` if the limit cannot be read (network error or unexpected shape),
        so the owner always has a safe cap to reserve headroom under.
        """
        try:
            data = self._get("/fapi/v1/exchangeInfo")
            for rl in data.get("rateLimits", []):
                if (rl.get("rateLimitType") == "REQUEST_WEIGHT"
                        and rl.get("interval") == "MINUTE"
                        and int(rl.get("intervalNum", 1)) == 1):
                    return int(rl["limit"])
        except Exception as exc:  # noqa: BLE001 - fall back to the documented cap
            logger.warning("capture-core could not read REQUEST_WEIGHT cap (%s: %s); "
                           "using fallback %d", type(exc).__name__, exc, fallback)
        return fallback
