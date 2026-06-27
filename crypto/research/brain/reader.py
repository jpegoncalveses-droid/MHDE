"""Brain capture readers: READ-ONLY pyarrow consumers of the capture tape.

One generic core (:func:`_read_dataset_rows`) reads any capture dataset under
``<capture_root>/<dataset>/symbol=*/date=*`` filtered to ``recv_ts_ns > cursor``
and globally sorted by ``recv_ts_ns`` (part files are disjoint but hash-named, so
filename order is not time order). Per-source functions then map the terse venue
field names to clean names and cast the VARCHAR numerics to float.

Symbols are UTF-8 (CJK / digit-leading exist on Binance USDT-M) — read through
the Hive partitioning / in-row ``s`` field, never an ASCII regex.

Bucket (event-time) field per source — all clean rows expose ``event_time_ms``
as the field the primitives bucket on:
  * aggTrade   -> trade time ``T`` (the match time), also exposes event_time_ms = E
  * bookTicker -> event time ``E``
  * markPrice  -> event time ``E``  (FOOTGUN: markPrice ``T`` is the *next funding
    time*, a future stamp — never bucket on it; it is kept as ``next_funding_time_ms``)
  * forceOrder -> event time ``E``  (``T`` trade time also exposed)

Strictly read-only: these never write anything, anywhere.
"""
from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone
from typing import Optional, Sequence

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.compute as pc
import pyarrow.parquet as pq

from crypto.research.brain import config as cfg

logger = logging.getLogger("mhde.crypto.brain.reader")

_MS_PER_DAY = 86_400_000
#: One UTC day of slack subtracted from the recv cursor before deriving the lower-bound
#: ``date=`` partition. The ``date=`` of a recv-aligned dataset is keyed on the live-WS
#: event time E (which trails recv by sub-second) or on recv itself, so a row received
#: just after a UTC midnight can sit in the prior day's partition (E at 23:59 on D-1).
#: ``date=`` is day-granular, so one day of margin can NEVER drop such a row while still
#: pruning all history older than the cursor's prior day (memory stays O(2 days), not
#: O(history)). Far exceeds any flush (30s) or clock skew.
_DATE_PRUNE_MARGIN_NS = 86_400 * 1_000_000_000

#: Datasets whose ``date=`` partition is RECV-ALIGNED — keyed on recv_ts itself
#: (depth_state) or on the live-WS event time E that trails recv within one flush
#: interval (the WS firehose). For these, a recv-cursor lower-date filter can never drop
#: an in-window row, so date-pruning is safe. EXCLUDES the venue-time-decoupled datasets:
#: klines_1h (``date=`` keyed on bar openTime — a backfill writes 90-day-old partitions
#: at recv=now) and the REST as-of series (the /futures/data ones key ``date=`` on a
#: bucket timestamp up to ~35 min behind recv). Pruning those by recv-date would silently
#: drop just-arrived rows, so they keep the full per-symbol scan (they are sparse — no
#: memory pressure). read_new_asof is one function shared by recv-aligned /fapi AND
#: decoupled /futures/data series, so the whole as-of path stays out (a per-function flag
#: could not separate them; the gate must be per-dataset, here).
_RECV_DATED_DATASETS = frozenset({
    cfg.AGGTRADE_DATASET,
    cfg.BOOKTICKER_CAPTURE_DATASET,
    cfg.MARKPRICE_CAPTURE_DATASET,
    cfg.FORCEORDER_CAPTURE_DATASET,
    cfg.DEPTH_STATE_CAPTURE_DATASET,
})


