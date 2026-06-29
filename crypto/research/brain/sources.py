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
    asof, bookticker, depth, forceorder, markprice, reader, store, trades,
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
    capture_dataset: str                          # CAPTURE-side dataset dir (for universe
    #                                               enumeration: <capture_root>/<capture_dataset>/
    #                                               symbol=*). NOTE this is the capture dir
    #                                               ('aggTrade', 'depth_state'), NOT the brain
    #                                               store ``dataset`` ('trades', 'depth').


TRADES = SourceSpec(
    dataset=cfg.TRADES_DATASET, reader_name=cfg.TRADES_READER,
    read_fn=reader.read_new_aggtrades, bucket_fn=trades.bucket_trades,
    schema=store.TRADES_SNAPSHOT_SCHEMA, event_time_key="trade_time_ms",
    count_fn=lambda s: s["trade_count"], capture_dataset=cfg.AGGTRADE_DATASET,
)

BOOKTICKER = SourceSpec(
    dataset=cfg.BOOKTICKER_DATASET, reader_name=cfg.BOOKTICKER_READER,
    read_fn=reader.read_new_bookticker, bucket_fn=bookticker.bucket_bookticker,
    schema=store.BOOKTICKER_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: s["update_count"], capture_dataset=cfg.BOOKTICKER_CAPTURE_DATASET,
)

MARKPRICE = SourceSpec(
    dataset=cfg.MARKPRICE_DATASET, reader_name=cfg.MARKPRICE_READER,
    read_fn=reader.read_new_markprice, bucket_fn=markprice.bucket_markprice,
    schema=store.MARKPRICE_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: s["update_count"], capture_dataset=cfg.MARKPRICE_CAPTURE_DATASET,
)

FORCEORDER = SourceSpec(
    dataset=cfg.FORCEORDER_DATASET, reader_name=cfg.FORCEORDER_READER,
    read_fn=reader.read_new_forceorder, bucket_fn=forceorder.bucket_forceorder,
    schema=store.FORCEORDER_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: s["liq_buy_count"] + s["liq_sell_count"],
    capture_dataset=cfg.FORCEORDER_CAPTURE_DATASET,
)

# -- AS-OF (REST present-state) sources -----------------------------------------
# Sparse point-in-time series share one generic reader + primitive, parameterized
# by the venue field map. Each emits one as-of value per window (count == 1).

def _asof_reader(capture_dataset, value_map, asof_time_col, symbol_col="s", int_map=None):
    def _read(capture_root, after_recv_ts_ns=0, symbols=None, before_recv_ts_ns=None):
        return reader.read_new_asof(
            capture_root, capture_dataset, value_map=value_map, asof_time_col=asof_time_col,
            symbol_col=symbol_col, int_map=int_map,
            after_recv_ts_ns=after_recv_ts_ns, symbols=symbols,
            before_recv_ts_ns=before_recv_ts_ns)
    return _read


def _asof_bucket(value_fields, tiebreak_fields=()):
    def _bucket(rows, *, cadence_ns):
        return asof.bucket_asof(rows, cadence_ns=cadence_ns, value_fields=value_fields,
                                tiebreak_fields=tiebreak_fields)
    return _bucket


def _asof_spec(name, *, value_map, asof_time_col, schema, symbol_col="s", int_map=None):
    value_fields = list(value_map) + list(int_map or {})
    return SourceSpec(
        dataset=name, reader_name=name,
        read_fn=_asof_reader(name, value_map, asof_time_col, symbol_col, int_map),
        # Arrival-keyed (event_time_ms = recv): a batched fetch delivers many
        # observations at one recv -> collapse to the latest by VENUE time.
        bucket_fn=_asof_bucket(value_fields, tiebreak_fields=("asof_event_time_ms",)),
        schema=schema, event_time_key="event_time_ms", count_fn=lambda s: 1,
        # As-of capture dir == the series name (basis stores under symbol=<pair>).
        capture_dataset=name,
    )


_LS_MAP = {"long_account": "longAccount", "short_account": "shortAccount",
           "long_short_ratio": "longShortRatio"}

OPEN_INTEREST = _asof_spec(
    cfg.OPEN_INTEREST_DATASET, value_map={"open_interest": "openInterest"},
    asof_time_col="time", schema=store.OPEN_INTEREST_SNAPSHOT_SCHEMA)

PREMIUM_INDEX = _asof_spec(
    cfg.PREMIUM_INDEX_DATASET,
    value_map={"mark_price": "markPrice", "index_price": "indexPrice",
               "estimated_settle_price": "estimatedSettlePrice",
               "last_funding_rate": "lastFundingRate", "interest_rate": "interestRate"},
    int_map={"next_funding_time": "nextFundingTime"},
    asof_time_col="time", schema=store.PREMIUM_INDEX_SNAPSHOT_SCHEMA)

GLOBAL_LS_ACCOUNT = _asof_spec(
    cfg.GLOBAL_LS_ACCOUNT_DATASET, value_map=_LS_MAP, asof_time_col="timestamp",
    schema=store.GLOBAL_LS_ACCOUNT_SNAPSHOT_SCHEMA)

