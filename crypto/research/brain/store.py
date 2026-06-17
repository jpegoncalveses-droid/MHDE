"""Brain parquet event store: the trades-primitive snapshots, Hive-partitioned.

Mirrors the capture_core store convention — explicit ``pa.schema``, pyarrow +
zstd, ``<root>/<dataset>/symbol=<S>/date=<YYYY-MM-DD>/part-<uuid>.parquet`` keyed
on the *event* time (window start, UTC). Unlike capture (which keeps raw venue
strings lossless), the brain persists NUMERIC within-window summaries, so the
schema is int64 / float64 / string only.

The schema field names are the persistence half of the NO-BIAS guardrail
(INFORMATION vs INTERPRETATION): raw per-event quantities (incl. notional, which
is irrecoverable from the qty/price summaries) and within-window single-field
summaries, plus immutable provenance/bounds — but NO engineered signals over the
summaries (ratios/imbalance, normalization, thresholds, selection).

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

from crypto.research.brain import config as cfg

#: Trades snapshot schema — provenance/bounds + raw separable primitives.
TRADES_SNAPSHOT_SCHEMA = pa.schema([
    # provenance / immutable bounds
    ("recv_ts_ns", pa.int64()),       # max receive ns in the window (cursor high-water)
    ("symbol", pa.string()),
    ("window_start_ns", pa.int64()),  # immutable bound (event-time floor)
    ("window_end_ns", pa.int64()),    # immutable bound (start + cadence)
    # raw separable primitives (per-event quantities + single-field summaries,
    # taker split kept separate)
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

_MS_PER_DAY = 86_400_000


def _date_str_from_ns(ns: int) -> str:
    """UTC ``YYYY-MM-DD`` for an event-time nanosecond stamp (window start)."""
    day = (ns // 1_000_000) // _MS_PER_DAY
    return datetime.fromtimestamp(day * 86_400, tz=timezone.utc).strftime("%Y-%m-%d")


def _partition(snap: Mapping[str, Any]) -> str:
    # UTF-8 symbol straight into the path — no ASCII regex, no normalization.
    return f"symbol={snap['symbol']}/date={_date_str_from_ns(snap['window_start_ns'])}"


def write_snapshots(root: str, snapshots: Iterable[Mapping[str, Any]]) -> list[str]:
    """Persist snapshot dicts to the trades dataset; one part file per partition.

    Returns the list of written parquet paths (empty if there were no rows).
    Each snapshot must carry exactly the :data:`TRADES_SNAPSHOT_SCHEMA` fields.
    """
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for snap in snapshots:
        buckets.setdefault(_partition(snap), []).append(snap)

    written: list[str] = []
    for subdir, rows in buckets.items():
        table = pa.Table.from_pylist(list(rows), schema=TRADES_SNAPSHOT_SCHEMA)
        out_dir = os.path.join(root, cfg.TRADES_DATASET, subdir)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"part-{uuid4().hex}.parquet")
        pq.write_table(table, path, compression=cfg.PARQUET_COMPRESSION)
        written.append(path)
    return written


def read_snapshots(root: str, symbol: Optional[str] = None) -> list[dict]:
    """Read persisted snapshots back as dicts (optionally one symbol).

    Used for round-trip fidelity and downstream consumption. Files are read by
    their physical schema and concatenated; callers that need event order should
    sort by ``window_start_ns`` / ``recv_ts_ns``.
    """
    base = pathlib.Path(root, cfg.TRADES_DATASET)
    if not base.exists():
        return []
    if symbol is None:
        files = base.rglob("*.parquet")
    else:
        files = base.glob(f"symbol={symbol}/**/*.parquet")
    rows: list[dict] = []
    for fp in sorted(files):
        # Read by the file's PHYSICAL schema (via ParquetFile, not read_table) so
        # pyarrow does NOT infer the Hive ``symbol=`` partition as a dictionary
        # column and collide with our in-row string ``symbol`` (capture_core
        # dodges this by naming its in-row field ``s``; we keep ``symbol``).
        rows.extend(pq.ParquetFile(str(fp)).read().to_pylist())
    return rows
