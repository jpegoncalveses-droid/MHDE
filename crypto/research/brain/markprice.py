"""Brain markPrice primitive: bucket mark-price updates into base-cadence windows
by EVENT time and emit RAW, SEPARABLE within-window summaries.

NO-BIAS (information vs interpretation): within-window single-field summaries of
the native venue fields only — mark / index / est-settle price OHLC, funding rate
last/min/max, and the last next-funding-time. No engineered mark-index premium
signal, no ratios, no normalization (the funding rate ``r`` already encodes the
premium relationship natively, and premium is a separate REST series).

FOOTGUN: the venue ``T`` is the *next funding time* (a future stamp), NOT an
event time — buckets are keyed on ``event_time_ms`` (E). ``next_funding_time_ms``
is summarized as a raw field (last), never used for windowing.

Pure: no I/O, deterministic. ``bucket_markprice`` is the whole surface.
"""
from __future__ import annotations

from statistics import fmean
from typing import Any, Iterable, Mapping

_MS_TO_NS = 1_000_000


def _window_start_ns(event_time_ms: int, cadence_ns: int) -> int:
    return (event_time_ms * _MS_TO_NS // cadence_ns) * cadence_ns


def bucket_markprice(rows: Iterable[Mapping[str, Any]], *, cadence_ns: int) -> list[dict]:
    """Group clean markPrice dicts into ``(symbol, window)`` raw-summary snapshots.

    Each input row: recv_ts_ns, symbol, event_time_ms, mark, index, settle,
    funding, next_funding_time_ms. Windows are keyed on ``event_time_ms``.
    """
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        ws = _window_start_ns(r["event_time_ms"], cadence_ns)
        groups.setdefault((r["symbol"], ws), []).append(dict(r))

    snapshots: list[dict] = []
    for (symbol, ws), obs in groups.items():
        obs.sort(key=lambda r: (r["event_time_ms"], r["recv_ts_ns"]))
        marks = [o["mark"] for o in obs]
        indices = [o["index"] for o in obs]
        settles = [o["settle"] for o in obs]
        fundings = [o["funding"] for o in obs]

        snapshots.append({
            "recv_ts_ns": max(o["recv_ts_ns"] for o in obs),
            "symbol": symbol,
            "window_start_ns": ws,
            "window_end_ns": ws + cadence_ns,
            "mark_open": marks[0],
            "mark_high": max(marks),
            "mark_low": min(marks),
            "mark_close": marks[-1],
            "index_open": indices[0],
            "index_high": max(indices),
            "index_low": min(indices),
            "index_close": indices[-1],
            "settle_open": settles[0],
            "settle_high": max(settles),
            "settle_low": min(settles),
            "settle_close": settles[-1],
            "funding_last": fundings[-1],
            "funding_min": min(fundings),
            "funding_max": max(fundings),
            "next_funding_time_last": obs[-1]["next_funding_time_ms"],
            "update_count": len(obs),
        })

    snapshots.sort(key=lambda s: (s["symbol"], s["window_start_ns"]))
    return snapshots
