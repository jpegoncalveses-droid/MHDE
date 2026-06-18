"""Brain subsystem (Phase 1): an isolated reader + event store over the
capture_core tape.

Each source reads one capture dataset READ-ONLY, summarizes each
``(symbol, base-cadence window)`` into RAW, separable within-window primitives,
persists them to the brain's OWN parquet event store (one dataset per source),
and advances a resumable per-source cursor in a SQLite-WAL registry. A single
generic pipeline is driven by a declarative :class:`sources.SourceSpec`. No
continuous runner, no systemd, nothing deployed.

Sources fall into three shapes. EVENT STREAMS (WS, dense — aggregate within a
window): step 1 ``aggTrade`` -> ``trades``; step 2a ``bookTicker`` ->
``bookticker``, ``markPrice`` -> ``markprice``, ``forceOrder`` -> ``forceorder``.
AS-OF series (REST present-state, sparse — point-in-time values valid AS OF a
timestamp, one per window at most): step 2b ``open_interest``, ``premium_index``,
``global_ls_account``, ``top_ls_account``, ``top_ls_position``, ``taker_ls_ratio``,
``basis`` -> same-named datasets; step 2c ``klines_1h`` -> ``klines_1h``, the
hourly-context bar (a MULTI-FIELD as-of source storing the native bar fields +
openTime/closeTime identity). As-of snapshots hold the latest raw value per
window (not an OHLC summary), and venue-native ratios/rates are RAW information.
SAMPLED STATE (a periodically-sampled top-N book): step 3b ``depth_state`` ->
``depth`` — many book samples per window, summarized as the per-level ladder
(levels 2-20; L1 is bookTicker's) price OHLC + qty last/min/max/mean, plus the
full-book per-sample total qty (max/min — the mean total is recoverable) and
total notional (mean/max/min — irrecoverable). Imbalance / micro-price / any
engineered book signal is Phase 3.
All as-of sources are FORWARD-ONLY (uniform): they key visibility on recv arrival,
NOT the venue time — a value is visible only once the brain observed it, never
retroactively in a window before its arrival (a lookahead; acute for klines,
whose REST-backfilled closeTime can long precede arrival, and for batched
futures_data fetches re-delivering old buckets). The venue time is retained as
``asof_event_time_ms``, a stored staleness signal; a batched fetch collapses to
its latest-by-venue-time value.

Isolation: the brain is its own writer domain (``data/research/brain/``). It
never opens ``mhde.duckdb``, the engine DB, or capture's store for writing, and
is not registered in ``crypto.schema.ALL_SCHEMAS``. See :mod:`config` for the
full contract.

NO-BIAS line (inherited by every step) — INFORMATION vs INTERPRETATION, not
"presence of a product": persist raw per-event quantities that cannot be
reconstructed from the separate window summaries (e.g. notional ``price*qty``,
irrecoverable from ``Σqty`` + price OHLC) plus within-window single-field
summaries; defer to Phase 3 every engineered signal computed OVER those
summaries — ratios/imbalance, normalization (rank/z-score), thresholds, selection.
"""
