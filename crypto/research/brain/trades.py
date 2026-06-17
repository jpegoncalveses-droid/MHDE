"""Brain trades primitive (Phase 1 step 1): bucket raw aggTrades into fixed
base-cadence windows by event time and emit RAW, SEPARABLE within-window
summaries.

NO-BIAS contract — the line is INFORMATION vs INTERPRETATION, not "presence of
a product". A raw per-event quantity that cannot be reconstructed from the
separate window summaries IS a raw primitive and must be captured; only
engineered signals computed OVER those summaries are deferred to Phase 3. So
every emitted column is one of:
  * immutable provenance / bounds: ``recv_ts_ns`` (max receive ns in the
    window), ``symbol``, ``window_start_ns``, ``window_end_ns``;
  * a raw per-event quantity summed within the window — base volume AND notional
    (``price*qty``, the venue-native quote volume), each kept SEPARATE by taker
    side. Notional is RAW: it is NOT recoverable later from the stored qty/price
    summaries (different per-trade price*qty pairings share the same ``qty_sum``
    and price OHLC), so it carries information that would be lost otherwise; or
  * a single-field within-window summary of a raw venue field
    (sum / max / mean / OHLC of price or quantity).

FORBIDDEN here (Phase 3, not now): engineered signals OVER the window summaries
— ratios / imbalance, normalization (rank / z-score), thresholds, and any
selection. Also deliberately omitted because they are recoverable downstream
from the columns above: a total (= buy + sell) and VWAP (= notional / qty).

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
        taker_buy_quote_vol = 0.0
        taker_sell_quote_vol = 0.0
        buy_trade_count = 0
        sell_trade_count = 0
        for r in rows:
            notional = r["price"] * r["qty"]  # per-event quote volume (raw, irrecoverable)
            if r["is_buyer_maker"]:          # m=True  -> taker SELL
                taker_sell_vol += r["qty"]
                taker_sell_quote_vol += notional
                sell_trade_count += 1
            else:                             # m=False -> taker BUY
                taker_buy_vol += r["qty"]
                taker_buy_quote_vol += notional
                buy_trade_count += 1

        n = len(rows)
        qty_sum = sum(qtys)
        snapshots.append({
            # provenance / immutable bounds
            "recv_ts_ns": max(r["recv_ts_ns"] for r in rows),
            "symbol": symbol,
            "window_start_ns": ws,
            "window_end_ns": ws + cadence_ns,
            # raw separable primitives (per-event quantities + single-field
            # summaries; taker split kept separate)
            "taker_buy_vol": taker_buy_vol,
            "taker_sell_vol": taker_sell_vol,
            "taker_buy_quote_vol": taker_buy_quote_vol,
            "taker_sell_quote_vol": taker_sell_quote_vol,
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
