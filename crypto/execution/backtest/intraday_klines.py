"""Intraday (1-minute) klines research store + backfill driver.

Scope — this is **research infrastructure for the intraday faithful
replay**, deliberately kept out of the production path:

  * The klines live in a **separate research DB**
    (``data/research/intraday.duckdb`` by default), *never* in the
    production ``mhde.duckdb`` and *never* registered in
    ``crypto.schema.ALL_SCHEMAS``. The table is created on demand
    (``CREATE TABLE IF NOT EXISTS``) by :func:`connect_research_db`.
  * No live timer ingests this data; the backfill is run manually,
    paced, and outside the live predict/export windows.

The table is keyed on ``(symbol, interval, open_time)`` so re-runs UPSERT
idempotently. The backfill driver takes an injected Binance client (with a
``fetch_klines(symbol, interval, start_dt, end_dt)`` method) so it is
unit-testable without the network; per-symbol failures (e.g. an unknown
symbol) are skipped and logged rather than aborting the run.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

import duckdb

logger = logging.getLogger("mhde.crypto.intraday_klines")

#: Default research DB path (gitignored; NOT the production mhde.duckdb).
RESEARCH_DB_PATH = "data/research/intraday.duckdb"

#: Minutes per interval string, for gap detection.
_INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60}

SCHEMA_CRYPTO_KLINES_INTRADAY = """
CREATE TABLE IF NOT EXISTS crypto_klines_intraday (
    symbol VARCHAR NOT NULL,
    interval VARCHAR NOT NULL,
    open_time TIMESTAMP NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    PRIMARY KEY (symbol, interval, open_time)
);
"""


def connect_research_db(
    path: str = RESEARCH_DB_PATH, *, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """Open the research klines DB, creating the table when writable.

    ``read_only=True`` opens for replay (the DB must already exist); the
    table is not (re)created in that mode.
    """
    if not read_only:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = duckdb.connect(path, read_only=read_only)
    if not read_only:
        conn.execute(SCHEMA_CRYPTO_KLINES_INTRADAY)
    return conn


def upsert_klines(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    interval: str,
    rows: Sequence[dict[str, Any]],
) -> int:
    """Idempotently UPSERT ``rows`` for one ``(symbol, interval)``.

    Each row is a dict with ``open_time`` (tz-aware or naive UTC datetime)
    and ``open/high/low/close/volume``. Returns the number of rows written.
    """
    if not rows:
        return 0
    payload = [
        (
            symbol, interval, r["open_time"],
            float(r["open"]), float(r["high"]), float(r["low"]),
            float(r["close"]), float(r["volume"]),
        )
        for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO crypto_klines_intraday
            (symbol, interval, open_time, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume
        """,
        payload,
    )
    return len(payload)


def _count_gaps(rows: Sequence[dict[str, Any]], interval: str) -> int:
    """Count holes in a sorted minute series (consecutive ``open_time`` gaps
    larger than one interval). Used only for diagnostics."""
    step = timedelta(minutes=_INTERVAL_MINUTES.get(interval, 1))
    gaps = 0
    prev: Optional[datetime] = None
    for r in rows:
        t = r["open_time"]
        if prev is not None and (t - prev) > step:
            gaps += 1
        prev = t
    return gaps


def backfill_intraday(
    client: Any,
    conn: duckdb.DuckDBPyConnection,
    *,
    symbols: Sequence[str],
    interval: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Backfill 1-minute klines for ``symbols`` into the research DB.

    Per symbol: fetch via ``client.fetch_klines`` (which paginates 1000/req
    and rate-limits internally), UPSERT idempotently, and tally gaps. A
    symbol whose fetch raises (unknown symbol, transient API error) is
    logged and skipped — the run continues with the remaining symbols.

    Returns a summary dict: ``rows_written``, ``symbols_ok``,
    ``symbols_skipped`` (list), ``gaps`` (total holes seen).
    """
    rows_written = 0
    symbols_ok = 0
    symbols_skipped: list[str] = []
    total_gaps = 0

    for symbol in symbols:
        try:
            rows = client.fetch_klines(symbol, interval, start, end)
        except Exception as exc:  # unknown symbol / transient API error
            logger.warning("backfill-intraday: skipping %s (%s: %s)",
                           symbol, type(exc).__name__, exc)
            symbols_skipped.append(symbol)
            continue
        rows = sorted(rows, key=lambda r: r["open_time"])
        gaps = _count_gaps(rows, interval)
        if gaps:
            logger.warning("backfill-intraday: %s has %d gap(s) in [%s, %s]",
                           symbol, gaps, start, end)
        n = upsert_klines(conn, symbol, interval, rows)
        rows_written += n
        total_gaps += gaps
        symbols_ok += 1
        logger.info("backfill-intraday: %s wrote %d rows (%d gaps)", symbol, n, gaps)

    return {
        "rows_written": rows_written,
        "symbols_ok": symbols_ok,
        "symbols_skipped": symbols_skipped,
        "gaps": total_gaps,
    }
