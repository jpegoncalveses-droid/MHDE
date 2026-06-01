"""Shared Binance API client with rate limiting."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

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

    def _parse_intraday_kline(self, raw: list) -> dict:
        """Parse a raw Binance kline into an intraday OHLCV row.

        Unlike :meth:`_parse_kline` (which keys on the calendar ``trade_date``
        and stores quote-asset volume for the daily pipeline), this keeps the
        full ``open_time`` timestamp and base-asset volume — what the intraday
        replay needs.
        """
        open_time_ms = raw[0]
        return {
            "open_time": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc),
            "open": float(raw[1]),
            "high": float(raw[2]),
            "low": float(raw[3]),
            "close": float(raw[4]),
            "volume": float(raw[5]),
        }

    def fetch_klines(self, symbol: str, interval: str,
                     start_dt: datetime | None = None,
                     end_dt: datetime | None = None,
                     futures: bool = True) -> list[dict]:
        """Fetch paginated klines at an arbitrary ``interval`` (e.g. ``"1m"``).

        Paginates 1000 rows/request, advancing ``startTime`` past the last
        ``open_time`` until a short page is returned. Rate-limited via the
        shared ``_get`` delay. Returns intraday OHLCV rows (see
        :meth:`_parse_intraday_kline`). Used by the intraday-replay backfill;
        the existing daily/funding/OI methods are unaffected.
        """
        base = BINANCE_FUTURES_BASE if futures else BINANCE_SPOT_BASE
        endpoint = f"{base}/fapi/v1/klines" if futures else f"{base}/api/v3/klines"

        start_ms = int(start_dt.timestamp() * 1000) if start_dt else None
        end_ms = int(end_dt.timestamp() * 1000) if end_dt else None

        all_rows: list[dict] = []
        while True:
            params = self._klines_params(symbol, interval, limit=1000,
                                         start_ms=start_ms, end_ms=end_ms)
            data = self._get(endpoint, params)
            if not data:
                break
            for raw in data:
                all_rows.append(self._parse_intraday_kline(raw))
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
                onboard_ms = s.get("onboardDate")
                onboard_date = (
                    datetime.fromtimestamp(onboard_ms / 1000, tz=timezone.utc).date()
                    if onboard_ms else None
                )
                symbols.append({
                    "symbol": s["symbol"],
                    "base_asset": s["baseAsset"],
                    "onboard_date": onboard_date,
                })
        return symbols

    def fetch_24hr_tickers(self) -> list[dict]:
        endpoint = f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr"
        data = self._get(endpoint)
        return [{"symbol": t["symbol"], "quote_volume": float(t["quoteVolume"])} for t in data]

    def fetch_30d_avg_quote_volume_at(self, symbol: str, end_date: date,
                                      days: int = 30) -> float | None:
        """Point-in-time 30-day average quote-asset volume for ``symbol`` ending
        on ``end_date`` (UTC). Window: ``[end_date - days, end_date]`` inclusive.

        Used by the backfill of crypto_universe_ranking_buffer so hysteresis
        rules have a runway of historical daily rankings to evaluate. Returns
        ``None`` if Binance has no klines in the window.
        """
        endpoint = f"{BINANCE_FUTURES_BASE}/fapi/v1/klines"
        end_dt = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc)
        end_dt = end_dt + timedelta(days=1)
        end_ms = int(end_dt.timestamp() * 1000)
        start_ms = end_ms - (days + 1) * 24 * 3600 * 1000
        params = {
            "symbol": symbol,
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": days + 2,
        }
        klines = self._get(endpoint, params)
        if not klines:
            return None
        return sum(float(k[7]) for k in klines) / len(klines)

    def fetch_30d_avg_quote_volume(self, symbol: str, days: int = 30) -> float | None:
        """Average daily quote-asset volume over the last ``days`` daily klines.

        Replaces the 24h-snapshot proxy previously used by the universe
        builder (commit 9ec0044 mislabeled the persisted column
        ``avg_daily_volume_30d`` while populating it from a 24h ticker).
        Returns ``None`` when Binance has no klines for the symbol — caller
        must skip rather than treating as zero.
        """
        endpoint = f"{BINANCE_FUTURES_BASE}/fapi/v1/klines"
        end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        start_ms = end_ms - (days + 1) * 24 * 3600 * 1000
        params = {
            "symbol": symbol,
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": days + 2,
        }
        klines = self._get(endpoint, params)
        if not klines:
            return None
        qvs = [float(k[7]) for k in klines]
        return sum(qvs) / len(qvs)
