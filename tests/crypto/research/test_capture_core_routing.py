"""Tests for split-endpoint routing (Binance /public vs /market, 2026-04-23).

PUBLIC: <sym>@bookTicker, !bookTicker, <sym>@depth*
MARKET: aggTrade, markPrice (per-symbol + array), forceOrder (per-symbol + array)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import conn_manager as cm


# -- classify_endpoint --

@pytest.mark.parametrize("stream", [
    "btcusdt@bookTicker", "!bookTicker", "btcusdt@depth@100ms", "btcusdt@depth",
])
def test_classify_public(stream):
    assert cfg.classify_endpoint(stream) == "public"


@pytest.mark.parametrize("stream", [
    "btcusdt@aggTrade", "btcusdt@markPrice@1s", "!markPrice@arr@1s",
    "btcusdt@forceOrder", "!forceOrder@arr",
])
def test_classify_market(stream):
    assert cfg.classify_endpoint(stream) == "market"


# -- plan_shards: grouping + base assignment + global-unique indices --

def test_plan_shards_groups_by_endpoint_with_correct_base():
    streams = ["btcusdt@aggTrade", "btcusdt@depth@100ms", "btcusdt@bookTicker",
               "!markPrice@arr@1s", "!forceOrder@arr"]
    plan = cm.plan_shards(streams, per_conn=100,
                          classify=cfg.classify_endpoint,
                          public_base=cfg.WS_PUBLIC_BASE,
                          market_base=cfg.WS_MARKET_BASE)
    by_base = {base: shard for _, base, shard in plan}
    assert set(by_base[cfg.WS_PUBLIC_BASE]) == {"btcusdt@depth@100ms", "btcusdt@bookTicker"}
    assert set(by_base[cfg.WS_MARKET_BASE]) == {
        "btcusdt@aggTrade", "!markPrice@arr@1s", "!forceOrder@arr"}


def test_plan_shards_indices_globally_unique_across_groups():
    # 3 public + 3 market streams, per_conn=2 -> 2 public shards + 2 market shards
    streams = (["s%d@bookTicker" % i for i in range(3)]
               + ["s%d@aggTrade" % i for i in range(3)])
    plan = cm.plan_shards(streams, per_conn=2, classify=cfg.classify_endpoint,
                          public_base=cfg.WS_PUBLIC_BASE,
                          market_base=cfg.WS_MARKET_BASE)
    idxs = [idx for idx, _, _ in plan]
    assert idxs == list(range(len(plan)))      # contiguous + unique across groups
    assert len(plan) == 4


def test_plan_shards_empty_group_omitted():
    plan = cm.plan_shards(["btcusdt@bookTicker"], per_conn=100,
                          classify=cfg.classify_endpoint,
                          public_base=cfg.WS_PUBLIC_BASE,
                          market_base=cfg.WS_MARKET_BASE)
    assert len(plan) == 1
    assert plan[0][1] == cfg.WS_PUBLIC_BASE


# -- end-to-end: the manager connects each stream on the right base --

class _FakeConn:
    def __init__(self, frame):
        self._frames = [frame]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        raise ConnectionError("closed")


def _frame(stream):
    return json.dumps({"stream": stream, "data": {"e": "x", "s": "BTCUSDT"}})


def _assert_base(stream, expected_base):
    urls = []

    def connect_fn(url):
        urls.append(url)
        return _FakeConn(_frame(stream))

    def on_message(s, d, recv_ns):
        mgr.stop()

    mgr = cm.ConnectionManager(
        streams=[stream], on_message=on_message, connect_fn=connect_fn,
        proactive_reconnect_s=10**9, sleep_fn=lambda x: asyncio.sleep(0),
        time_fn=lambda: 0.0,
    )
    asyncio.run(mgr.run())
    assert urls and urls[0].startswith(expected_base)


def test_market_stream_connects_on_market_base():
    _assert_base("btcusdt@aggTrade", cfg.WS_MARKET_BASE)


def test_public_stream_connects_on_public_base():
    _assert_base("btcusdt@bookTicker", cfg.WS_PUBLIC_BASE)
