"""Thin read-only Binance USDT-M PUBLIC client for the signal probe.

Self-contained on purpose: it wraps exactly the public endpoints the probe
needs (klines with full fields, premiumIndex, openInterest, openInterestHist,
depth), with the same ``time.sleep`` rate-limit pacing as the engine's
``BinanceClient``. No auth, no engine client, no production import beyond the
base URL / delay constants. Keeping it separate means the probe never touches
the live prediction/execution code path.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from crypto.research.signal_probe.config import (
    BINANCE_FUTURES_BASE, REQUEST_DELAY_S,
)

logger = logging.getLogger("mhde.crypto.signal_probe.client")


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class ProbeBinanceClient:
    """Read-only public-endpoint client with simple request pacing."""

    def __init__(self, delay: float = REQUEST_DELAY_S,
                 session: Optional[requests.Session] = None):
        self._delay = delay
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", "MHDE-signal-probe/1.0")

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        time.sleep(self._delay)
        resp = self._session.get(f"{BINANCE_FUTURES_BASE}{path}",
                                 params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # -- klines (full fields) --

    @staticmethod
    def _parse_kline(raw: list) -> dict:
        return {
            "open_time": _utc(raw[0]),
            "open": float(raw[1]),
            "high": float(raw[2]),
            "low": float(raw[3]),
            "close": float(raw[4]),
            "volume": float(raw[5]),
            "close_time": _utc(raw[6]),
            "quote_volume": float(raw[7]),
            "trades": int(raw[8]),
            "taker_buy_base": float(raw[9]),
            "taker_buy_quote": float(raw[10]),
        }

    def fetch_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        """Most-recent ``limit`` klines at ``interval`` (ascending open_time)."""
        data = self._get("/fapi/v1/klines",
                         {"symbol": symbol, "interval": interval, "limit": limit})
        return [self._parse_kline(r) for r in data]

    # -- funding / premium index (one call covers the whole universe) --

    def fetch_premium_index_all(self) -> dict[str, dict]:
        """``premiumIndex`` for every symbol, keyed by symbol (one request)."""
        data = self._get("/fapi/v1/premiumIndex")
        if isinstance(data, dict):  # single-symbol shape (defensive)
            data = [data]
        return {row["symbol"]: row for row in data}

    # -- open interest --

    def fetch_open_interest(self, symbol: str) -> Optional[float]:
        """Current real-time open interest (base) for ``symbol``."""
        data = self._get("/fapi/v1/openInterest", {"symbol": symbol})
        v = data.get("openInterest")
        return float(v) if v is not None else None

    def fetch_open_interest_hist(self, symbol: str, period: str,
                                 limit: int) -> list[float]:
        """``sumOpenInterest`` history (ascending) for ``symbol``."""
        data = self._get("/futures/data/openInterestHist",
                         {"symbol": symbol, "period": period, "limit": limit})
        return [float(r["sumOpenInterest"]) for r in data]

    # -- order-book depth --

    def fetch_depth(self, symbol: str, limit: int) -> dict:
        """Top-``limit`` order book (``bids`` / ``asks`` as [price, qty])."""
        return self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
