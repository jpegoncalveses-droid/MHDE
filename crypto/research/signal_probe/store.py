"""Signal-probe research store: schema + idempotent append.

One append-only table, ``signal_probe``, keyed on ``(symbol, ts)`` so a
re-run of the same minute UPSERTs in place (idempotent). The table is
created on demand by :func:`connect_probe_db`; it is NEVER part of
``crypto.schema.ALL_SCHEMAS`` and lives only in the separate research DB
(see :mod:`crypto.research.signal_probe.config`).

``ts`` is the **exchange** close-time of the latest closed 1-minute bar
for the cycle (a minute boundary, identical across symbols in a cycle).
Every feature column holds a **raw value** — no thresholds, no flags.
``NULL`` means the feature was not computable this cycle (e.g. an API
field Binance does not expose, or not enough lookback bars).
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

import duckdb

#: Ordered column spec: (name, duckdb_type). The collector emits a dict per
#: row keyed by these names; missing keys are written as NULL.
COLUMNS: list[tuple[str, str]] = [
    # -- identity + raw latest-minute OHLCV --
    ("ts", "TIMESTAMP"),
    ("symbol", "VARCHAR"),
    ("collected_at", "TIMESTAMP"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("quote_volume", "DOUBLE"),
    ("trades", "BIGINT"),
    ("taker_buy_base", "DOUBLE"),
    # -- funding + open interest (raw) --
    ("last_funding_rate", "DOUBLE"),
    ("predicted_funding_rate", "DOUBLE"),
    ("mark_price", "DOUBLE"),
    ("index_price", "DOUBLE"),
    ("interest_rate", "DOUBLE"),
    ("open_interest", "DOUBLE"),
    # -- ROC --
    ("roc_1m", "DOUBLE"),
    ("roc_5m", "DOUBLE"),
    ("roc_15m", "DOUBLE"),
    ("roc_60m", "DOUBLE"),
    # -- acceleration (ΔROC of equal windows) --
    ("accel_1m", "DOUBLE"),
    ("accel_5m", "DOUBLE"),
    ("accel_15m", "DOUBLE"),
    # -- move shape (largest 1m bar / net move) --
    ("move_shape_15m", "DOUBLE"),
    ("move_shape_60m", "DOUBLE"),
    # -- distance from VWAP --
    ("dist_vwap_session", "DOUBLE"),
    ("dist_vwap_rolling", "DOUBLE"),
    # -- distance from SMA --
    ("dist_sma_20", "DOUBLE"),
    ("dist_sma_50", "DOUBLE"),
    ("dist_sma_100", "DOUBLE"),
    # -- breakout vs prior high --
    ("breakout_15m", "DOUBLE"),
    ("breakout_60m", "DOUBLE"),
    ("breakout_1d", "DOUBLE"),
    # -- volume --
    ("up_down_vol_ratio_60m", "DOUBLE"),
    # -- relative volume --
    ("rvol_1m_20", "DOUBLE"),
    ("rvol_1m_60", "DOUBLE"),
    ("rvol_5m_20", "DOUBLE"),
    ("rvol_5m_60", "DOUBLE"),
    # -- taker imbalance --
    ("taker_imbalance_1m", "DOUBLE"),
    ("taker_imbalance_5m", "DOUBLE"),
    ("taker_imbalance_15m", "DOUBLE"),
    # -- trade count / size --
    ("trade_count_ratio_60", "DOUBLE"),
    ("avg_trade_size", "DOUBLE"),
    ("avg_trade_size_ratio_60", "DOUBLE"),
    # -- OI change --
    ("oi_change_1m", "DOUBLE"),
    ("oi_change_5m", "DOUBLE"),
    ("oi_change_15m", "DOUBLE"),
    ("oi_change_1h", "DOUBLE"),
    # -- return vs BTC --
    ("ret_vs_btc_5m", "DOUBLE"),
    ("ret_vs_btc_15m", "DOUBLE"),
    ("ret_vs_btc_60m", "DOUBLE"),
    # -- return vs universe --
    ("ret_pct_5m", "DOUBLE"),
    ("ret_pct_15m", "DOUBLE"),
    ("ret_pct_60m", "DOUBLE"),
    ("ret_spread_median_5m", "DOUBLE"),
    ("ret_spread_median_15m", "DOUBLE"),
    ("ret_spread_median_60m", "DOUBLE"),
    # -- trend alignment --
    ("dist_sma50_1h", "DOUBLE"),
    ("dist_30d_high", "DOUBLE"),
    # -- depth (optional) --
    ("depth_imbalance", "DOUBLE"),
    ("spread_bps", "DOUBLE"),
]

_COL_NAMES = [c for c, _ in COLUMNS]

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS signal_probe (\n    "
    + ",\n    ".join(f"{name} {typ}" for name, typ in COLUMNS)
    + ",\n    PRIMARY KEY (symbol, ts)\n);"
)


def connect_probe_db(
    path: str, *, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """Open the probe research DB, creating the table when writable.

    ``read_only=True`` opens for analysis (the DB must already exist); the
    table is not (re)created in that mode.
    """
    if not read_only:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = duckdb.connect(path, read_only=read_only)
    if not read_only:
        conn.execute(_SCHEMA)
    return conn


def upsert_rows(
    conn: duckdb.DuckDBPyConnection,
    rows: Sequence[Mapping[str, Any]],
) -> int:
    """Idempotently UPSERT probe ``rows`` keyed on ``(symbol, ts)``.

    Each row is a mapping keyed by :data:`COLUMNS` names; any absent key is
    written as ``NULL``. Returns the number of rows written.
    """
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _COL_NAMES)
    update_cols = [c for c in _COL_NAMES if c not in ("symbol", "ts")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    sql = (
        f"INSERT INTO signal_probe ({', '.join(_COL_NAMES)})\n"
        f"VALUES ({placeholders})\n"
        f"ON CONFLICT (symbol, ts) DO UPDATE SET {set_clause}"
    )
    payload = [tuple(r.get(c) for c in _COL_NAMES) for r in rows]
    conn.executemany(sql, payload)
    return len(payload)
