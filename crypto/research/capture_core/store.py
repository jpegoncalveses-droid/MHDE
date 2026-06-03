"""Capture-core parquet store: one dataset per stream, Hive partitions, zstd.

A :class:`RawDatasetWriter` buffers raw event rows per partition and flushes a
parquet part file on the **earlier of** ``flush_interval_s`` (age) or
``flush_max_bytes`` (size). Partitioning is derived from each row by a
``partition_fn`` (e.g. ``symbol=<S>/date=<YYYY-MM-DD>`` keyed on the *event*
time, so a file maps to the exchange day, not the local flush day).

Lossless by design: numeric venue fields that arrive as strings (``p``/``q``)
are stored as strings — no float coercion — and the diff update ids are kept
verbatim. A local ``recv_ts_ns`` is added at dequeue time.

This module NEVER opens DuckDB. It writes only under the given root.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Mapping
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from crypto.research.capture_core import config as cfg

# -- per-stream schemas (fixed; venue numeric strings kept as strings) --

#: aggTrade combined-stream ``data`` payload + local ``recv_ts_ns``.
AGGTRADE_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("e", pa.string()),       # event type
    ("E", pa.int64()),        # event time (ms)
    ("a", pa.int64()),        # aggregate trade id
    ("s", pa.string()),       # symbol
    ("p", pa.string()),       # price (venue string, lossless)
    ("q", pa.string()),       # quantity (venue string, lossless)
    ("f", pa.int64()),        # first trade id
    ("l", pa.int64()),        # last trade id
    ("T", pa.int64()),        # trade time (ms)
    ("m", pa.bool_()),        # is buyer the market maker
])

_PRICE_LEVELS = pa.list_(pa.list_(pa.string()))  # [[price, qty], ...] venue strings

#: depthUpdate diff event (raw; zero-qty levels kept verbatim).
DEPTH_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("e", pa.string()),
    ("E", pa.int64()),        # event time (ms)
    ("T", pa.int64()),        # transaction time (ms)
    ("s", pa.string()),
    ("U", pa.int64()),        # first update id in event
    ("u", pa.int64()),        # final update id in event
    ("pu", pa.int64()),       # previous final update id (continuity field)
    ("b", _PRICE_LEVELS),     # bid deltas
    ("a", _PRICE_LEVELS),     # ask deltas
])

#: bookTicker best bid/ask event.
BOOKTICKER_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("e", pa.string()),
    ("u", pa.int64()),        # order book updateId
    ("s", pa.string()),
    ("b", pa.string()),       # best bid price
    ("B", pa.string()),       # best bid qty
    ("a", pa.string()),       # best ask price
    ("A", pa.string()),       # best ask qty
    ("T", pa.int64()),        # transaction time
    ("E", pa.int64()),        # event time
])

#: forceOrder (liquidation) — the inner ``o`` object flattened + event time.
FORCEORDER_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("E", pa.int64()),
    ("s", pa.string()),
    ("S", pa.string()),       # side
    ("o", pa.string()),       # order type
    ("f", pa.string()),       # time in force
    ("q", pa.string()),       # original quantity
    ("p", pa.string()),       # price
    ("ap", pa.string()),      # average price
    ("X", pa.string()),       # order status
    ("l", pa.string()),       # last filled qty
    ("z", pa.string()),       # cumulative filled qty
    ("T", pa.int64()),        # trade time
])

#: markPriceUpdate array element (one row per symbol per push).
MARKPRICE_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("e", pa.string()),
    ("E", pa.int64()),
    ("s", pa.string()),
    ("p", pa.string()),       # mark price
    ("i", pa.string()),       # index price
    ("P", pa.string()),       # estimated settle price
    ("r", pa.string()),       # funding rate
    ("T", pa.int64()),        # next funding time
])

#: REST order-book snapshot used to seed/resync the diff stream (own dataset).
DEPTH_SNAPSHOT_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("s", pa.string()),       # symbol (from the request; REST omits it)
    ("lastUpdateId", pa.int64()),
    ("E", pa.int64()),
    ("T", pa.int64()),
    ("b", _PRICE_LEVELS),
    ("a", _PRICE_LEVELS),
])

#: Gap manifest: records a hole in capture; never backfilled.
GAP_SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("stream", pa.string()),
    ("gap_start_ms", pa.int64()),
    ("gap_end_ms", pa.int64()),
    ("reason", pa.string()),
    ("recorded_recv_ts_ns", pa.int64()),
])


def _date_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _aggtrade_partition(row: Mapping[str, Any]) -> str:
    return f"symbol={row['s']}/date={_date_str(row['E'])}"


def _symbol_event_partition(row: Mapping[str, Any]) -> str:
    """``symbol=<s>/date=<event-day>`` keyed on the event-time field ``E``."""
    return f"symbol={row['s']}/date={_date_str(row['E'])}"


def _gap_partition(row: Mapping[str, Any]) -> str:
    return f"date={_date_str(row['gap_start_ms'])}"


def _estimate_row_bytes(row: Mapping[str, Any]) -> int:
    """Cheap uncompressed-size proxy used only to trigger size-based flushes."""
    return sum(len(str(v)) for v in row.values()) + 8 * len(row)


@dataclass
class _PartitionBuffer:
    started_at: float
    rows: list[dict] = field(default_factory=list)
    nbytes: int = 0


class RawDatasetWriter:
    """Buffer raw rows per partition; flush parquet parts on age or size."""

    def __init__(
        self,
        root: str,
        dataset: str,
        schema: pa.Schema,
        partition_fn: Callable[[Mapping[str, Any]], str],
        *,
        flush_interval_s: float = cfg.FLUSH_INTERVAL_S,
        flush_max_bytes: int = cfg.FLUSH_MAX_BYTES,
        compression: str = cfg.PARQUET_COMPRESSION,
        now_fn: Callable[[], float] = monotonic,
    ) -> None:
        self._root = root
        self._dataset = dataset
        self._schema = schema
        self._partition_fn = partition_fn
        self._flush_interval_s = flush_interval_s
        self._flush_max_bytes = flush_max_bytes
        self._compression = compression
        self._now = now_fn
        self._buffers: dict[str, _PartitionBuffer] = {}
        self.rows_written = 0
        self.files_written = 0

    def append(self, row: Mapping[str, Any]) -> None:
        subdir = self._partition_fn(row)
        buf = self._buffers.get(subdir)
        if buf is None:
            buf = _PartitionBuffer(started_at=self._now())
            self._buffers[subdir] = buf
        buf.rows.append(dict(row))
        buf.nbytes += _estimate_row_bytes(row)

    def flush_due(self) -> int:
        """Flush partitions past the size or age threshold. Returns count flushed."""
        now = self._now()
        due = [
            subdir for subdir, buf in self._buffers.items()
            if buf.nbytes >= self._flush_max_bytes
            or (now - buf.started_at) >= self._flush_interval_s
        ]
        for subdir in due:
            self._flush(subdir)
        return len(due)

    def flush_all(self) -> int:
        """Flush every buffered partition (e.g. on shutdown). Returns count."""
        subdirs = list(self._buffers)
        for subdir in subdirs:
            self._flush(subdir)
        return len(subdirs)

    def _flush(self, subdir: str) -> None:
        buf = self._buffers.pop(subdir, None)
        if buf is None or not buf.rows:
            return
        table = pa.Table.from_pylist(buf.rows, schema=self._schema)
        out_dir = os.path.join(self._root, self._dataset, subdir)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"part-{uuid4().hex}.parquet")
        pq.write_table(table, path, compression=self._compression)
        self.files_written += 1
        self.rows_written += len(buf.rows)


def aggtrade_writer(root: str, **kwargs: Any) -> RawDatasetWriter:
    """Writer for the ``aggTrade`` dataset (partitioned by symbol + event date)."""
    return RawDatasetWriter(root, "aggTrade", AGGTRADE_SCHEMA, _aggtrade_partition, **kwargs)


def depth_writer(root: str, **kwargs: Any) -> RawDatasetWriter:
    """Writer for the raw ``depth`` diff dataset (symbol + event date)."""
    return RawDatasetWriter(root, "depth", DEPTH_SCHEMA, _symbol_event_partition, **kwargs)


def bookticker_writer(root: str, **kwargs: Any) -> RawDatasetWriter:
    """Writer for the ``bookTicker`` dataset (symbol + event date)."""
    return RawDatasetWriter(root, "bookTicker", BOOKTICKER_SCHEMA,
                            _symbol_event_partition, **kwargs)


def forceorder_writer(root: str, **kwargs: Any) -> RawDatasetWriter:
    """Writer for the ``forceOrder`` (liquidation) dataset (symbol + event date)."""
    return RawDatasetWriter(root, "forceOrder", FORCEORDER_SCHEMA,
                            _symbol_event_partition, **kwargs)


def markprice_writer(root: str, **kwargs: Any) -> RawDatasetWriter:
    """Writer for the ``markPrice`` dataset (one row per symbol per push)."""
    return RawDatasetWriter(root, "markPrice", MARKPRICE_SCHEMA,
                            _symbol_event_partition, **kwargs)


def depth_snapshot_writer(root: str, **kwargs: Any) -> RawDatasetWriter:
    """Writer for the REST ``depth_snapshot`` seeding dataset (symbol + event date)."""
    return RawDatasetWriter(root, "depth_snapshot", DEPTH_SNAPSHOT_SCHEMA,
                            _symbol_event_partition, **kwargs)


def gap_writer(root: str, **kwargs: Any) -> RawDatasetWriter:
    """Writer for the ``_gaps`` manifest dataset (partitioned by date)."""
    return RawDatasetWriter(root, "_gaps", GAP_SCHEMA, _gap_partition, **kwargs)
