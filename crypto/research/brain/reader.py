"""Brain capture readers: READ-ONLY pyarrow consumers of the capture tape.

One generic core (:func:`_read_dataset_rows`) reads any capture dataset under
``<capture_root>/<dataset>/symbol=*/date=*`` filtered to ``recv_ts_ns > cursor``
and globally sorted by ``recv_ts_ns`` (part files are disjoint but hash-named, so
filename order is not time order). Per-source functions then map the terse venue
field names to clean names and cast the VARCHAR numerics to float.

Symbols are UTF-8 (CJK / digit-leading exist on Binance USDT-M) — read through
the Hive partitioning / in-row ``s`` field, never an ASCII regex.

Bucket (event-time) field per source — all clean rows expose ``event_time_ms``
as the field the primitives bucket on:
  * aggTrade   -> trade time ``T`` (the match time), also exposes event_time_ms = E
  * bookTicker -> event time ``E``
  * markPrice  -> event time ``E``  (FOOTGUN: markPrice ``T`` is the *next funding
    time*, a future stamp — never bucket on it; it is kept as ``next_funding_time_ms``)
  * forceOrder -> event time ``E``  (``T`` trade time also exposed)

Strictly read-only: these never write anything, anywhere.
"""
from __future__ import annotations

import pathlib
from typing import Optional, Sequence

import pyarrow.dataset as ds
import pyarrow.compute as pc

from crypto.research.brain import config as cfg


def _read_dataset_rows(
    capture_root: str,
    capture_dataset: str,
    after_recv_ts_ns: int,
    columns: list[str],
    symbols: Optional[Sequence[str]],
    symbol_col: str = "s",
) -> list[dict]:
    """Read terse rows from a capture dataset, ``recv_ts_ns > cursor``, sorted asc."""
    base = pathlib.Path(capture_root, capture_dataset)
    if not base.exists() or not any(base.rglob("*.parquet")):
        return []
    dataset = ds.dataset(str(base), format="parquet", partitioning="hive")
    flt = pc.field("recv_ts_ns") > after_recv_ts_ns
    if symbols is not None:
        # Filter on the in-row symbol field (``s``, or ``pair`` for basis) — robust
        # regardless of partition dictionary encoding; path pruning still applies.
        flt = flt & pc.field(symbol_col).isin(list(symbols))
    table = dataset.to_table(columns=columns, filter=flt)
    table = table.sort_by([("recv_ts_ns", "ascending")])
    return table.to_pylist()


def _safe_float(v) -> Optional[float]:
    """Cast a VARCHAR venue numeric to float; empty string / None -> None (null)."""
    if v is None or v == "":
        return None
    return float(v)


def read_new_aggtrades(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Clean aggTrade dicts with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, trade_time_ms, agg_id, price, qty,
    is_buyer_maker, taker_buy. Primitive buckets on ``trade_time_ms``.
    """
    rows = _read_dataset_rows(capture_root, cfg.AGGTRADE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "a", "s", "p", "q", "T", "m"], symbols)
    out: list[dict] = []
    for r in rows:
        m = bool(r["m"])
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "trade_time_ms": int(r["T"]),
            "agg_id": int(r["a"]),
            "price": float(r["p"]),   # VARCHAR -> float
            "qty": float(r["q"]),     # VARCHAR -> float
            "is_buyer_maker": m,
            "taker_buy": not m,       # m=False -> taker BUY
        })
    return out


def read_new_bookticker(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Clean bookTicker dicts with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, transaction_time_ms, bid, bid_qty,
    ask, ask_qty. Primitive buckets on ``event_time_ms`` (E).
    """
    rows = _read_dataset_rows(capture_root, cfg.BOOKTICKER_CAPTURE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "T", "s", "b", "B", "a", "A"], symbols)
    out: list[dict] = []
    for r in rows:
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "transaction_time_ms": int(r["T"]),
            "bid": float(r["b"]),       # VARCHAR -> float
            "bid_qty": float(r["B"]),
            "ask": float(r["a"]),
            "ask_qty": float(r["A"]),
        })
    return out


