"""Brain parquet event store: per-source within-window snapshots, Hive-partitioned.

Mirrors the capture_core store convention — explicit ``pa.schema``, pyarrow +
zstd, ``<root>/<dataset>/symbol=<S>/date=<YYYY-MM-DD>/part-<uuid>.parquet`` keyed
on the *event* time (window start, UTC). Unlike capture (which keeps raw venue
strings lossless), the brain persists NUMERIC within-window summaries, so the
schema is int64 / float64 / string only.

One schema per source dataset (trades, bookticker, markprice, forceorder); the
write/read functions are generic over ``(dataset, schema)``. The schema field
names are the persistence half of the NO-BIAS guardrail (INFORMATION vs
INTERPRETATION): raw per-event quantities (incl. irrecoverable cross-field ones
like notional and bid-ask spread) and within-window single-field summaries, plus
immutable provenance/bounds — but NO engineered signals over the summaries
(ratios/imbalance, normalization, thresholds, selection).

This module writes ONLY under the given ``root`` and NEVER opens DuckDB, the
engine DB, or capture's store.
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

# Common provenance / immutable bounds prefix shared by every snapshot schema.
_PROVENANCE = [
    ("recv_ts_ns", pa.int64()),       # max receive ns in the window (cursor high-water)
    ("symbol", pa.string()),
    ("window_start_ns", pa.int64()),  # immutable bound (event-time floor)
    ("window_end_ns", pa.int64()),    # immutable bound (start + cadence)
]

#: Trades snapshot — per-event quantities (vol, notional) + price/qty summaries.
TRADES_SNAPSHOT_SCHEMA = pa.schema(_PROVENANCE + [
    ("taker_buy_vol", pa.float64()),
    ("taker_sell_vol", pa.float64()),
    ("taker_buy_quote_vol", pa.float64()),   # raw notional (price*qty), irrecoverable downstream
    ("taker_sell_quote_vol", pa.float64()),
    ("buy_trade_count", pa.int64()),
    ("sell_trade_count", pa.int64()),
    ("trade_count", pa.int64()),
    ("price_open", pa.float64()),
    ("price_high", pa.float64()),
    ("price_low", pa.float64()),
    ("price_close", pa.float64()),
    ("qty_sum", pa.float64()),
    ("qty_max", pa.float64()),
    ("qty_mean", pa.float64()),
])

#: bookTicker snapshot — bid/ask OHLC, bid/ask qty summaries, and the bid-ask
#: SPREAD (a raw cross-field observable, irrecoverable from separate bid/ask
#: summaries because the per-instant max spread is not max(ask) - min(bid)).
BOOKTICKER_SNAPSHOT_SCHEMA = pa.schema(_PROVENANCE + [
    ("bid_open", pa.float64()),
    ("bid_high", pa.float64()),
    ("bid_low", pa.float64()),
    ("bid_close", pa.float64()),
    ("ask_open", pa.float64()),
    ("ask_high", pa.float64()),
    ("ask_low", pa.float64()),
    ("ask_close", pa.float64()),
    ("bid_qty_last", pa.float64()),
    ("bid_qty_min", pa.float64()),
    ("bid_qty_max", pa.float64()),
    ("bid_qty_mean", pa.float64()),
    ("ask_qty_last", pa.float64()),
    ("ask_qty_min", pa.float64()),
    ("ask_qty_max", pa.float64()),
    ("ask_qty_mean", pa.float64()),
    ("spread_max", pa.float64()),     # raw irrecoverable cross-field observable
    ("spread_min", pa.float64()),
    ("spread_mean", pa.float64()),
    ("spread_last", pa.float64()),
    ("update_count", pa.int64()),
])

#: markPrice snapshot — mark / index / est-settle OHLC, funding-rate summaries,
#: last next-funding-time. All native venue fields; no engineered premium signal.
MARKPRICE_SNAPSHOT_SCHEMA = pa.schema(_PROVENANCE + [
    ("mark_open", pa.float64()),
    ("mark_high", pa.float64()),
    ("mark_low", pa.float64()),
    ("mark_close", pa.float64()),
    ("index_open", pa.float64()),
    ("index_high", pa.float64()),
    ("index_low", pa.float64()),
    ("index_close", pa.float64()),
    ("settle_open", pa.float64()),
    ("settle_high", pa.float64()),
    ("settle_low", pa.float64()),
    ("settle_close", pa.float64()),
    ("funding_last", pa.float64()),
    ("funding_min", pa.float64()),
    ("funding_max", pa.float64()),
    ("next_funding_time_last", pa.int64()),  # native venue field T (next funding time, ms)
    ("update_count", pa.int64()),
])

#: forceOrder snapshot — liquidations split by side (like trades): base volume,
#: per-event notional (irrecoverable), and counts. Absent side is a raw 0.
FORCEORDER_SNAPSHOT_SCHEMA = pa.schema(_PROVENANCE + [
    ("liq_buy_vol", pa.float64()),
    ("liq_sell_vol", pa.float64()),
    ("liq_buy_quote_vol", pa.float64()),     # raw notional (price*qty), irrecoverable downstream
    ("liq_sell_quote_vol", pa.float64()),
    ("liq_buy_count", pa.int64()),
    ("liq_sell_count", pa.int64()),
])

# -- AS-OF (REST present-state) snapshot schemas --------------------------------
# Sparse point-in-time series: provenance/bounds + the as-of event timestamp +
# the venue's OWN raw fields (native ratios/rates included — they are RAW venue
# information, not engineered signals). Float fields are nullable (a venue '' ->
# null). One observation per window; no OHLC (it would be redundant).

#: provenance/bounds + the as-of (venue time-key) instant.
_ASOF_PROVENANCE = _PROVENANCE + [("asof_event_time_ms", pa.int64())]


def _asof_schema(value_specs):
    return pa.schema(_ASOF_PROVENANCE + list(value_specs))


OPEN_INTEREST_SNAPSHOT_SCHEMA = _asof_schema([
    ("open_interest", pa.float64()),
])

PREMIUM_INDEX_SNAPSHOT_SCHEMA = _asof_schema([
    ("mark_price", pa.float64()),
    ("index_price", pa.float64()),
    ("estimated_settle_price", pa.float64()),
    ("last_funding_rate", pa.float64()),
    ("interest_rate", pa.float64()),
    ("next_funding_time", pa.int64()),
])

# global / top account / top position long-short ratios share one shape.
_LS_RATIO_SPECS = [
    ("long_account", pa.float64()),
    ("short_account", pa.float64()),
    ("long_short_ratio", pa.float64()),   # native venue ratio (raw)
]
GLOBAL_LS_ACCOUNT_SNAPSHOT_SCHEMA = _asof_schema(_LS_RATIO_SPECS)
TOP_LS_ACCOUNT_SNAPSHOT_SCHEMA = _asof_schema(_LS_RATIO_SPECS)
TOP_LS_POSITION_SNAPSHOT_SCHEMA = _asof_schema(_LS_RATIO_SPECS)

TAKER_LS_RATIO_SNAPSHOT_SCHEMA = _asof_schema([
    ("buy_sell_ratio", pa.float64()),     # native venue ratio (raw)
    ("buy_vol", pa.float64()),
    ("sell_vol", pa.float64()),
])

BASIS_SNAPSHOT_SCHEMA = _asof_schema([
    ("index_price", pa.float64()),
    ("futures_price", pa.float64()),
    ("basis", pa.float64()),
    ("basis_rate", pa.float64()),         # native venue rate (raw)
    ("annualized_basis_rate", pa.float64()),
])

#: klines_1h snapshot — the hourly bar's native fields verbatim + openTime/
#: closeTime as the bar's identity. As-of keyed on recv arrival (forward-only),
#: stored sparsely at the bar cadence. NO returns/MA/momentum (Phase 3).
KLINES_SNAPSHOT_SCHEMA = _asof_schema([
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("quote_volume", pa.float64()),
    ("trades", pa.int64()),
    ("taker_buy_base", pa.float64()),
    ("taker_buy_quote", pa.float64()),
    ("open_time", pa.int64()),    # bar identity
    ("close_time", pa.int64()),   # bar identity (closed vs in-progress check downstream)
])

#: depth (step 3b) snapshot — the periodically-sampled top-N book, window-summarized.
#: Per-level ladder (levels 2-20; L1 is bookTicker's domain): price OHLC + qty
#: last/min/max/mean. Full-book per-sample totals: qty max/min (mean is recoverable
#: from the per-level qty means -> omitted), notional mean/max/min (irrecoverable).
#: All level fields are NULLABLE (a level absent in a window's samples -> null).
def _depth_level_fields():
    # levels 2-20, both sides; mirrors brain.depth LADDER_FROM=2 / TOP_N=20 (pinned
    # by test_full_book_snapshot_keys_match_schema_exactly).
    fields = []
    for side in ("bid", "ask"):
        for lvl in range(2, 21):
            fields += [
                (f"{side}_l{lvl}_price_open", pa.float64()),
                (f"{side}_l{lvl}_price_high", pa.float64()),
                (f"{side}_l{lvl}_price_low", pa.float64()),
                (f"{side}_l{lvl}_price_close", pa.float64()),
                (f"{side}_l{lvl}_qty_last", pa.float64()),
                (f"{side}_l{lvl}_qty_min", pa.float64()),
                (f"{side}_l{lvl}_qty_max", pa.float64()),
                (f"{side}_l{lvl}_qty_mean", pa.float64()),
            ]
    return fields


DEPTH_SNAPSHOT_SCHEMA = pa.schema(_PROVENANCE + [
    ("sample_count", pa.int64()),
    ("update_id_last", pa.int64()),
    ("bid_total_qty_max", pa.float64()),
    ("bid_total_qty_min", pa.float64()),
    ("ask_total_qty_max", pa.float64()),
    ("ask_total_qty_min", pa.float64()),
    ("bid_total_notional_mean", pa.float64()),  # Σ price·qty per sample (irrecoverable)
    ("bid_total_notional_max", pa.float64()),
    ("bid_total_notional_min", pa.float64()),
    ("ask_total_notional_mean", pa.float64()),
    ("ask_total_notional_max", pa.float64()),
    ("ask_total_notional_min", pa.float64()),
] + _depth_level_fields())

_MS_PER_DAY = 86_400_000
#: Date-prune skew margin (1 day): the partition date is the WINDOW-start date, but the
#: caller's cursor is a recv stamp, so prune a day below the cursor's date to never drop a
#: window whose recv just crossed midnight (mirrors the capture reader's PR #55 margin).
_DATE_PRUNE_MARGIN_NS = 86_400 * 1_000_000_000


def _date_str_from_ns(ns: int) -> str:
    """UTC ``YYYY-MM-DD`` for an event-time nanosecond stamp (window start)."""
    day = (ns // 1_000_000) // _MS_PER_DAY
    return datetime.fromtimestamp(day * 86_400, tz=timezone.utc).strftime("%Y-%m-%d")


def _partition(snap: Mapping[str, Any]) -> str:
    # UTF-8 symbol straight into the path — no ASCII regex, no normalization.
    return f"symbol={snap['symbol']}/date={_date_str_from_ns(snap['window_start_ns'])}"


def write_snapshots(
    root: str,
    dataset: str,
    schema: pa.Schema,
    snapshots: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Persist snapshot dicts to ``<root>/<dataset>``; one part file per partition.

    Returns the list of written parquet paths (empty if there were no rows).
    Each snapshot must carry exactly the ``schema`` fields.
    """
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for snap in snapshots:
        buckets.setdefault(_partition(snap), []).append(snap)

    written: list[str] = []
    for subdir, rows in buckets.items():
        table = pa.Table.from_pylist(list(rows), schema=schema)
        out_dir = os.path.join(root, dataset, subdir)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"part-{uuid4().hex}.parquet")
        pq.write_table(table, path, compression="zstd")
        written.append(path)
    return written


