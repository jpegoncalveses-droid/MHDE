"""Read-only OKX v5 PUBLIC REST client for the Stage A collectors.

Same shape as :class:`capture_core.client.CaptureRestClient` so the imported
``RestPresentStateCollector`` drives it unchanged: ``get_with_weight`` returns
``(payload, None)`` — OKX exposes no used-weight header; ``None`` keeps the
collector's /fapi budget logic permanently idle, and pacing is the client's
fixed inter-request delay (per-endpoint OKX buckets are generous and mostly
per-instId; see okx config).

Two synthetic COMPOSITE endpoints serve the series that have no single OKX
call (recon Q1): ``join:premium_index`` (mark-price + index-tickers +
funding-rate ANY) and ``join:basis`` (tickers + index-tickers). The composite
is fetched here, joined in the series parser — the collector still sees one
(endpoint, payload) pair per poll.

No auth anywhere (all endpoints keyless, live-verified 2026-07-03). NEVER
opens mhde.duckdb or the engine DB.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

import requests

from crypto.research.capture_core_okx import config as cfg
from crypto.research.capture_core_okx.symbols import filter_universe

logger = logging.getLogger("mhde.crypto.capture_core_okx.client")

#: Synthetic composite endpoints -> the real GETs behind them.
_JOINS: dict[str, list[tuple[str, str, dict]]] = {
    "join:premium_index": [
        ("mark", "/api/v5/public/mark-price", {"instType": "SWAP"}),
        ("index", "/api/v5/market/index-tickers", {"quoteCcy": "USDT"}),
        ("funding", "/api/v5/public/funding-rate", {"instId": "ANY"}),
    ],
    "join:basis": [
        ("tickers", "/api/v5/market/tickers", {"instType": "SWAP"}),
        ("index", "/api/v5/market/index-tickers", {"quoteCcy": "USDT"}),
    ],
}


class OkxRestClient:
    """Paced public-endpoint client with OKX envelope + 429 handling."""

    def __init__(
        self,
        *,
        delay: float = cfg.REQUEST_DELAY_S,
        session: Optional[requests.Session] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_retries: int = cfg.REST_MAX_RETRIES,
    ) -> None:
        self._delay = delay
        self._session = session or requests.Session()
        self._sleep = sleep_fn
        self._max_retries = max_retries
        self._session.headers.setdefault("User-Agent", "MHDE-capture-okx/1.0")
        #: HTTP requests actually issued (composite fetches count each leg) —
        #: the gate/ops signal for one poll cycle's request budget.
        self.requests_made = 0

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET returning the OKX ``data`` payload; raises on envelope errors."""
        url = f"{cfg.OKX_REST_BASE}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            if self._delay:
                self._sleep(self._delay)
            self.requests_made += 1
            resp = self._session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning("capture-okx REST %s -> 429; backing off %.1fs "
                               "(attempt %d/%d)", path, wait, attempt,
                               self._max_retries)
                self._sleep(wait)
                last_exc = RuntimeError("rate-limited HTTP 429")
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") not in ("0", 0):
                raise RuntimeError(
                    f"OKX {path} code={body.get('code')} msg={body.get('msg')}")
            return body.get("data", [])
        raise last_exc or RuntimeError(f"GET {path} exhausted retries")

    def get_with_weight(self, path: str,
                        params: Optional[dict] = None) -> tuple[Any, Optional[int]]:
        """Collector-facing GET: ``(payload, None)``; composites fan out here."""
        join = _JOINS.get(path)
        if join is None:
            return self._get(path, params), None
        return {key: self._get(p, dict(q)) for key, p, q in join}, None

    def fetch_okx_linear_usdt_universe(self) -> list[str]:
        """Sorted instIds of every live, crypto, USDT-margined linear perp."""
        return filter_universe(self._get("/api/v5/public/instruments",
                                         {"instType": "SWAP"}))
