"""Brain subsystem (Phase 1): an isolated reader + event store over the
capture_core tape.

Each source reads one capture dataset READ-ONLY, summarizes each
``(symbol, base-cadence window)`` into RAW, separable within-window primitives,
persists them to the brain's OWN parquet event store (one dataset per source),
and advances a resumable per-source cursor in a SQLite-WAL registry. A single
generic pipeline is driven by a declarative :class:`sources.SourceSpec`. No
continuous runner, no systemd, nothing deployed.

Sources fall into two shapes. EVENT STREAMS (WS, dense — aggregate within a
window): step 1 ``aggTrade`` -> ``trades``; step 2a ``bookTicker`` ->
``bookticker``, ``markPrice`` -> ``markprice``, ``forceOrder`` -> ``forceorder``.
AS-OF series (REST present-state, sparse — point-in-time values valid AS OF a
timestamp, one per window at most): step 2b ``open_interest``, ``premium_index``,
``global_ls_account``, ``top_ls_account``, ``top_ls_position``, ``taker_ls_ratio``,
``basis`` -> same-named datasets. As-of snapshots hold the latest raw value per
window (not an OHLC summary), and venue-native ratios/rates are RAW information.

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
