"""Brain subsystem (Phase 1): an isolated reader + event store over the
capture_core tape.

Phase 1 step 1 is a vertical slice — read capture's ``aggTrade`` dataset
READ-ONLY, summarize each ``(symbol, base-cadence window)`` into RAW, separable
within-window primitives, persist them to the brain's OWN parquet event store,
and advance a resumable cursor in a SQLite-WAL registry. No continuous runner,
no systemd, nothing deployed.

Isolation: the brain is its own writer domain (``data/research/brain/``). It
never opens ``mhde.duckdb``, the engine DB, or capture's store for writing, and
is not registered in ``crypto.schema.ALL_SCHEMAS``. See :mod:`config` for the
full contract.
"""
