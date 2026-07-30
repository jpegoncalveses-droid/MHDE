"""OKX public-WS frame -> store row, byte-identical to the Binance WS schemas.

The OKX analog of ``capture_core/service.py``'s pure ``*_row`` helpers. Each function
maps one OKX ``data`` element to a row whose column set matches the corresponding Binance
dataset (``capture_core/store.py`` schemas), so the shared store writers and the brain WS
readers need zero changes. Venue arg-routing (symbol resolution, incl. the ``instId`` that
``bbo-tbt`` frames omit) happens in the WS client; these functions are pure and receive the
already-resolved ``symbol`` and ``Decimal`` ctVal. OKX sizes are in CONTRACTS and are
converted to coin units via :func:`ctval.contracts_to_coin`.

Field mappings (see docs/OKX_STAGE_B_WS.md):
  trades      a=l=tradeId, f=tradeId-count+1, m=(side=='sell'), q=sz x ctVal
  bbo-tbt     u=seqId, s injected, B/A=sz x ctVal, E=T=ts
  markPrice   p=markPx, i=idxPx, P=markPx (D1), r=fundingRate, T=fundingTime, E=mark ts
  liquidation p=ap=bkPx, q=sz x ctVal, S=side.upper(), E=T=ts, o/f/X/l/z=''
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from crypto.research.capture_core_okx.ctval import contracts_to_coin


def okx_trades_row(d: Mapping[str, Any], *, symbol: str, ct_val: Decimal, recv_ns: int) -> dict:
    """OKX ``trades`` element -> aggTrade row. ``count`` = trades aggregated into this push."""
    trade_id = int(d["tradeId"])
    count = int(d["count"])
    ts = int(d["ts"])
    return {
        "recv_ts_ns": recv_ns,
        "e": "aggTrade",
        "E": ts,
        "a": trade_id,
        "s": symbol,
        "p": d["px"],                                    # venue string, lossless
        "q": contracts_to_coin(d["sz"], ct_val),         # contracts -> coin
        "f": trade_id - count + 1,
        "l": trade_id,
        "T": ts,
        "m": d["side"] == "sell",                        # taker sell -> buyer is maker
    }


def okx_bbo_row(d: Mapping[str, Any], *, symbol: str, ct_val: Decimal, recv_ns: int) -> dict:
    """OKX ``bbo-tbt`` element -> bookTicker row. Frame carries NO instId; ``symbol`` injected.

    Level shape is ``[px, sz, liqOrders(deprecated), numOrders]``; sizes are in contracts.
    """
    bid = d["bids"][0]
    ask = d["asks"][0]
    ts = int(d["ts"])
    return {
        "recv_ts_ns": recv_ns,
        "e": None,                                       # Binance bookTicker has no 'e'
        "u": int(d["seqId"]),
        "s": symbol,
        "b": bid[0],
        "B": contracts_to_coin(bid[1], ct_val),
        "a": ask[0],
        "A": contracts_to_coin(ask[1], ct_val),
        "T": ts,
        "E": ts,
    }


def okx_markprice_merge_row(*, symbol: str, mark: Mapping[str, Any], index: Mapping[str, Any],
                            funding: Mapping[str, Any], recv_ns: int) -> dict:
    """Merge last-seen mark / index / funding into one markPrice row (D5: 1s per symbol).

    D1: OKX has no estimated-settle-price, so ``P`` mirrors ``markPx`` — Binance's own ``P``
    tracks mark within ~0.04%, so this keeps ``settle_*`` ~= ``mark_*`` and byte-identical.
    ``T`` is the FUTURE funding time (not an event time).
    """
    return {
        "recv_ts_ns": recv_ns,
        "e": "markPriceUpdate",
        "E": int(mark["ts"]),
        "s": symbol,
        "p": mark["markPx"],
        "i": index["idxPx"],
        "P": mark["markPx"],
        "r": funding["fundingRate"],
        "T": int(funding["fundingTime"]),
    }


def okx_liquidation_rows(d: Mapping[str, Any], *, symbol: str, ct_val: Decimal,
                         recv_ns: int) -> list[dict]:
    """OKX ``liquidation-orders`` element -> one forceOrder row per ``details`` entry.

    forceOrder has NO ``e`` column. OKX has no order-type/status/fill fields, so
    ``o/f/X/l/z`` are empty strings (not projected by ``read_new_forceorder``).
    """
    rows = []
    for det in d["details"]:
        ts = int(det["ts"])
        rows.append({
            "recv_ts_ns": recv_ns,
            "E": ts,
            "s": symbol,
            "S": det["side"].upper(),                    # 'sell' -> 'SELL' (Binance style)
            "o": "",
            "f": "",
            "q": contracts_to_coin(det["sz"], ct_val),
            "p": det["bkPx"],                            # bankruptcy price
            "ap": det["bkPx"],
            "X": "",
            "l": "",
            "z": "",
            "T": ts,
        })
    return rows


def _coin_levels(levels, ct_val: Decimal) -> list:
    """OKX book levels [px, sz_contracts, liqOrders, numOrders] -> [[px, coin_sz]] (drop trailing)."""
    return [[lvl[0], contracts_to_coin(lvl[1], ct_val)] for lvl in levels]


def okx_books_row(d: Mapping[str, Any], *, symbol: str, ct_val: Decimal, recv_ns: int) -> dict:
    """OKX `books` frame element -> raw DEPTH_SCHEMA row (the full-ladder tape; brain never reads it).

    OKX has one sequence per message, so U == u == seqId; pu == prevSeqId (-1 on the snapshot).
    Zero-size levels are kept verbatim (raw diff tape); sizes are contracts->coin.
    """
    ts = int(d["ts"])
    seq = int(d["seqId"])
    return {
        "recv_ts_ns": recv_ns,
        "e": "depthUpdate",
        "E": ts,
        "T": ts,
        "s": symbol,
        "U": seq,
        "u": seq,
        "pu": int(d["prevSeqId"]),
        "b": _coin_levels(d["bids"], ct_val),
        "a": _coin_levels(d["asks"], ct_val),
    }


def okx_book_state_row(maintainer, *, symbol: str, ct_val: Decimal, recv_ns: int,
                       top_n: int = 20) -> dict:
    """Maintained OKX book -> DEPTH_STATE_SCHEMA row (top-N, ctVal-normalized) — the brain's need."""
    bids, asks = maintainer.top_levels(top_n)
    return {
        "recv_ts_ns": recv_ns,
        "s": symbol,
        "update_id": int(maintainer.last_seq_id),
        "valid": bool(maintainer.synced),
        "b": _coin_levels(bids, ct_val),
        "a": _coin_levels(asks, ct_val),
    }
