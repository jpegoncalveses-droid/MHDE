"""Brain depth primitive (Phase 1 step 3b): summarize capture's ``depth_state``
top-N book snapshots into RAW, SEPARABLE within-window primitives.

depth_state is the order book sampled periodically (one top-20 snapshot per
synced symbol every few seconds). A base-cadence window therefore holds N samples;
each per-sample observable collapses onto the window via single-field summaries
ONLY (the bookTicker rule — no composed/cross-field features).

NO-BIAS contract (information vs interpretation):
  * PER-LEVEL ladder, levels 2-20 — L1 (best bid/ask) is bookTicker's (2a) domain,
    so depth contributes only the deeper book. Per level, per side: price OHLC and
    qty last/min/max/mean. (Subsumes "book reach": the level-20 price.)
  * FULL-BOOK (levels 1-20) per-SAMPLE total qty -> max/min only. The mean total is
    RECOVERABLE from the per-level qty means by linearity, so it is omitted; max/min
    of the per-sample sums are IRRECOVERABLE (max of sums != sum of maxes) and kept.
  * FULL-BOOK per-SAMPLE total notional (Σ price·qty) -> mean/max/min, all
    irrecoverable (the per-sample product cannot be rebuilt from separate price/qty
    summaries — the trades-notional rule).
  * provenance: ``sample_count``, ``update_id_last`` (venue sequence of the latest
    sample), ``recv_ts_ns`` (max sample arrival), window bounds.

FORWARD-ONLY: a depth_state sample has no venue wall-clock (the book is an
aggregate of many diffs), so the bucket keys on ``event_time_ms`` = recv ARRIVAL
ms — a window is the set of samples observed within it, never a retroactive book.

FORBIDDEN here (Phase 3): imbalance / ratios, normalization (z-score / rank),
thresholds, slope / shape, mid / micro-price — every engineered signal OVER these
raw summaries.

Pure: no I/O, deterministic. ``bucket_depth`` is the whole surface.
"""
from __future__ import annotations

from statistics import fmean
from typing import Any, Iterable, Mapping

_MS_TO_NS = 1_000_000

#: Per-level ladder spans levels [LADDER_FROM .. TOP_N]; L1 is bookTicker's domain.
LADDER_FROM = 2
TOP_N = 20
_PRICE_SUMMARIES = ("price_open", "price_high", "price_low", "price_close")
_QTY_SUMMARIES = ("qty_last", "qty_min", "qty_max", "qty_mean")
_LEVEL_SUMMARIES = _PRICE_SUMMARIES + _QTY_SUMMARIES


def level_field_names() -> list[str]:
    """The per-level ladder field names, in (side, level, summary) order. The
    persistence schema mirrors this exactly (pinned by a test)."""
    names: list[str] = []
    for side in ("bid", "ask"):
        for lvl in range(LADDER_FROM, TOP_N + 1):
            names += [f"{side}_l{lvl}_{suffix}" for suffix in _LEVEL_SUMMARIES]
    return names


def _window_start_ns(event_time_ms: int, cadence_ns: int) -> int:
    return (event_time_ms * _MS_TO_NS // cadence_ns) * cadence_ns


def bucket_depth(rows: Iterable[Mapping[str, Any]], *, cadence_ns: int) -> list[dict]:
    """Group clean depth_state samples into ``(symbol, window)`` raw-summary snapshots.

    Each input row (from :func:`reader.read_new_depth_state`): ``recv_ts_ns``,
    ``symbol``, ``event_time_ms`` (recv arrival ms), ``update_id``, ``bids`` /
    ``asks`` as ``[(price, qty), ...]`` float tuples in venue (best-first) order.
    """
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        ws = _window_start_ns(r["event_time_ms"], cadence_ns)
        groups.setdefault((r["symbol"], ws), []).append(dict(r))

    snapshots: list[dict] = []
    for (symbol, ws), samples in groups.items():
        # Arrival order within the window (event_time_ms, recv_ts_ns) ascending.
        samples.sort(key=lambda r: (r["event_time_ms"], r["recv_ts_ns"]))
        snap = {
            "recv_ts_ns": max(s["recv_ts_ns"] for s in samples),
            "symbol": symbol,
            "window_start_ns": ws,
            "window_end_ns": ws + cadence_ns,
            "sample_count": len(samples),
            "update_id_last": samples[-1]["update_id"],
        }
        for side, key in (("bid", "bids"), ("ask", "asks")):
            _emit_ladder(snap, side, key, samples)
            _emit_totals(snap, side, key, samples)
        snapshots.append(snap)

    snapshots.sort(key=lambda s: (s["symbol"], s["window_start_ns"]))
    return snapshots


def _emit_ladder(snap: dict, side: str, key: str, samples: list[dict]) -> None:
    """Per-level price OHLC + qty last/min/max/mean for levels 2-20. A level absent
    in every sample of the window (thin book) is emitted as null for all summaries."""
    for lvl in range(LADDER_FROM, TOP_N + 1):
        idx = lvl - 1
        prices = [s[key][idx][0] for s in samples if len(s[key]) >= lvl]
        qtys = [s[key][idx][1] for s in samples if len(s[key]) >= lvl]
        p = f"{side}_l{lvl}_"
        if prices:
            snap[p + "price_open"] = prices[0]
            snap[p + "price_high"] = max(prices)
            snap[p + "price_low"] = min(prices)
            snap[p + "price_close"] = prices[-1]
            snap[p + "qty_last"] = qtys[-1]
            snap[p + "qty_min"] = min(qtys)
            snap[p + "qty_max"] = max(qtys)
            snap[p + "qty_mean"] = fmean(qtys)
        else:
            for suffix in _LEVEL_SUMMARIES:
                snap[p + suffix] = None


def _emit_totals(snap: dict, side: str, key: str, samples: list[dict]) -> None:
    """Full-book (all captured levels) per-SAMPLE totals, then window-summarized:
    qty -> max/min (mean is recoverable); notional -> mean/max/min (irrecoverable)."""
    qty_totals = [sum(q for _p, q in s[key]) for s in samples]
    notional_totals = [sum(p * q for p, q in s[key]) for s in samples]
    snap[f"{side}_total_qty_max"] = max(qty_totals)
    snap[f"{side}_total_qty_min"] = min(qty_totals)
    snap[f"{side}_total_notional_mean"] = fmean(notional_totals)
    snap[f"{side}_total_notional_max"] = max(notional_totals)
    snap[f"{side}_total_notional_min"] = min(notional_totals)