TOP_LS_ACCOUNT = _asof_spec(
    cfg.TOP_LS_ACCOUNT_DATASET, value_map=_LS_MAP, asof_time_col="timestamp",
    schema=store.TOP_LS_ACCOUNT_SNAPSHOT_SCHEMA)

TOP_LS_POSITION = _asof_spec(
    cfg.TOP_LS_POSITION_DATASET, value_map=_LS_MAP, asof_time_col="timestamp",
    schema=store.TOP_LS_POSITION_SNAPSHOT_SCHEMA)

TAKER_LS_RATIO = _asof_spec(
    cfg.TAKER_LS_RATIO_DATASET,
    value_map={"buy_sell_ratio": "buySellRatio", "buy_vol": "buyVol", "sell_vol": "sellVol"},
    asof_time_col="timestamp", schema=store.TAKER_LS_RATIO_SNAPSHOT_SCHEMA)

BASIS = _asof_spec(
    cfg.BASIS_DATASET,
    value_map={"index_price": "indexPrice", "futures_price": "futuresPrice",
               "basis": "basis", "basis_rate": "basisRate",
               "annualized_basis_rate": "annualizedBasisRate"},
    asof_time_col="timestamp", symbol_col="pair", schema=store.BASIS_SNAPSHOT_SCHEMA)

#: The seven REST present-state ("as-of") scalar series.
ASOF_SOURCES = [OPEN_INTEREST, PREMIUM_INDEX, GLOBAL_LS_ACCOUNT, TOP_LS_ACCOUNT,
                TOP_LS_POSITION, TAKER_LS_RATIO, BASIS]

# klines_1h: the hourly-context bar — a MULTI-FIELD as-of source. Same as-of
# mechanism, but its reader keys event_time on recv ARRIVAL (forward-only; the
# bar is REST-backfilled so closeTime can precede arrival), and a backfill page
# delivers many bars at one recv -> break the tie on close_time (latest bar).
_KLINES_FIELDS = ["open", "high", "low", "close", "volume", "quote_volume", "trades",
                  "taker_buy_base", "taker_buy_quote", "open_time", "close_time"]


def _klines_reader(capture_root, after_recv_ts_ns=0, symbols=None, before_recv_ts_ns=None):
    return reader.read_new_klines(capture_root, after_recv_ts_ns=after_recv_ts_ns,
                                  symbols=symbols, before_recv_ts_ns=before_recv_ts_ns)


KLINES = SourceSpec(
    dataset=cfg.KLINES_DATASET, reader_name=cfg.KLINES_DATASET,
    read_fn=_klines_reader,
    bucket_fn=_asof_bucket(_KLINES_FIELDS, tiebreak_fields=("close_time",)),
    schema=store.KLINES_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: 1, capture_dataset=cfg.KLINES_CAPTURE_DATASET,
)

# depth (step 3b): periodically-sampled top-N book -> per-level + full-book depth
# primitives. Arrival-keyed (event_time_ms = recv ms), forward-only by construction;
# within-window event count = the number of book samples in the window.
DEPTH = SourceSpec(
    dataset=cfg.DEPTH_DATASET, reader_name=cfg.DEPTH_DATASET,
    read_fn=reader.read_new_depth_state, bucket_fn=depth.bucket_depth,
    schema=store.DEPTH_SNAPSHOT_SCHEMA, event_time_key="event_time_ms",
    count_fn=lambda s: s["sample_count"],
    # reader reads the depth_state capture dir (NOT the raw 'depth' diff stream).
    capture_dataset=cfg.DEPTH_STATE_CAPTURE_DATASET,
)

#: All sources wired into the continuous runner, keyed by dataset name.
#:
#: DEPTH is DEFERRED OUT (KI-159): it reads ``depth_state``, an expire-only 2-day rolling
#: buffer deliberately excluded from the capture compactor (``FIREHOSE_PRUNABLE_DATASETS``),
#: so its ~3M uncompacted fragments OOM ``ds.dataset()`` at construction — a harder wall than
#: the scoped-construction fix addresses, needing its own fragmentation plan (a depth-aware
#: compactor over sealed-only partitions, or a latest-snapshot read) before it can rejoin. The
#: ``DEPTH`` SourceSpec + primitive above stay defined so re-wiring it here is a one-line change
#: once that lands. Do NOT re-add it to this set without that plan — it re-creates the stall.
SOURCES: dict[str, SourceSpec] = {
    s.dataset: s for s in (TRADES, BOOKTICKER, MARKPRICE, FORCEORDER, *ASOF_SOURCES,
                           KLINES)
}

#: SLOW sources for the runner's sub-cadence (Fix 1): klines_1h + the 7 REST as-of series.
#: Sparse in rows but footer-scanned every tick — the runner runs them every
#: ``BRAIN_SLOW_SOURCE_EVERY_N_TICKS`` ticks. The four dense recv-dated sources
#: (trades, bookticker, markprice, forceorder) are deliberately ABSENT — they stay every-tick.
#: Membership is by brain store dataset name (the runner classifies each spec by ``spec.dataset``;
#: a spec whose dataset is not listed defaults to FAST, so an unknown source is never starved).
SLOW_SOURCE_DATASETS: frozenset = frozenset(s.dataset for s in (*ASOF_SOURCES, KLINES))
