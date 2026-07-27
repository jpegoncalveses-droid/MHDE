"""OKX Stage B WS — pure frame->row normalizers, byte-identical to the Binance WS schemas.

Each mapper turns one OKX public-WS `data` element into a store row whose column set is
identical to the corresponding Binance dataset (capture_core/store.py). Arg-routing (symbol
resolution, incl. the instId that `bbo-tbt` omits) is the client's job; these mappers are pure
and receive the resolved `symbol` + `Decimal` ctVal. Sizes are converted contracts->coin.
"""
from __future__ import annotations

from decimal import Decimal

from crypto.research.capture_core import store
from crypto.research.capture_core_okx import ws_normalize as wn
from crypto.research.capture_core_okx.symbols import instid_to_symbol

_RECV_NS = 1_700_000_000_123_456_789
_TS = 1_700_000_000_123          # OKX ms timestamp string source
_CT_BTC = Decimal("0.01")
_CT_PEPE = Decimal("10000000")


# ---- trades (aggTrade) ----------------------------------------------------

def test_trades_row_schema_parity_vs_binance_aggtrade():
    d = {"instId": "BTC-USDT-SWAP", "tradeId": "130639474", "px": "42219.9",
         "sz": "3", "side": "sell", "ts": str(_TS), "count": "3"}
    row = wn.okx_trades_row(d, symbol="BTCUSDT", ct_val=_CT_BTC, recv_ns=_RECV_NS)

    assert set(row) == set(store.AGGTRADE_SCHEMA.names)     # byte-identical column set
    assert row["recv_ts_ns"] == _RECV_NS
    assert row["E"] == _TS and row["T"] == _TS              # E == T == ts
    assert row["a"] == 130639474 and row["l"] == 130639474  # a == l == tradeId
    assert row["f"] == 130639474 - 3 + 1                    # f = tradeId - count + 1
    assert row["s"] == "BTCUSDT"
    assert row["p"] == "42219.9"                            # price lossless
    assert row["q"] == "0.03"                               # 3 contracts x 0.01
    assert row["m"] is True                                 # side 'sell' -> buyer is maker
    assert row["e"] == "aggTrade"


def test_trades_taker_buy_sets_maker_false():
    d = {"instId": "BTC-USDT-SWAP", "tradeId": "5", "px": "1", "sz": "1",
         "side": "buy", "ts": str(_TS), "count": "1"}
    row = wn.okx_trades_row(d, symbol="BTCUSDT", ct_val=_CT_BTC, recv_ns=_RECV_NS)
    assert row["m"] is False                                # taker buy -> maker False


# ---- bbo-tbt (bookTicker) -------------------------------------------------

def test_bbo_row_schema_parity_vs_binance_bookticker():
    # bbo-tbt frames carry NO instId; the client injects the resolved symbol.
    d = {"asks": [["42224.7", "5", "0", "2"]], "bids": [["42224.6", "1", "0", "1"]],
         "ts": str(_TS), "seqId": 987654}
    row = wn.okx_bbo_row(d, symbol="BTCUSDT", ct_val=_CT_BTC, recv_ns=_RECV_NS)

    assert set(row) == set(store.BOOKTICKER_SCHEMA.names)
    assert row["u"] == 987654                               # u <- seqId
    assert row["s"] == "BTCUSDT"                            # injected
    assert row["b"] == "42224.6" and row["a"] == "42224.7"  # px lossless
    assert row["B"] == "0.01"                               # 1 contract x 0.01
    assert row["A"] == "0.05"                               # 5 contracts x 0.01
    assert row["E"] == _TS and row["T"] == _TS              # E == T == ts


# ---- mark/index/funding merge (markPrice) ---------------------------------

def test_markprice_merge_row_schema_parity_and_settle_equals_mark():
    mark = {"markPx": "42310.6", "ts": str(_TS)}
    index = {"idxPx": "42309.1"}
    funding = {"fundingRate": "0.00012", "fundingTime": "1700000800000"}
    row = wn.okx_markprice_merge_row(symbol="BTCUSDT", mark=mark, index=index,
                                     funding=funding, recv_ns=_RECV_NS)

    assert set(row) == set(store.MARKPRICE_SCHEMA.names)
    assert row["p"] == "42310.6"                            # p <- markPx
    assert row["i"] == "42309.1"                            # i <- idxPx
    assert row["P"] == "42310.6"                            # D1: P == markPx (settle proxy)
    assert row["r"] == "0.00012"                            # r <- fundingRate
    assert row["T"] == 1700000800000                        # T <- fundingTime (future)
    assert row["E"] == _TS                                  # E <- mark ts
    assert row["e"] == "markPriceUpdate"


# ---- liquidation-orders (forceOrder) --------------------------------------

def test_liquidation_rows_schema_parity_no_e_column():
    d = {"instId": "BTC-USDT-SWAP",
         "details": [{"side": "sell", "posSide": "long", "bkPx": "41000",
                      "sz": "5", "ts": str(_TS)}]}
    rows = wn.okx_liquidation_rows(d, symbol="BTCUSDT", ct_val=_CT_BTC, recv_ns=_RECV_NS)

    assert len(rows) == 1
    r = rows[0]
    assert set(r) == set(store.FORCEORDER_SCHEMA.names)     # NO 'e' column
    assert "e" not in r
    assert r["p"] == "41000" and r["ap"] == "41000"         # p == ap == bkPx
    assert r["q"] == "0.05"                                  # 5 contracts x 0.01
    assert r["S"] == "SELL"                                  # side upper-cased (Binance style)
    assert r["E"] == _TS and r["T"] == _TS
    assert r["o"] == "" and r["f"] == "" and r["X"] == "" and r["l"] == "" and r["z"] == ""


def test_liquidation_multiple_details_fan_out():
    d = {"instId": "BTC-USDT-SWAP",
         "details": [{"side": "sell", "posSide": "long", "bkPx": "41000", "sz": "5", "ts": str(_TS)},
                     {"side": "buy", "posSide": "short", "bkPx": "41010", "sz": "2", "ts": str(_TS)}]}
    rows = wn.okx_liquidation_rows(d, symbol="BTCUSDT", ct_val=_CT_BTC, recv_ns=_RECV_NS)
    assert [r["S"] for r in rows] == ["SELL", "BUY"]
    assert [r["q"] for r in rows] == ["0.05", "0.02"]


# ---- symbol contract (incl. multiplier symbols never bridged) -------------

def test_multiplier_symbols_never_bridged():
    # OKX PEPE-USDT-SWAP -> PEPEUSDT (distinct series from Binance 1000PEPEUSDT, 1000x scale)
    sym = instid_to_symbol("PEPE-USDT-SWAP")
    assert sym == "PEPEUSDT"
    d = {"instId": "PEPE-USDT-SWAP", "tradeId": "9", "px": "0.0000082",
         "sz": "4", "side": "buy", "ts": str(_TS), "count": "1"}
    row = wn.okx_trades_row(d, symbol=sym, ct_val=_CT_PEPE, recv_ns=_RECV_NS)
    assert row["s"] == "PEPEUSDT" and row["s"] != "1000PEPEUSDT"
    assert row["q"] == "40000000"                           # 4 x 10000000, no sci-notation
