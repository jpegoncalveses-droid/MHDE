"""Shared retry-aware opener for the crypto-trading-engine DuckDB (read-only).

The MHDE monitors read the engine's DuckDB **read-only** — a deliberate,
scoped exception to INTERFACE.md's "no cross-system DB access" rule (ADR-020).
The engine's per-minute ``monitor`` phase (and the daily ``entry`` phase)
briefly hold the write lock, and a read-only open that lands in that window
raises ``duckdb.IOException("... Could not set lock ...")``. The monitors used
to surface that transient collision as a false ``"engine DuckDB not reachable"``
RED even though the engine was perfectly healthy.

This opener is the single source of truth for those opens. It retries a
lock conflict with **sub-second** backoff (total budget ~1.85s — far shorter
than ``storage/db.py``'s 30/60/120s, which targets MHDE's own long-running
nightly writer, not the engine's sub-second lock) and **fails fast** on a
genuinely missing database. Path resolves from ``CRYPTO_ENGINE_DB_PATH``.

Lock vs. missing-DB is decided on the duckdb 1.5.2 messages captured in the
fix's Step-0 diagnostic:
  * lock collision  -> ``IO Error: Could not set lock on file ...`` (retry)
  * missing DB      -> ``... in read-only mode: database does not exist`` (fail fast)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import duckdb

logger = logging.getLogger("mhde.monitoring.engine_db")

DEFAULT_ENGINE_DB = "/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb"
ENGINE_DB_ENV = "CRYPTO_ENGINE_DB_PATH"

#: Sub-second backoff: the engine holds the write lock only for the brief
#: monitor/entry phase, so a few fast retries clears it. Initial attempt + these
#: gives 5 tries over ~1.85s.
_LOCK_RETRY_DELAYS_SEC = (0.1, 0.25, 0.5, 1.0)

#: Honest message for the persistent-failure RED branches in the monitors:
#: after exhausting the retry budget we genuinely cannot tell lock contention
#: from a downed engine, so we say exactly that instead of blaming the engine.
ENGINE_DB_UNREADABLE_MSG = (
    "engine DB unreadable after retries (lock contention or engine down); "
    "timer state unknown"
)


def _is_lock_conflict(exc: duckdb.IOException) -> bool:
    """True for a write-lock collision (retryable), False for e.g. a missing DB."""
    msg = str(exc).lower()
    return "lock" in msg and "does not exist" not in msg


def open_engine_db_readonly(path: Optional[str] = None) -> duckdb.DuckDBPyConnection:
    """Open the crypto-engine DuckDB read-only, retrying on a write-lock conflict.

    Resolves ``path`` from the argument, else ``CRYPTO_ENGINE_DB_PATH``, else
    the default engine DB path. Retries a lock conflict with sub-second backoff;
    re-raises immediately for any non-lock failure (missing DB, corruption, …)
    and re-raises the lock error once the retry budget is exhausted.
    """
    db_path = path or os.environ.get(ENGINE_DB_ENV, DEFAULT_ENGINE_DB)
    attempts = len(_LOCK_RETRY_DELAYS_SEC) + 1
    for attempt in range(attempts):
        try:
            return duckdb.connect(db_path, read_only=True)
        except duckdb.IOException as exc:
            if not _is_lock_conflict(exc) or attempt == attempts - 1:
                raise
            wait = _LOCK_RETRY_DELAYS_SEC[attempt]
            logger.warning(
                "engine DuckDB read-only open blocked by lock; retrying in %.2fs "
                "(attempt %d/%d): %s",
                wait, attempt + 1, attempts, exc,
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover
