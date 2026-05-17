"""Tests for Binance API client."""
from datetime import date, datetime

from crypto.ingestion.binance_client import BinanceClient


def test_klines_url_construction():
    client = BinanceClient()
    params = client._klines_params("BTCUSDT", "1d", limit=10)
    assert params["symbol"] == "BTCUSDT"
    assert params["interval"] == "1d"
    assert params["limit"] == 10


def test_klines_params_with_times():
    client = BinanceClient()
    params = client._klines_params("ETHUSDT", "1d", limit=500, start_ms=1000, end_ms=2000)
    assert params["startTime"] == 1000
    assert params["endTime"] == 2000


def test_parse_kline_row():
    client = BinanceClient()
    raw = [
        1609459200000,
        "29000.0",
        "29500.0",
        "28500.0",
        "29200.0",
        "1000.0",
        1609545599999,
        "29000000.0",
        5000,
        "600.0",
        "17400000.0",
        "0",
    ]
    parsed = client._parse_kline(raw)
    assert parsed["open"] == 29000.0
    assert parsed["close"] == 29200.0
    assert parsed["volume"] == 29000000.0
    assert parsed["trades"] == 5000
    assert parsed["taker_buy_volume"] == 17400000.0
    assert parsed["trade_date"] == date(2021, 1, 1)


def test_fetch_30d_avg_quote_volume_computes_average(monkeypatch):
    """Returns the arithmetic mean of quote-asset-volume (field [7]) across
    the returned daily klines."""
    client = BinanceClient()

    # Three daily klines with quote volumes 1M, 2M, 3M -> avg = 2M.
    def fake_get(self, url, params=None):
        return [
            [0, "0", "0", "0", "0", "0", 0, "1000000", 0, "0", "0", "0"],
            [0, "0", "0", "0", "0", "0", 0, "2000000", 0, "0", "0", "0"],
            [0, "0", "0", "0", "0", "0", 0, "3000000", 0, "0", "0", "0"],
        ]

    monkeypatch.setattr(BinanceClient, "_get", fake_get)
    avg = client.fetch_30d_avg_quote_volume("BTCUSDT")
    assert avg == 2_000_000.0


def test_fetch_30d_avg_quote_volume_returns_none_on_empty(monkeypatch):
    """If Binance returns no klines (delisted / very new), return None
    so build_universe can skip the coin rather than ranking it as zero."""
    client = BinanceClient()
    monkeypatch.setattr(BinanceClient, "_get", lambda self, url, params=None: [])
    assert client.fetch_30d_avg_quote_volume("DEADUSDT") is None


def test_fetch_30d_avg_quote_volume_uses_30_day_window(monkeypatch):
    """Verifies the helper requests daily interval for a ~30-day window
    so we're not accidentally pulling 4h candles or a 1-day window."""
    client = BinanceClient()
    captured = {}

    def fake_get(self, url, params=None):
        captured["url"] = url
        captured["params"] = params
        return [[0, "0", "0", "0", "0", "0", 0, "1000", 0, "0", "0", "0"]]

    monkeypatch.setattr(BinanceClient, "_get", fake_get)
    client.fetch_30d_avg_quote_volume("BTCUSDT")
    assert "klines" in captured["url"]
    assert captured["params"]["symbol"] == "BTCUSDT"
    assert captured["params"]["interval"] == "1d"
    # Window must cover at least 28 days; 32 is acceptable slack.
    span_days = (captured["params"]["endTime"] - captured["params"]["startTime"]) / 1000 / 86400
    assert 28 <= span_days <= 35


def test_parse_funding_rate():
    client = BinanceClient()
    raw = {
        "symbol": "BTCUSDT",
        "fundingRate": "0.00010000",
        "fundingTime": 1609459200000,
        "markPrice": "29000.0",
    }
    parsed = client._parse_funding_rate(raw)
    assert parsed["symbol"] == "BTCUSDT"
    assert abs(parsed["funding_rate"] - 0.0001) < 1e-10
    assert parsed["mark_price"] == 29000.0
    assert isinstance(parsed["funding_time"], datetime)
    assert parsed["funding_time"].year == 2021
