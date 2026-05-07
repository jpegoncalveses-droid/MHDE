"""Compute FX ML labels (forward pip returns and binary targets).

For each hourly bar, computes the maximum pip move up/down within
24h and 48h forward windows.
"""
from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

from fx.config import PIP_SIZE
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.labels")


def compute_labels(conn: duckdb.DuckDBPyConnection) -> int:
    create_all_tables(conn)

    df = conn.execute("""
        SELECT datetime_utc, gbpeur_close, gbpeur_high, gbpeur_low
        FROM fx_prices_hourly
        WHERE data_quality = 'OK'
        ORDER BY datetime_utc
    """).fetchdf()

    logger.info("Computing labels for %d bars...", len(df))

    closes = df["gbpeur_close"].values
    highs = df["gbpeur_high"].values
    lows = df["gbpeur_low"].values
    n = len(df)

    fwd_max_up_24h = np.full(n, np.nan)
    fwd_max_down_24h = np.full(n, np.nan)
    fwd_max_up_48h = np.full(n, np.nan)
    fwd_max_down_48h = np.full(n, np.nan)
    fwd_close_24h = np.full(n, np.nan)
    fwd_close_48h = np.full(n, np.nan)

    for i in range(n - 48):
        c = closes[i]
        # 24h window
        h24 = highs[i + 1:i + 25]
        l24 = lows[i + 1:i + 25]
        fwd_max_up_24h[i] = (np.max(h24) - c) / PIP_SIZE
        fwd_max_down_24h[i] = (c - np.min(l24)) / PIP_SIZE
        fwd_close_24h[i] = (closes[i + 24] - c) / PIP_SIZE

        # 48h window
        h48 = highs[i + 1:i + 49]
        l48 = lows[i + 1:i + 49]
        fwd_max_up_48h[i] = (np.max(h48) - c) / PIP_SIZE
        fwd_max_down_48h[i] = (c - np.min(l48)) / PIP_SIZE
        fwd_close_48h[i] = (closes[i + 48] - c) / PIP_SIZE

    # Also compute for i in range(n-48, n-24) — 24h only.
    # Clamp to non-negative to avoid IndexError when n < 48.
    for i in range(max(0, n - 48), max(0, n - 24)):
        c = closes[i]
        h24 = highs[i + 1:i + 25]
        l24 = lows[i + 1:i + 25]
        fwd_max_up_24h[i] = (np.max(h24) - c) / PIP_SIZE
        fwd_max_down_24h[i] = (c - np.min(l24)) / PIP_SIZE
        fwd_close_24h[i] = (closes[i + 24] - c) / PIP_SIZE

    labels_df = pd.DataFrame({
        "datetime_utc": df["datetime_utc"],
        "close_price": closes,
        "fwd_max_up_pips_24h": fwd_max_up_24h,
        "fwd_max_down_pips_24h": fwd_max_down_24h,
        "fwd_max_up_pips_48h": fwd_max_up_48h,
        "fwd_max_down_pips_48h": fwd_max_down_48h,
        "fwd_close_pips_24h": fwd_close_24h,
        "fwd_close_pips_48h": fwd_close_48h,
    })

    # Binary labels
    labels_df["label_up_20pip_24h"] = labels_df["fwd_max_up_pips_24h"] >= 20
    labels_df["label_down_20pip_24h"] = labels_df["fwd_max_down_pips_24h"] >= 20
    labels_df["label_up_20pip_48h"] = labels_df["fwd_max_up_pips_48h"] >= 20
    labels_df["label_down_20pip_48h"] = labels_df["fwd_max_down_pips_48h"] >= 20
    labels_df["label_up_30pip_24h"] = labels_df["fwd_max_up_pips_24h"] >= 30
    labels_df["label_down_30pip_24h"] = labels_df["fwd_max_down_pips_24h"] >= 30
    labels_df["label_up_30pip_48h"] = labels_df["fwd_max_up_pips_48h"] >= 30
    labels_df["label_down_30pip_48h"] = labels_df["fwd_max_down_pips_48h"] >= 30

    # Only keep rows with at least 24h forward data
    labels_df = labels_df.dropna(subset=["fwd_max_up_pips_24h"])

    conn.execute("DELETE FROM fx_ml_labels")
    conn.execute("INSERT INTO fx_ml_labels SELECT * FROM labels_df")

    count = conn.execute("SELECT COUNT(*) FROM fx_ml_labels").fetchone()[0]
    logger.info("Labels computed: %d rows", count)
    return count