def _date_str_from_ns(ns: int) -> str:
    """UTC ``YYYY-MM-DD`` for a ns stamp — byte-identical to capture's ``date=`` label
    (``_date_str`` over ms), so a lexicographic ``>=`` compare equals a chronological one."""
    day = (ns // 1_000_000) // _MS_PER_DAY
    return datetime.fromtimestamp(day * 86_400, tz=timezone.utc).strftime("%Y-%m-%d")


def _scoped_partition_files(
    base: pathlib.Path, symbols: Sequence[str], lower_date: Optional[str],
) -> list[pathlib.Path]:
    """The existing parquet FILE paths under the batch's ``symbol=S/date=D`` partitions with
    ``D >= lower_date`` (``lower_date is None`` keeps every date — the venue-time-decoupled
    klines / as-of case). ``ds.dataset()`` only accepts files, not directories, for an explicit
    source list.

    This is what makes a batched construction CHEAP: ``ds.dataset()`` over this list lists only
    the batch's fragments, never the whole ``symbol=*/date=*`` tree, so the in-memory fragment
    list pyarrow builds before any filter scales with the BATCH, not the dataset total.

    Symbols are taken VERBATIM into the path (UTF-8 / CJK / digit-leading safe — never a regex)
    and DEDUPED preserving order — a repeated symbol must not list (and so read) its files twice
    (the whole-tree path used ``symbol.isin([...])``, i.e. set membership, so each file once).
    A missing ``symbol=`` or ``date=`` dir is skipped, not an error: a sparse source or an empty
    in-window batch member simply contributes no files."""
    files: list[pathlib.Path] = []
    for sym in dict.fromkeys(symbols):          # dedup, order-preserving (no double-count)
        sym_dir = base / f"symbol={sym}"
        if not sym_dir.is_dir():
            continue
        for date_dir in sorted(sym_dir.glob("date=*")):
            if not date_dir.is_dir():
                continue
            if lower_date is not None and date_dir.name[len("date="):] < lower_date:
                continue
            files.extend(sorted(date_dir.glob("*.parquet")))
    return files


def _open_scoped_dataset(paths: list[str]) -> Optional[ds.Dataset]:
    """``ds.dataset()`` over an explicit FILE LIST, tolerant of a corrupt LEAD fragment.

    pyarrow seeds the dataset schema from the FIRST file, OUTSIDE the per-fragment ``to_table``
    guard — so a truncated head (real capture parts are ``part-<uuid>.parquet`` and the
    lexicographically-first can be the corrupt one) would crash the whole read and, with the
    cursor left unadvanced, re-crash every tick: a permanent per-source stall. On that failure,
    move the first READABLE file to the front (corrupt files stay in the list and are still
    skipped at ``to_table``) and retry. Returns ``None`` iff NO file is readable."""
    try:
        return ds.dataset(paths, format="parquet", partitioning="hive")
    except (pa.ArrowInvalid, OSError):
        pass
    for i, p in enumerate(paths):
        try:
            pq.ParquetFile(p)                    # footer read; raises on a truncated/missing file
            ordered = [p] + paths[:i] + paths[i + 1:]
            return ds.dataset(ordered, format="parquet", partitioning="hive")
        except (pa.ArrowInvalid, OSError) as exc:  # also guards a deleted-after-stat race on retry
            logger.warning(
                "brain reader: skipping unreadable lead capture fragment for schema "
                "inference: %s (%s: %s)", p, type(exc).__name__, exc)
            continue
    return None


def _read_dataset_rows(
    capture_root: str,
    capture_dataset: str,
    after_recv_ts_ns: int,
    columns: list[str],
    symbols: Optional[Sequence[str]],
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Read terse rows from a capture dataset, ``recv_ts_ns > cursor``, sorted asc.

    A bounded read prunes on BOTH Hive partition columns AND (optionally) the forward
    extent:
      * ``symbol=`` — when ``symbols`` is given, only those partitions are opened (PR #53).
      * ``date=`` — when the dataset is RECV-ALIGNED (see ``_RECV_DATED_DATASETS``), a
        lower-bound ``date >= date(cursor - 1 day)`` prunes every partition older than the
        cursor's prior day, so memory stops scaling with total history length. Skipped for
        venue-time-decoupled datasets (klines/as-of), where ``date=`` can lag the recv
        cursor by minutes..days and pruning would drop just-arrived rows.
      * FORWARD CEILING — when ``before_recv_ts_ns`` is given, only rows with
        ``recv_ts_ns <= before_recv_ts_ns`` are materialized. This is the upper bound the
        lower-bound cursor always lacked: a pass reads at most ``(cursor, cursor+W]`` of
        tape, so a cursor that has fallen N hours behind reads O(W) rows, NOT the whole
        ``(cursor, now]`` backlog (the OOM death-spiral). ``None`` keeps the unbounded read
        (the deliberate from-zero full-backfill path).
    (Full-universe ``symbols=None`` still scans every selected-date fragment; chunking by
    symbol-batch is the runner's job. The date prune still applies, bounding memory.)

    FRAGMENT-ROBUST: a corrupt/truncated parquet is SKIPPED and LOGGED (the partition
    is recorded as missing data), never silently dropped and never crashing the read.
    """
    base = pathlib.Path(capture_root, capture_dataset)
    if not base.exists():
        return []

    # DATE-partition floor (recv-aligned datasets only): a row's `date=` tracks recv within
    # one flush, so prune every partition older than the margin-adjusted cursor day. Gated
    # cursor > margin so a from-zero read keeps all history. klines / as-of opt out — their
    # `date=` is venue-time-decoupled, so pruning by recv-date would drop just-arrived rows.
    lower_date: Optional[str] = None
    if (capture_dataset in _RECV_DATED_DATASETS
            and after_recv_ts_ns > _DATE_PRUNE_MARGIN_NS):
        lower_date = _date_str_from_ns(after_recv_ts_ns - _DATE_PRUNE_MARGIN_NS)

    if symbols is not None:
        symbols = list(symbols)                  # materialize once (read twice: scope + frag_filter)
        # SCOPED CONSTRUCTION (the structural fix): build the dataset over ONLY the batch's
        # `symbol=/date=` partition dirs, so the fragment list pyarrow materializes BEFORE any
        # filter scales with the BATCH's fragment count, not the dataset total. The whole-tree
        # construction was the wall that pinned the runner at the 2G cgroup on the un-date-pruned
        # klines (226k frags) and OOM'd on depth_state (~3M) — a cost W/row-filter never touched.
        scoped = _scoped_partition_files(base, symbols, lower_date)
        if not scoped:
            return []
        dataset = _open_scoped_dataset([str(f) for f in scoped])
        if dataset is None:                      # every scoped fragment corrupt/absent
            return []
    else:
        # Full-universe / from-zero read (deliberate, e.g. a one-shot backfill): kept whole-tree.
        if not any(base.rglob("*.parquet")):
            return []
        dataset = ds.dataset(str(base), format="parquet", partitioning="hive")

    row_filter = pc.field("recv_ts_ns") > after_recv_ts_ns
    if before_recv_ts_ns is not None:
        # FORWARD CEILING (the time-twin of the symbol/date prunes): pushed into the
        # row predicate so each fragment's to_table decodes only rows up to the ceiling,
        # bounding the materialized slice to W of tape regardless of the cursor gap.
        row_filter = row_filter & (pc.field("recv_ts_ns") <= before_recv_ts_ns)
    frag_filter = row_filter
    if symbols is not None:
        # Redundant after the path scoping above (every scoped fragment already matches),
        # kept as a defense-in-depth equivalence guarantee on the `symbol=` Hive column.
        frag_filter = frag_filter & pc.field("symbol").isin(list(symbols))
    if lower_date is not None:
        # The load-bearing date prune on the whole-tree (symbols=None) path; redundant but
        # harmless on the scoped path (which already dropped the older `date=` dirs).
        frag_filter = frag_filter & (pc.field("date") >= lower_date)

    tables: list[pa.Table] = []
    for frag in dataset.get_fragments(filter=frag_filter):
        try:
            table = frag.to_table(columns=columns, filter=row_filter)
        except (pa.ArrowInvalid, OSError) as exc:  # corrupt/truncated/unreadable file
            logger.warning(
                "brain reader: skipping unreadable capture fragment (data absent for "
                "this partition): %s (%s: %s)", frag.path, type(exc).__name__, exc)
            continue
        if table.num_rows:
            tables.append(table)
    if not tables:
        return []
    return pa.concat_tables(tables).sort_by([("recv_ts_ns", "ascending")]).to_pylist()


def _safe_float(v) -> Optional[float]:
    """Cast a VARCHAR venue numeric to float; empty string / None -> None (null)."""
    if v is None or v == "":
        return None
    return float(v)


def read_new_aggtrades(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Clean aggTrade dicts with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, trade_time_ms, agg_id, price, qty,
    is_buyer_maker, taker_buy. Primitive buckets on ``trade_time_ms``.
    """
    rows = _read_dataset_rows(capture_root, cfg.AGGTRADE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "a", "s", "p", "q", "T", "m"], symbols,
                              before_recv_ts_ns=before_recv_ts_ns)
    out: list[dict] = []
    for r in rows:
        m = bool(r["m"])
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "trade_time_ms": int(r["T"]),
            "agg_id": int(r["a"]),
            "price": float(r["p"]),   # VARCHAR -> float
            "qty": float(r["q"]),     # VARCHAR -> float
            "is_buyer_maker": m,
            "taker_buy": not m,       # m=False -> taker BUY
        })
    return out


def read_new_bookticker(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Clean bookTicker dicts with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, transaction_time_ms, bid, bid_qty,
    ask, ask_qty. Primitive buckets on ``event_time_ms`` (E).
    """
    rows = _read_dataset_rows(capture_root, cfg.BOOKTICKER_CAPTURE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "T", "s", "b", "B", "a", "A"], symbols,
                              before_recv_ts_ns=before_recv_ts_ns)
    out: list[dict] = []
    for r in rows:
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "transaction_time_ms": int(r["T"]),
            "bid": float(r["b"]),       # VARCHAR -> float
            "bid_qty": float(r["B"]),
            "ask": float(r["a"]),
            "ask_qty": float(r["A"]),
        })
    return out


def read_new_markprice(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Clean markPrice dicts with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, mark, index, settle, funding,
    next_funding_time_ms. Primitive buckets on ``event_time_ms`` (E). FOOTGUN:
    the venue ``T`` is the *next funding time* (future) -> ``next_funding_time_ms``,
    NEVER the bucket key.
    """
    rows = _read_dataset_rows(capture_root, cfg.MARKPRICE_CAPTURE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "s", "p", "i", "P", "r", "T"], symbols,
                              before_recv_ts_ns=before_recv_ts_ns)
    out: list[dict] = []
    for r in rows:
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "mark": float(r["p"]),      # VARCHAR -> float
            "index": float(r["i"]),
            "settle": float(r["P"]),
            "funding": float(r["r"]),
            "next_funding_time_ms": int(r["T"]),  # future funding time, NOT an event time
        })
    return out