def read_new_markprice(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Clean markPrice dicts with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, mark, index, settle, funding,
    next_funding_time_ms. Primitive buckets on ``event_time_ms`` (E). FOOTGUN:
    the venue ``T`` is the *next funding time* (future) -> ``next_funding_time_ms``,
    NEVER the bucket key.
    """
    rows = _read_dataset_rows(capture_root, cfg.MARKPRICE_CAPTURE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "s", "p", "i", "P", "r", "T"], symbols)
    out: list[dict] = []
    for r in rows:
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "mark": float(r["p"]),      # VARCHAR -> float
            "index": float(r["i"]),
            "settle": float(r["P"]),
            "funding": float(r["r"]),
            "next_funding_time_ms": int(r["T"]),  # future funding time, NOT an event time
        })
    return out


def read_new_forceorder(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Clean forceOrder (liquidation) dicts, ``recv_ts_ns > after``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, trade_time_ms, side, qty, price.
    Primitive buckets on ``event_time_ms`` (E). ``side`` is the raw venue ``S``
    ('BUY' / 'SELL'); only the fields the primitive needs are projected (avoids
    the flattened single-letter collisions ``o``/``l``/``z``).
    """
    rows = _read_dataset_rows(capture_root, cfg.FORCEORDER_CAPTURE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "T", "s", "S", "q", "p"], symbols)
    out: list[dict] = []
    for r in rows:
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "trade_time_ms": int(r["T"]),
            "side": r["S"],             # raw venue side: 'BUY' / 'SELL'
            "qty": float(r["q"]),       # VARCHAR -> float
            "price": float(r["p"]),
        })
    return out


def read_new_asof(
    capture_root: str,
    capture_dataset: str,
    *,
    value_map: dict,
    asof_time_col: str,
    symbol_col: str = "s",
    int_map: Optional[dict] = None,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Clean rows for a REST present-state ("as-of") series, recv-order.

    FORWARD-ONLY, uniform with klines: ``event_time_ms`` (the bucket / visibility
    key) is the recv-time ARRIVAL ms (``recv_ts_ns // 1e6``), NOT the venue time —
    a value is visible only once the brain has observed it, never retroactively in
    a window before its arrival (which would be a lookahead, e.g. a batched
    futures_data fetch re-delivering old 5-min buckets). The venue time-key
    (``asof_time_col``) is kept as ``asof_event_time_ms`` — a stored staleness
    signal, no longer the visibility gate.

    ``value_map`` maps clean float field name -> venue VARCHAR column; ``int_map``
    (optional) maps clean int field name -> venue int column. ``symbol_col`` is the
    in-row symbol field (``s`` or ``pair``). Empty-string numerics -> None.
    """
    int_map = int_map or {}
    columns = (["recv_ts_ns", symbol_col, asof_time_col]
               + list(value_map.values()) + list(int_map.values()))
    rows = _read_dataset_rows(capture_root, capture_dataset, after_recv_ts_ns,
                              columns, symbols, symbol_col=symbol_col)
    out: list[dict] = []
    for r in rows:
        recv = int(r["recv_ts_ns"])
        clean = {
            "recv_ts_ns": recv,
            "symbol": r[symbol_col],
            "event_time_ms": recv // 1_000_000,        # ARRIVAL — bucket/visibility key
            "asof_event_time_ms": int(r[asof_time_col]),  # venue time — staleness signal only
        }
        for clean_name, venue_col in value_map.items():
            clean[clean_name] = _safe_float(r[venue_col])
        for clean_name, venue_col in int_map.items():
            clean[clean_name] = int(r[venue_col])
        out.append(clean)
    return out


# klines_1h on-disk columns (the 'ignore' venue field is dropped by capture).
_KLINES_COLUMNS = ["recv_ts_ns", "s", "openTime", "open", "high", "low", "close",
                   "volume", "closeTime", "quoteVolume", "trades", "takerBuyBase", "takerBuyQuote"]


def read_new_klines(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Clean klines_1h bars with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, + the native bar fields (open, high,
    low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote) and
    the bar identity (open_time, close_time).

    FORWARD-ONLY (load-bearing): ``event_time_ms`` is the recv-time ARRIVAL ms
    (``recv_ts_ns // 1e6``), NOT the bar's openTime/closeTime. A 1h bar is
    REST-backfilled, so its closeTime can be long before the brain observed it;
    keying the as-of on closeTime would expose a bar before it was available
    (lookahead). The bar's own times are kept only as identity fields.
    """
    rows = _read_dataset_rows(capture_root, cfg.KLINES_CAPTURE_DATASET, after_recv_ts_ns,
                              _KLINES_COLUMNS, symbols)
    out: list[dict] = []
    for r in rows:
        recv = int(r["recv_ts_ns"])
        out.append({
            "recv_ts_ns": recv,
            "symbol": r["s"],
            "event_time_ms": recv // 1_000_000,   # ARRIVAL time, NOT the bar time
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
            "quote_volume": float(r["quoteVolume"]),
            "trades": int(r["trades"]),
            "taker_buy_base": float(r["takerBuyBase"]),
            "taker_buy_quote": float(r["takerBuyQuote"]),
            "open_time": int(r["openTime"]),
            "close_time": int(r["closeTime"]),
        })
    return out
