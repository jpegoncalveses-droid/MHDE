"""Brain AS-OF primitive: bucket sparse observations into base-cadence windows by
the bucket key (``event_time_ms``) and keep the latest (as-of) value.

FORWARD-ONLY: callers key ``event_time_ms`` on recv ARRIVAL (``recv_ts_ns // 1e6``),
not the venue time, so a value is visible only once the brain observed it — never
retroactively in a window before its arrival (a lookahead). The venue time is
retained as ``asof_event_time_ms``, a stored staleness signal, no longer the
visibility gate. This is uniform for the REST present-state series and klines.

These series (open interest, premium/funding, long/short ratios, basis, the 1h
bar) are point-in-time values sampled sparsely — never more than one observation
per 60s window in normal operation. So the within-window "summary" is degenerate:
the window holds the single (latest) observation's RAW field values, NOT an OHLC
summary (which would be identical copies). Windows with no observation get no
snapshot — a consumer forward-fills at read time by taking the last window <= its
query time (the as-of value). No forward-fill is synthesized here.

NO-BIAS (information vs interpretation): the value fields are the venue's OWN
fields, kept verbatim. Native ratios/rates (longShortRatio, buySellRatio,
basisRate, …) are RAW venue information, not engineered signals. Nothing is
computed over the values — the snapshot is provenance/bounds + the as-of
timestamp + the raw field values.

Pure: no I/O, deterministic. ``bucket_asof`` is the whole surface.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

_MS_TO_NS = 1_000_000


def _window_start_ns(event_time_ms: int, cadence_ns: int) -> int:
    return (event_time_ms * _MS_TO_NS // cadence_ns) * cadence_ns


def bucket_asof(
    rows: Iterable[Mapping[str, Any]],
    *,
    cadence_ns: int,
    value_fields: Sequence[str],
    tiebreak_fields: Sequence[str] = (),
) -> list[dict]:
    """Group clean as-of rows into ``(symbol, window)`` snapshots of the as-of value.

    Each input row: recv_ts_ns, symbol, event_time_ms (the bucket key — recv
    arrival, forward-only), an optional ``asof_event_time_ms`` (venue staleness
    time; defaults to event_time_ms when absent, e.g. klines), and each name in
    ``value_fields`` (float or int, or None for an absent venue value). Per
    ``(symbol, window)`` the LATEST observation (by event time, then recv_ts_ns,
    then ``tiebreak_fields``) supplies the value — this also dedups overlapping
    re-fetches and collapses a batched fetch to its latest-by-venue-time value.

    ``tiebreak_fields`` break a remaining tie when event time and recv_ts_ns are
    equal (e.g. klines: a backfill page delivers many bars at one recv_ts_ns, so
    the highest ``close_time`` bar is the deterministic as-of for that window).
    """
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        ws = _window_start_ns(r["event_time_ms"], cadence_ns)
        groups.setdefault((r["symbol"], ws), []).append(dict(r))

    snapshots: list[dict] = []
    for (symbol, ws), obs in groups.items():
        obs.sort(key=lambda r: (r["event_time_ms"], r["recv_ts_ns"],
                                *(r[f] for f in tiebreak_fields)))
        last = obs[-1]   # the as-of observation for this window
        snap = {
            "recv_ts_ns": last["recv_ts_ns"],
            "symbol": symbol,
            "window_start_ns": ws,
            "window_end_ns": ws + cadence_ns,
            # The staleness signal: the value's own as-of time when the source
            # provides one (the venue time-key); else the bucket/event time (e.g.
            # klines, whose as-of instant IS its recv arrival).
            "asof_event_time_ms": last.get("asof_event_time_ms", last["event_time_ms"]),
        }
        for f in value_fields:
            snap[f] = last[f]
        snapshots.append(snap)

    snapshots.sort(key=lambda s: (s["symbol"], s["window_start_ns"]))
    return snapshots
