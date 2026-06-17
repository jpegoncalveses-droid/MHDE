"""Brain bookTicker primitive: bucket top-of-book updates into base-cadence
windows by event time and emit RAW, SEPARABLE within-window summaries.

NO-BIAS (information vs interpretation): within-window single-field summaries of
each native field — bid/ask price OHLC, bid/ask quantity last/min/max/mean — PLUS
the bid-ask SPREAD (ask - bid) summaries. Spread is a raw cross-field observable
that is IRRECOVERABLE from separate bid/ask summaries (the per-instant max spread
is not max(ask) - min(bid)), so it carries information that must be captured. No
engineered ratios, no normalization, no thresholds. Mid price is deliberately
omitted (see the PR): it is a derived reference price already spanned by bid/ask
OHLC, not a venue-native field.

Pure: no I/O, deterministic. ``bucket_bookticker`` is the whole surface.
"""
from __future__ import annotations

from statistics import fmean
from typing import Any, Iterable, Mapping

_MS_TO_NS = 1_000_000


def _window_start_ns(event_time_ms: int, cadence_ns: int) -> int:
    return (event_time_ms * _MS_TO_NS // cadence_ns) * cadence_ns


def bucket_bookticker(rows: Iterable[Mapping[str, Any]], *, cadence_ns: int) -> list[dict]:
    """Group clean bookTicker dicts into ``(symbol, window)`` raw-summary snapshots.

    Each input row: recv_ts_ns, symbol, event_time_ms, bid, bid_qty, ask, ask_qty.
    """
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        ws = _window_start_ns(r["event_time_ms"], cadence_ns)
        groups.setdefault((r["symbol"], ws), []).append(dict(r))

    snapshots: list[dict] = []
    for (symbol, ws), obs in groups.items():
        # Event order within the window: (event_time_ms, recv_ts_ns) ascending.
        obs.sort(key=lambda r: (r["event_time_ms"], r["recv_ts_ns"]))
        bids = [o["bid"] for o in obs]
        asks = [o["ask"] for o in obs]
        bid_qtys = [o["bid_qty"] for o in obs]
        ask_qtys = [o["ask_qty"] for o in obs]
        spreads = [o["ask"] - o["bid"] for o in obs]   # per-observation, paired

        snapshots.append({
            "recv_ts_ns": max(o["recv_ts_ns"] for o in obs),
            "symbol": symbol,
            "window_start_ns": ws,
            "window_end_ns": ws + cadence_ns,
            "bid_open": bids[0],
            "bid_high": max(bids),
            "bid_low": min(bids),
            "bid_close": bids[-1],
            "ask_open": asks[0],
            "ask_high": max(asks),
            "ask_low": min(asks),
            "ask_close": asks[-1],
            "bid_qty_last": bid_qtys[-1],
            "bid_qty_min": min(bid_qtys),
            "bid_qty_max": max(bid_qtys),
            "bid_qty_mean": fmean(bid_qtys),
            "ask_qty_last": ask_qtys[-1],
            "ask_qty_min": min(ask_qtys),
            "ask_qty_max": max(ask_qtys),
            "ask_qty_mean": fmean(ask_qtys),
            "spread_max": max(spreads),
            "spread_min": min(spreads),
            "spread_mean": fmean(spreads),
            "spread_last": spreads[-1],
            "update_count": len(obs),
        })

    snapshots.sort(key=lambda s: (s["symbol"], s["window_start_ns"]))
    return snapshots
