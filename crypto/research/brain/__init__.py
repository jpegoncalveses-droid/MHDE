"""Brain subsystem (Phase 1): an isolated reader + event store over the
capture_core tape.

Each source reads one capture dataset READ-ONLY, summarizes each
``(symbol, base-cadence window)`` into RAW, separable within-window primitives,
persists them to the brain's OWN parquet event store (one dataset per source),
and advances a resumable per-source cursor in a SQLite-WAL registry. A single
generic pipeline is driven by a declarative :class:`sources.SourceSpec`. No
continuous runner, no systemd, nothing deployed.

Sources: step 1 — ``aggTrade`` -> ``trades``; step 2a — ``bookTicker`` ->
``bookticker`` (bid/ask OHLC + qty summaries + bid-ask spread), ``markPrice`` ->
``markprice`` (mark/index/settle OHLC + funding summaries), ``forceOrder`` ->
``forceorder`` (liquidations split by side, like trades).

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
