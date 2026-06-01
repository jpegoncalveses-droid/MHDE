"""Tests for BinanceClient.fetch_klines — generic-interval paginated fetch.

Additive method (the daily/funding/OI methods are untouched). The
pagination loop (1000 rows/request, advance startTime past the last
open_time) is exercised with a stubbed ``_get`` so no network is hit.
"""
from __future__ import annotations

from datetime import datetime, timezone

from crypto.ingestion.binance_client import BinanceClient


def _raw(open_time_ms, base=100.0):
    # Binance kline array: [openTime, open, high, low, close, volume,
    # closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]
    return [
        open_time_ms, f"{base}", f"{base + 1}", f"{base - 1}", f"{base + 0.5}",
        "12.5", open_time_ms + 59_999, "1250.0", 7, "6.0", "600.0", "0",
    ]


def _install_stub(client, pages):
    """Stub ``_get`` to return successive ``pages`` and record params."""
    calls = []
    seq = list(pages)

    def fake_get(url, params=None):
        calls.append(params)
        return seq.pop(0) if seq else []

    client._get = fake_get  # type: ignore[assignment]
    return calls


def test_fetch_klines_parses_one_page():
    client = BinanceClient(delay=0)
    t0 = int(datetime(2026, 2, 7, 0, 45, tzinfo=timezone.utc).timestamp() * 1000)
    _install_stub(client, [[_raw(t0), _raw(t0 + 60_000)]])
    rows = client.fetch_klines("BTCUSDT", "1m")
    assert len(rows) == 2
    assert rows[0]["open_time"] == datetime(2026, 2, 7, 0, 45, tzinfo=timezone.utc)
    assert rows[0]["open"] == 100.0
    assert rows[0]["high"] == 101.0
    assert rows[0]["low"] == 99.0
    assert rows[0]["close"] == 100.5
    assert rows[0]["volume"] == 12.5


def test_fetch_klines_paginates_until_short_page():
    client = BinanceClient(delay=0)
    t0 = int(datetime(2026, 2, 7, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    full = [_raw(t0 + i * 60_000) for i in range(1000)]        # exactly 1000 → continue
    tail = [_raw(t0 + (1000 + i) * 60_000) for i in range(3)]  # 3 → stop
    calls = _install_stub(client, [full, tail])
    rows = client.fetch_klines("BTCUSDT", "1m")
    assert len(rows) == 1003
    # Two requests: the second advances startTime past the last open_time.
    assert len(calls) == 2
    assert calls[1]["startTime"] == full[-1][0] + 1


def test_fetch_klines_empty_first_page_returns_empty():
    client = BinanceClient(delay=0)
    _install_stub(client, [[]])
    assert client.fetch_klines("BTCUSDT", "1m") == []


def test_fetch_klines_passes_interval_and_symbol():
    client = BinanceClient(delay=0)
    calls = _install_stub(client, [[]])
    client.fetch_klines("ETHUSDT", "1m")
    assert calls[0]["symbol"] == "ETHUSDT"
    assert calls[0]["interval"] == "1m"
    assert calls[0]["limit"] == 1000
