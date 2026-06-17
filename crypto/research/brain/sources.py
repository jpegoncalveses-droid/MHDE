"""Brain source registry: one declarative :class:`SourceSpec` per upstream
capture dataset, wiring its reader, primitive, store schema, bucket field, and
event-count function. The generic :func:`pipeline.run_once` is driven entirely by
a spec, so adding a source is one entry here — mirroring capture_core's
``rest_series`` registry pattern.

Each source has its OWN store dataset and its OWN registry cursor, so they
advance independently. ``event_time_key`` is the clean-row field the primitive
buckets on (trades/forceOrder use trade/event time; all of bookTicker, markPrice,
forceOrder bucket on the venue event time ``E``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pyarrow as pa

from crypto.research.brain import config as cfg
from crypto.research.brain import (
    bookticker, forceorder, markprice, reader, store, trades,
)


@dataclass(frozen=True)
class SourceSpec:
    """A single brain source: how to read it, summarize it, and persist it."""
    dataset: str                                  # brain store dataset + bookkeeping key
    reader_name: str                              # registry cursor name (own cursor)
    read_fn: Callable[..., list]                  # (capture_root, after_recv_ts_ns, symbols)
    bucket_fn: Callable[..., list]                # (rows, *, cadence_ns) -> snapshots
    schema: pa.Schema                             # store snapshot schema
    event_time_key: str                           # clean-row field the bucket keys on
    count_fn: Callable[[Mapping[str, Any]], int]  # snapshot -> within-window event count


TRADES = SourceSpec(
    dataset=cfg.TRADES_DATASET, reader_name=cfg.TRADES_READER,
    read_fn=reader.read_new_aggtrades, bucket_fn=trades.bucket_trades,
    schema=store.TRADES_SNAPSHOT_SCHEMA, event_time_key="trade_time_ms",
    count_fn=lambda s: s["trade_count"],
)

BOOKTICKER = SourceSpec(
    dataset=cfg.BOOKTICKER_DATASET, reader_name=cfg.BOOKTICKER_READER,
    read_fn=reader.read_new_bookticker, bucket_fn=bookticker.bucket_bookticker,
    schema=store.BOOKTICKER_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: s["update_count"],
)

MARKPRICE = SourceSpec(
    dataset=cfg.MARKPRICE_DATASET, reader_name=cfg.MARKPRICE_READER,
    read_fn=reader.read_new_markprice, bucket_fn=markprice.bucket_markprice,
    schema=store.MARKPRICE_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: s["update_count"],
)

FORCEORDER = SourceSpec(
    dataset=cfg.FORCEORDER_DATASET, reader_name=cfg.FORCEORDER_READER,
    read_fn=reader.read_new_forceorder, bucket_fn=forceorder.bucket_forceorder,
    schema=store.FORCEORDER_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: s["liq_buy_count"] + s["liq_sell_count"],
)

#: All sources keyed by dataset name.
SOURCES: dict[str, SourceSpec] = {
    s.dataset: s for s in (TRADES, BOOKTICKER, MARKPRICE, FORCEORDER)
}
