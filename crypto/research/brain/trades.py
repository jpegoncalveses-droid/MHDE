"""Brain trades primitive (Phase 1 step 1): bucket raw aggTrades into fixed
base-cadence windows by event time and emit RAW, SEPARABLE within-window
summaries.

NO-BIAS contract — every emitted column is either:
  * immutable provenance / bounds: ``recv_ts_ns`` (max receive ns in the
    window), ``symbol``, ``window_start_ns``, ``window_end_ns``; or
  * a single-field within-window summary of a raw venue field
    (sum / max / mean / OHLC of price or quantity); plus
  * the venue-native taker buy/sell split, kept SEPARATE (never a ratio).

FORBIDDEN here (this is Phase 1, not Phase 3): ratios / imbalance,
normalization (rank / z-score), thresholds, cross-field products (e.g. quote
notional ``price*qty``), and any selection. Composition is a later phase.

Taker side from ``isBuyerMaker`` (``m``): ``m=True`` -> taker SELL (the buyer
was the maker, so the aggressor/taker sold); ``m=False`` -> taker BUY. Inverting
this is the classic footgun; it is pinned by a test.

Pure: no I/O, deterministic. ``bucket_trades`` is the whole surface.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

_MS_TO_NS = 1_000_000


def _window_start_ns(trade_time_ms: int, cadence_ns: int) -> int:
    """Floor an event-time (ms) trade to its fixed base-cadence window start (ns)."""
    t_ns = trade_time_ms * _MS_TO_NS
    return (t_ns // cadence_ns) * cadence_ns


def bucket_trades(trades: Iterable[Mapping[str, Any]], *, cadence_ns: int) -> list[dict]:
    """Group clean trade dicts into ``(symbol, window)`` snapshots of raw primitives.

    Each input trade is the clean shape produced by :mod:`brain.reader`:
    ``recv_ts_ns`` (int), ``symbol`` (str), ``trade_time_ms`` (int event time),
    ``price`` (float), ``qty`` (float), ``is_buyer_maker`` (bool).

    Windows are fixed ``[w, w + cadence_ns)`` half-open buckets keyed on event
    time. Returns one snapshot dict per ``(symbol, window)``, ordered by
    ``(symbol, window_start_ns)`` for determinism.
    """
    groups: dict[tuple[str, int], list[dict]] = {}
    for t in trades:
        ws = _window_start_ns(t["trade_time_ms"], cadence_ns)
        groups.setdefault((t["symbol"], ws), []).append(dict(t))

    snapshots: list[dict] = []
    for (symbol, ws), rows in groups.items():
        # Event order within the window: (trade_time_ms, recv_ts_ns) ascending,
        # so open/close are the first/last *trades*, not first/last *received*.
        rows.sort(key=lambda r: (r["trade_time_ms"], r["recv_ts_ns"]))
        prices = [r["price"] for r in rows]
        qtys = [r["qty"] for r in rows]

        taker_buy_vol = 0.0
        taker_sell_vol = 0.0
        buy_trade_count = 0
        sell_trade_count = 0
        for r in rows:
            if r["is_buyer_maker"]:          # m=True  -> taker SELL
                taker_sell_vol += r["qty"]
                sell_trade_count += 1
            else:                             # m=False -> taker BUY
                taker_buy_vol += r["qty"]
                buy_trade_count += 1

        n = len(rows)
        qty_sum = sum(qtys)
        snapshots.append({
            # provenance / immutable bounds
            "recv_ts_ns": max(r["recv_ts_ns"] for r in rows),
            "symbol": symbol,
            "window_start_ns": ws,
            "window_end_ns": ws + cadence_ns,
            # raw separable primitives (single-field summaries + taker split)
            "taker_buy_vol": taker_buy_vol,
            "taker_sell_vol": taker_sell_vol,
            "buy_trade_count": buy_trade_count,
            "sell_trade_count": sell_trade_count,
            "trade_count": n,
            "price_open": prices[0],
            "price_high": max(prices),
            "price_low": min(prices),
            "price_close": prices[-1],
            "qty_sum": qty_sum,
            "qty_max": max(qtys),
            "qty_mean": qty_sum / n,
        })

    snapshots.sort(key=lambda s: (s["symbol"], s["window_start_ns"]))
    return snapshots
