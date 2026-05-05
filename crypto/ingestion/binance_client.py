"""Shared Binance API client with rate limiting."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import requests

from crypto.config import BINANCE_FUTURES_BASE, BINANCE_SPOT_BASE, REQUEST_DELAY_S

logger = logging.getLogger("mhde.crypto.binance")


class BinanceClient:
    def __init__(self, delay: float = REQUEST_DELAY_S):
        self._delay = delay
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "MHDE/1.0"

    def _get(self, url: str, params: dict | None = None) -> list | dict:
        time.sleep(self._delay)
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # -- Klines (OHLCV) --

    def _klines_params(self, symbol: str, interval: str, limit: int = 1000,
                       start_ms: int | None = None, end_ms: int | None = None) -> dict:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return params

    def _parse_kline(self, raw: list) -> dict:
        open_time_ms = raw[0]
        return {
            "trade_date": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date(),
            "open": float(raw[1]),
            "high": float(raw[2]),
            "low": float(raw[3]),
            "close": float(raw[4]),
            "volume": float(raw[7]),
            "trades": int(raw[8]),
            "taker_buy_volume": float(raw[10]),
        }

    def fetch_daily_klines(self, symbol: str, start_date: date | None = None,
                           end_date: date | None = None, futures: bool = True) -> list[dict]:
        base = BINANCE_FUTURES_BASE if futures else BINANCE_SPOT_BASE
        endpoint = f"{base}/fapi/v1/klines" if futures else f"{base}/api/v3/klines"

        start_ms = int(datetime.combine(start_date, datetime.min.time()).timestamp() * 1000) if start_date else None
        end_ms = int(datetime.combine(end_date, datetime.max.time()).timestamp() * 1000) if end_date else None

        all_rows = []
        while True:
            params = self._klines_params(symbol, "1d", limit=1000, start_ms=start_ms, end_ms=end_ms)
            data = self._get(endpoint, params)
            if not data:
                break
            for raw in data:
                all_rows.append(self._parse_kline(raw))
            if len(data) < 1000:
                break
            start_ms = data[-1][0] + 1

        return all_rows

    # -- Funding rates --

    def _parse_funding_rate(self, raw: dict) -> dict:
        return {
            "symbol": raw["symbol"],
            "funding_time": datetime.fromtimestamp(raw["fundingTime"] / 1000, tz=timezone.utc),
            "funding_rate": float(raw["fundingRate"]),
            "mark_price": float(raw.get("markPrice", 0)),
        }

    def fetch_funding_rates(self, symbol: str, start_date: date | None = None,
                            end_date: date | None = None) -> list[dict]:
        endpoint = f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate"
        start_ms = int(datetime.combine(start_date, datetime.min.time()).timestamp() * 1000) if start_date else None
        end_ms = int(datetime.combine(end_date, datetime.max.time()).timestamp() * 1000) if end_date else None

        all_rows = []
        while True:
            params = {"symbol": symbol, "limit": 1000}
            if start_ms is not None:
                params["startTime"] = start_ms
            if end_ms is not None:
                params["endTime"] = end_ms
            data = self._get(endpoint, params)
            if not data:
                break
            for raw in data:
                all_rows.append(self._parse_funding_rate(raw))
            if len(data) < 1000:
                break
            start_ms = data[-1]["fundingTime"] + 1

        return all_rows

    # -- Open Interest --

    def fetch_open_interest_hist(self, symbol: str, period: str = "1d",
                                 limit: int = 30) -> list[dict]:
        endpoint = f"{BINANCE_FUTURES_BASE}/futures/data/openInterestHist"
        params = {"symbol": symbol, "period": period, "limit": limit}
        data = self._get(endpoint, params)
        rows = []
        for item in data:
            rows.append({
                "symbol": symbol,
                "trade_date": datetime.fromtimestamp(item["timestamp"] / 1000, tz=timezone.utc).date(),
                "open_interest": float(item["sumOpenInterest"]),
                "open_interest_value": float(item["sumOpenInterestValue"]),
            })
        return rows

    # -- Exchange info (for universe) --

    def fetch_futures_exchange_info(self) -> list[dict]:
        endpoint = f"{BINANCE_FUTURES_BASE}/fapi/v1/exchangeInfo"
        data = self._get(endpoint)
        symbols = []
        for s in data.get("symbols", []):
            if (s.get("contractType") == "PERPETUAL"
                    and s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"):
                symbols.append({
                    "symbol": s["symbol"],
                    "base_asset": s["baseAsset"],
                })
        return symbols

    def fetch_24hr_tickers(self) -> list[dict]:
        endpoint = f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr"
        data = self._get(endpoint)
        return [{"symbol": t["symbol"], "quote_volume": float(t["quoteVolume"])} for t in data]
