"""Brain forceOrder primitive: bucket liquidations into base-cadence windows by
event time and emit RAW, SEPARABLE within-window summaries.

forceOrder is sparse, event-like (a forced trade). Applying the trades lesson:
split by side ``S`` (BUY/SELL) and keep, per side, base volume, per-event
notional (``price*qty`` summed — irrecoverable downstream), and counts. The
absent side in a window is a raw 0 (a real "no liquidation on that side"
observation), never null. No ratios, no normalization, no thresholds.

Side: a SELL forceOrder is a long being liquidated, a BUY a short — kept as the
raw venue side, never interpreted. Windows are keyed on ``event_time_ms`` (E).

Pure: no I/O, deterministic. ``bucket_forceorder`` is the whole surface.

NOTE: this primitive emits a snapshot only for windows that contain >= 1
liquidation (the brain is event-driven and has no dense window grid here). A
fully-empty window has no row; consumers reading the forceorder dataset treat a
missing (symbol, window) as zero liquidation — consistent with the raw-zero
semantics above.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

_MS_TO_NS = 1_000_000


def _window_start_ns(event_time_ms: int, cadence_ns: int) -> int:
    return (event_time_ms * _MS_TO_NS // cadence_ns) * cadence_ns


def bucket_forceorder(rows: Iterable[Mapping[str, Any]], *, cadence_ns: int) -> list[dict]:
    """Group clean forceOrder dicts into ``(symbol, window)`` raw-summary snapshots.

    Each input row: recv_ts_ns, symbol, event_time_ms, side ('BUY'/'SELL'), qty,
    price. Windows are keyed on ``event_time_ms``.
    """
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        ws = _window_start_ns(r["event_time_ms"], cadence_ns)
        groups.setdefault((r["symbol"], ws), []).append(dict(r))

    snapshots: list[dict] = []
    for (symbol, ws), evs in groups.items():
        liq_buy_vol = 0.0
        liq_sell_vol = 0.0
        liq_buy_quote_vol = 0.0
        liq_sell_quote_vol = 0.0
        liq_buy_count = 0
        liq_sell_count = 0
        for e in evs:
            notional = e["price"] * e["qty"]   # per-event notional (raw, irrecoverable)
            if e["side"] == "SELL":
                liq_sell_vol += e["qty"]
                liq_sell_quote_vol += notional
                liq_sell_count += 1
            else:                               # 'BUY'
                liq_buy_vol += e["qty"]
                liq_buy_quote_vol += notional
                liq_buy_count += 1

        snapshots.append({
            "recv_ts_ns": max(e["recv_ts_ns"] for e in evs),
            "symbol": symbol,
            "window_start_ns": ws,
            "window_end_ns": ws + cadence_ns,
            "liq_buy_vol": liq_buy_vol,
            "liq_sell_vol": liq_sell_vol,
            "liq_buy_quote_vol": liq_buy_quote_vol,
            "liq_sell_quote_vol": liq_sell_quote_vol,
            "liq_buy_count": liq_buy_count,
            "liq_sell_count": liq_sell_count,
        })

    snapshots.sort(key=lambda s: (s["symbol"], s["window_start_ns"]))
    return snapshots
