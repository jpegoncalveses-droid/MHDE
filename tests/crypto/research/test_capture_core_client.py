"""Tests for the capture-core REST client (universe resolve + 429-aware GET)."""
from __future__ import annotations

import pytest

from crypto.research.capture_core import client as cc


class _Resp:
    def __init__(self, *, json_data=None, status=200, headers=None):
        self._json = json_data
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Returns scripted responses in order; records (path, params) calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


_EXCHANGE_INFO = {"symbols": [
    {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    {"symbol": "BTCUSDC", "contractType": "PERPETUAL", "quoteAsset": "USDC", "status": "TRADING"},  # not USDT
    {"symbol": "ETHUSDT_260626", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "status": "TRADING"},  # not perp
    {"symbol": "DEADUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "SETTLING"},  # not trading
]}


def test_universe_keeps_only_trading_usdt_perps_sorted():
    sess = _FakeSession([_Resp(json_data=_EXCHANGE_INFO)])
    client = cc.CaptureRestClient(session=sess, sleep_fn=lambda _s: None)
    assert client.fetch_usdtm_perp_universe() == ["BTCUSDT", "ETHUSDT"]


def test_get_retries_on_429_honoring_retry_after():
    slept = []
    sess = _FakeSession([
        _Resp(status=429, headers={"Retry-After": "2"}),
        _Resp(json_data=_EXCHANGE_INFO),
    ])
    client = cc.CaptureRestClient(session=sess, sleep_fn=slept.append, delay=0.0)
    out = client.fetch_usdtm_perp_universe()
    assert out == ["BTCUSDT", "ETHUSDT"]
    assert 2.0 in slept            # honored the Retry-After header
    assert len(sess.calls) == 2    # one retry


def test_get_raises_after_max_retries_of_429():
    sess = _FakeSession([_Resp(status=429, headers={"Retry-After": "0"})] * 10)
    client = cc.CaptureRestClient(session=sess, sleep_fn=lambda _s: None,
                                  delay=0.0, max_retries=3)
    with pytest.raises(Exception):
        client.fetch_usdtm_perp_universe()
    assert len(sess.calls) == 3