def read_new_forceorder(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Clean forceOrder (liquidation) dicts, ``recv_ts_ns > after``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, trade_time_ms, side, qty, price.
    Primitive buckets on ``event_time_ms`` (E). ``side`` is the raw venue ``S``
    ('BUY' / 'SELL'); only the fields the primitive needs are projected (avoids
    the flattened single-letter collisions ``o``/``l``/``z``).
    """
    rows = _read_dataset_rows(capture_root, cfg.FORCEORDER_CAPTURE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "E", "T", "s", "S", "q", "p"], symbols,
                              before_recv_ts_ns=before_recv_ts_ns)
    out: list[dict] = []
    for r in rows:
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "trade_time_ms": int(r["T"]),
            "side": r["S"],             # raw venue side: 'BUY' / 'SELL'
            "qty": float(r["q"]),       # VARCHAR -> float
            "price": float(r["p"]),
        })
    return out


def read_new_depth_state(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Clean depth_state book-snapshot rows with ``recv_ts_ns > after``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, update_id, bids, asks. ``bids``/``asks``
    are ``[(price, qty), ...]`` float tuples (venue best-first order), parsed from the
    stored ``[[price_str, qty_str], ...]`` ladders. Only the synced (``valid``) book
    is ever written, so no validity filter is needed here.

    FORWARD-ONLY: ``event_time_ms`` is the recv ARRIVAL ms (``recv_ts_ns // 1e6``).
    A depth_state sample is a reconstructed book with NO single venue wall-clock (it
    aggregates many diffs; ``update_id`` is the last applied sequence, not a time), so
    the sample's only time is its arrival — there is nothing to look ahead from.
    """
    rows = _read_dataset_rows(capture_root, cfg.DEPTH_STATE_CAPTURE_DATASET, after_recv_ts_ns,
                              ["recv_ts_ns", "s", "update_id", "b", "a"], symbols,
                              before_recv_ts_ns=before_recv_ts_ns)
    out: list[dict] = []
    for r in rows:
        recv = int(r["recv_ts_ns"])
        out.append({
            "recv_ts_ns": recv,
            "symbol": r["s"],
            "event_time_ms": recv // 1_000_000,    # ARRIVAL — forward-only bucket key
            "update_id": int(r["update_id"]),
            "bids": [(float(p), float(q)) for p, q in (r["b"] or [])],
            "asks": [(float(p), float(q)) for p, q in (r["a"] or [])],
        })
    return out


def read_new_asof(
    capture_root: str,
    capture_dataset: str,
    *,
    value_map: dict,
    asof_time_col: str,
    symbol_col: str = "s",
    int_map: Optional[dict] = None,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Clean rows for a REST present-state ("as-of") series, recv-order.

    FORWARD-ONLY, uniform with klines: ``event_time_ms`` (the bucket / visibility
    key) is the recv-time ARRIVAL ms (``recv_ts_ns // 1e6``), NOT the venue time —
    a value is visible only once the brain has observed it, never retroactively in
    a window before its arrival (which would be a lookahead, e.g. a batched
    futures_data fetch re-delivering old 5-min buckets). The venue time-key
    (``asof_time_col``) is kept as ``asof_event_time_ms`` — a stored staleness
    signal, no longer the visibility gate.

    ``value_map`` maps clean float field name -> venue VARCHAR column; ``int_map``
    (optional) maps clean int field name -> venue int column. ``symbol_col`` is the
    in-row symbol field (``s`` or ``pair``). Empty-string numerics -> None.
    """
    int_map = int_map or {}
    columns = (["recv_ts_ns", symbol_col, asof_time_col]
               + list(value_map.values()) + list(int_map.values()))
    rows = _read_dataset_rows(capture_root, capture_dataset, after_recv_ts_ns,
                              columns, symbols, before_recv_ts_ns=before_recv_ts_ns)
    out: list[dict] = []
    for r in rows:
        recv = int(r["recv_ts_ns"])
        clean = {
            "recv_ts_ns": recv,
            "symbol": r[symbol_col],
            "event_time_ms": recv // 1_000_000,        # ARRIVAL — bucket/visibility key
            "asof_event_time_ms": int(r[asof_time_col]),  # venue time — staleness signal only
        }
        for clean_name, venue_col in value_map.items():
            clean[clean_name] = _safe_float(r[venue_col])
        for clean_name, venue_col in int_map.items():
            clean[clean_name] = int(r[venue_col])
        out.append(clean)
    return out


# klines_1h on-disk columns (the 'ignore' venue field is dropped by capture).
_KLINES_COLUMNS = ["recv_ts_ns", "s", "openTime", "open", "high", "low", "close",
                   "volume", "closeTime", "quoteVolume", "trades", "takerBuyBase", "takerBuyQuote"]


def read_new_klines(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
    before_recv_ts_ns: Optional[int] = None,
) -> list[dict]:
    """Clean klines_1h bars with ``recv_ts_ns > after_recv_ts_ns``, recv-order.

    Keys: recv_ts_ns, symbol, event_time_ms, + the native bar fields (open, high,
    low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote) and
    the bar identity (open_time, close_time).

    FORWARD-ONLY (load-bearing): ``event_time_ms`` is the recv-time ARRIVAL ms
    (``recv_ts_ns // 1e6``), NOT the bar's openTime/closeTime. A 1h bar is
    REST-backfilled, so its closeTime can be long before the brain observed it;
    keying the as-of on closeTime would expose a bar before it was available
    (lookahead). The bar's own times are kept only as identity fields.
    """
    rows = _read_dataset_rows(capture_root, cfg.KLINES_CAPTURE_DATASET, after_recv_ts_ns,
                              _KLINES_COLUMNS, symbols, before_recv_ts_ns=before_recv_ts_ns)
    out: list[dict] = []
    for r in rows:
        recv = int(r["recv_ts_ns"])
        out.append({
            "recv_ts_ns": recv,
            "symbol": r["s"],
            "event_time_ms": recv // 1_000_000,   # ARRIVAL time, NOT the bar time
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
            "quote_volume": float(r["quoteVolume"]),
            "trades": int(r["trades"]),
            "taker_buy_base": float(r["takerBuyBase"]),
            "taker_buy_quote": float(r["takerBuyQuote"]),
            "open_time": int(r["openTime"]),
            "close_time": int(r["closeTime"]),
        })
    return out
