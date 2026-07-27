"""OKX Stage B WS — collector routing + the markPrice 3-channel merge.

The collector resolves instId->symbol + ctVal and routes each decoded frame to the right
normalizer/writer. markPrice has no bundled stream: mark-price / index-tickers / funding-rate
arrive on three async channels and are merged into one row per symbol on a 1s tick (D5), emitted
only once all three have been seen. Non-universe instruments are dropped.
"""
from __future__ import annotations

from decimal import Decimal

from crypto.research.capture_core_okx import ws_collector as col
from crypto.research.capture_core_okx.symbols import SymbolMap

_RECV_NS = 1_700_000_000_123_456_789
_TS = "1700000000123"
_UNIVERSE = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
_CTVAL = {"BTC-USDT-SWAP": Decimal("0.01"), "ETH-USDT-SWAP": Decimal("0.1")}


class _FakeWriter:
    def __init__(self):
        self.rows = []
        self.flush_due_calls = 0
        self.flush_all_calls = 0

    def append(self, row):
        self.rows.append(row)

    def flush_due(self):
        self.flush_due_calls += 1

    def flush_all(self):
        self.flush_all_calls += 1


def _collector():
    writers = {k: _FakeWriter() for k in ("aggTrade", "bookTicker", "markPrice", "forceOrder")}
    c = col.OkxWsCollector(symbol_map=SymbolMap(_UNIVERSE), ctval_table=_CTVAL, writers=writers)
    return c, writers


# ---- markPrice 3-channel merge -------------------------------------------

def test_markprice_merge_emits_only_when_all_three_channels_seen():
    m = col.MarkPriceMergeState()
    m.update_mark("BTCUSDT", "42310.6", _TS)
    assert m.emit(_RECV_NS) == []                         # index+funding not seen yet
    m.update_index("BTCUSDT", "42309.1")
    assert m.emit(_RECV_NS) == []                         # funding not seen yet
    m.update_funding("BTCUSDT", "0.00012", "1700000800000")

    rows = m.emit(_RECV_NS)
    assert len(rows) == 1
    r = rows[0]
    assert r["s"] == "BTCUSDT" and r["p"] == "42310.6" and r["P"] == "42310.6"
    assert r["i"] == "42309.1" and r["r"] == "0.00012" and r["T"] == 1700000800000


def test_markprice_merge_uses_latest_seen_per_field():
    m = col.MarkPriceMergeState()
    m.update_mark("BTCUSDT", "1", _TS)
    m.update_index("BTCUSDT", "2")
    m.update_funding("BTCUSDT", "0.0001", "1700000800000")
    m.update_mark("BTCUSDT", "9", "1700000009999")        # newer mark
    r = m.emit("999")[0]
    assert r["p"] == "9" and r["E"] == 1700000009999


# ---- channel router -------------------------------------------------------

def test_router_dispatches_trades_and_bbo_to_shared_writers():
    c, w = _collector()
    c.on_frame("trades", "BTC-USDT-SWAP",
               [{"tradeId": "5", "px": "42000", "sz": "3", "side": "sell",
                 "ts": _TS, "count": "1"}], _RECV_NS)
    c.on_frame("bbo-tbt", "BTC-USDT-SWAP",
               [{"asks": [["42001", "5", "0", "1"]], "bids": [["42000", "1", "0", "1"]],
                 "ts": _TS, "seqId": 7}], _RECV_NS)

    assert len(w["aggTrade"].rows) == 1 and w["aggTrade"].rows[0]["q"] == "0.03"
    assert len(w["bookTicker"].rows) == 1 and w["bookTicker"].rows[0]["u"] == 7


def test_router_drops_non_universe_instrument():
    c, w = _collector()
    c.on_frame("trades", "DOGE-USDT-SWAP",                # not in the universe map
               [{"tradeId": "1", "px": "1", "sz": "1", "side": "buy", "ts": _TS, "count": "1"}],
               _RECV_NS)
    assert w["aggTrade"].rows == []


def test_router_liquidation_fans_out_and_filters_universe():
    c, w = _collector()
    c.on_frame("liquidation-orders", None, [
        {"instId": "ETH-USDT-SWAP",
         "details": [{"side": "sell", "posSide": "long", "bkPx": "2500", "sz": "2", "ts": _TS}]},
        {"instId": "DOGE-USDT-SWAP",                       # non-universe -> dropped
         "details": [{"side": "buy", "posSide": "short", "bkPx": "0.1", "sz": "1", "ts": _TS}]},
    ], _RECV_NS)
    assert len(w["forceOrder"].rows) == 1
    r = w["forceOrder"].rows[0]
    assert r["s"] == "ETHUSDT" and r["S"] == "SELL" and r["q"] == "0.2"   # 2 x 0.1 (ETH ctVal)


def test_flush_due_flushes_all_writers_not_just_markprice():
    # BLOCKING regression: the fast writers (aggTrade/bookTicker/forceOrder) must be flushed
    # periodically too, else they buffer to RAM until MemoryMax SIGKILLs the daemon.
    c, w = _collector()
    c.flush_due()
    for name in ("aggTrade", "bookTicker", "markPrice", "forceOrder"):
        assert w[name].flush_due_calls == 1, f"{name} writer was not flushed"


def test_markprice_merge_skips_symbols_without_a_new_mark():
    # Stale-fill regression: a symbol must NOT re-emit from frozen last-seen state (advancing
    # recv_ts_ns over a gap / for a delisted-quiet symbol). Emit only when a NEW mark arrived.
    m = col.MarkPriceMergeState()
    m.update_mark("BTCUSDT", "42310.6", _TS)
    m.update_index("BTCUSDT", "42309.1")
    m.update_funding("BTCUSDT", "0.00012", "1700000800000")
    assert len(m.emit(1)) == 1                             # first tick: fresh mark -> emit
    assert m.emit(2) == []                                 # no new mark -> NO stale re-emit
    m.update_mark("BTCUSDT", "42311.0", "1700000009999")   # a new mark arrives
    assert len(m.emit(3)) == 1                             # emits again


def test_markprice_merge_invalidate_drops_state():
    # on socket break the merge state is invalidated so nothing is emitted until fresh frames.
    m = col.MarkPriceMergeState()
    m.update_mark("BTCUSDT", "1", _TS)
    m.update_index("BTCUSDT", "2")
    m.update_funding("BTCUSDT", "0.0001", "1700000800000")
    m.invalidate()
    assert m.emit(1) == []                                 # cleared


def test_router_markprice_channels_feed_merge_then_emit():
    c, w = _collector()
    c.on_frame("mark-price", "BTC-USDT-SWAP",
               [{"instId": "BTC-USDT-SWAP", "markPx": "42310.6", "ts": _TS}], _RECV_NS)
    c.on_frame("index-tickers", "BTC-USDT",               # index pair (no -SWAP)
               [{"instId": "BTC-USDT", "idxPx": "42309.1"}], _RECV_NS)
    c.on_frame("funding-rate", "BTC-USDT-SWAP",
               [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.00012",
                 "fundingTime": "1700000800000"}], _RECV_NS)
    assert w["markPrice"].rows == []                       # nothing written until the tick

    c.emit_markprice(_RECV_NS)
    assert len(w["markPrice"].rows) == 1
    assert w["markPrice"].rows[0]["s"] == "BTCUSDT" and w["markPrice"].rows[0]["i"] == "42309.1"
