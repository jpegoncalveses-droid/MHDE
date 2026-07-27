"""OKX Stage B WS — daemon runtime path (run / _emit_loop / run_for_window).

The pure-unit tests drive on_frame directly and the gate flushes manually, so nothing pinned the
long-running daemon contract. These drive the real daemon with an injected connect_fn (continuous
canned frames) over a short window and assert the two invariants the review found broken: ALL
writers are flushed periodically (not just markPrice) and on shutdown, and the emit loop survives
a writer error instead of silently dying.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from crypto.research.capture_core_okx import ws_collector as col
from crypto.research.capture_core_okx.symbols import SymbolMap

_TRADE = json.dumps({"arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                     "data": [{"tradeId": "1", "px": "1", "sz": "1", "side": "buy",
                               "ts": "1700000000123", "count": "1"}]})


class _FakeWriter:
    def __init__(self, raise_on_flush_due=False):
        self.rows = []
        self.flush_due_calls = 0
        self.flush_all_calls = 0
        self._raise = raise_on_flush_due

    def append(self, row):
        self.rows.append(row)

    def flush_due(self):
        self.flush_due_calls += 1
        if self._raise:
            raise OSError("[Errno 28] No space left on device")

    def flush_all(self):
        self.flush_all_calls += 1


class _ContinuousConn:
    """Async-CM WS stub that returns the same frame every recv (so recv never blocks and
    stop is noticed promptly), pacing with a tiny yield."""
    def __init__(self, frame):
        self._frame = frame

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        return None

    async def recv(self):
        await asyncio.sleep(0.001)
        return self._frame


def _collector(writers):
    return col.OkxWsCollector(
        symbol_map=SymbolMap(["BTC-USDT-SWAP"]), ctval_table={"BTC-USDT-SWAP": Decimal("0.01")},
        writers=writers, connect_fn=lambda url: _ContinuousConn(_TRADE), emit_interval_s=0.02)


def test_daemon_periodically_flushes_all_fast_writers_and_on_shutdown():
    writers = {k: _FakeWriter() for k in ("aggTrade", "bookTicker", "markPrice", "forceOrder", "_gaps")}
    c = _collector(writers)
    asyncio.run(col.run_for_window(c, 0.2, sleep_fn=asyncio.sleep))

    assert writers["aggTrade"].rows, "trade frames should have been routed"
    # BLOCKING fix: the fast writers are flushed periodically, not just markPrice
    assert writers["aggTrade"].flush_due_calls >= 1
    assert writers["bookTicker"].flush_due_calls >= 1
    assert writers["forceOrder"].flush_due_calls >= 1
    # and everything flushed on clean shutdown
    for name in ("aggTrade", "bookTicker", "markPrice", "forceOrder"):
        assert writers[name].flush_all_calls >= 1, f"{name} not flushed on shutdown"


def test_daemon_emit_loop_survives_writer_error():
    # FIX 4: a flush_due OSError (e.g. ENOSPC) must not silently kill the emit/flush loop or
    # leave the daemon hung; it completes cleanly and still flushes on shutdown.
    writers = {k: _FakeWriter() for k in ("aggTrade", "bookTicker", "markPrice", "forceOrder", "_gaps")}
    writers["markPrice"] = _FakeWriter(raise_on_flush_due=True)
    c = _collector(writers)

    asyncio.run(col.run_for_window(c, 0.2, sleep_fn=asyncio.sleep))   # must not raise/hang

    assert writers["markPrice"].flush_due_calls >= 1                  # kept trying (didn't die)
    assert writers["aggTrade"].flush_all_calls >= 1                   # shutdown flush still ran