def read_snapshots(root: str, dataset: str, symbol: Optional[str] = None, *,
                   after_recv_ts_ns: int = 0) -> list[dict]:
    """Read persisted snapshots from ``<root>/<dataset>`` back as dicts.

    Used for round-trip fidelity and downstream consumption. Files are read by
    their physical schema (via ``ParquetFile`` so pyarrow does NOT infer the Hive
    ``symbol=`` partition as a dictionary column and collide with our in-row
    string ``symbol``). Callers needing event order sort by ``window_start_ns``.

    ``after_recv_ts_ns`` (default 0 = read everything) is a cursor-driven DATE prune:
    ``date=`` partitions older than its date (minus a 1-day skew margin) are skipped
    WITHOUT opening their files, so a forward read stops rescanning a symbol's entire
    history. It is an optimisation — partition-granular, not a row filter — and the
    structural fix for fan-out is the (separate) compactor, not this.

    CAVEAT before wiring a real recv cursor: the 1-day margin assumes a snapshot's
    ``recv_ts_ns`` lags its ``window_start_ns`` (the partition date) by < 1 day. A
    late/replayed write with > 1-day lag could be pruned while a recv-cursor caller still
    wants it — widen the margin (or pass ``after_recv_ts_ns=0``) for such sources. Only
    files under ``symbol=*/date=*`` are enumerated (everything ``write_snapshots`` emits);
    a non-conforming stray parquet elsewhere is not read.
    """
    base = pathlib.Path(root, dataset)
    if not base.exists():
        return []
    lower_date = (_date_str_from_ns(after_recv_ts_ns - _DATE_PRUNE_MARGIN_NS)
                  if after_recv_ts_ns > _DATE_PRUNE_MARGIN_NS else None)
    if symbol is None:
        sym_dirs = sorted(base.glob("symbol=*"))
    else:
        sym_dir = base / f"symbol={symbol}"
        sym_dirs = [sym_dir] if sym_dir.exists() else []
    files: list[pathlib.Path] = []
    for sym_dir in sym_dirs:
        for date_dir in sorted(sym_dir.glob("date=*")):
            if lower_date is not None and date_dir.name[len("date="):] < lower_date:
                continue                              # pruned: older than the cursor window
            files.extend(sorted(date_dir.glob("*.parquet")))
    rows: list[dict] = []
    for fp in files:
        rows.extend(pq.ParquetFile(str(fp)).read().to_pylist())
    return rows
