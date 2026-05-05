"""Compute FX ML features for all hourly bars.

Features computed using only data available on or before each bar.
"""
from __future__ import annotations

import logging
import math

import duckdb
import numpy as np
import pandas as pd

from fx.config import PIP_SIZE, FEATURE_COLS, LONDON_OPEN, NY_OPEN
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.features")


def _compute_price_features(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    query = f"""
    WITH ordered AS (
        SELECT datetime_utc, date, weekday, hour_utc,
               gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count,
               ROW_NUMBER() OVER (ORDER BY datetime_utc) AS rn
        FROM fx_prices_hourly
        WHERE data_quality = 'OK'
    ),
    with_lags AS (
        SELECT o.*,
               LAG(gbpeur_close, 1) OVER w AS close_1ago,
               LAG(gbpeur_close, 4) OVER w AS close_4ago,
               LAG(gbpeur_close, 8) OVER w AS close_8ago,
               LAG(gbpeur_close, 24) OVER w AS close_24ago,
               LAG(gbpeur_close, 120) OVER w AS close_5dago,
               LAG(gbpeur_close, 480) OVER w AS close_20dago,
               AVG(gbpeur_close) OVER (ORDER BY datetime_utc ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS ma_24h,
               AVG(gbpeur_close) OVER (ORDER BY datetime_utc ROWS BETWEEN 119 PRECEDING AND CURRENT ROW) AS ma_120h,
               AVG(gbpeur_close) OVER (ORDER BY datetime_utc ROWS BETWEEN 479 PRECEDING AND CURRENT ROW) AS ma_480h,
               STDDEV(gbpeur_close) OVER (ORDER BY datetime_utc ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS std_24h,
               MAX(gbpeur_high) OVER (ORDER BY datetime_utc ROWS BETWEEN 479 PRECEDING AND CURRENT ROW) AS high_480h,
               MIN(gbpeur_low) OVER (ORDER BY datetime_utc ROWS BETWEEN 479 PRECEDING AND CURRENT ROW) AS low_480h,
               AVG(tick_count) OVER (ORDER BY datetime_utc ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS avg_ticks_24h
        FROM ordered o
        WINDOW w AS (ORDER BY datetime_utc)
    )
    SELECT
        datetime_utc, date, weekday, hour_utc,
        gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count,
        (gbpeur_close - close_1ago) / {PIP_SIZE} / 100 AS return_1h,
        (gbpeur_close - close_4ago) / NULLIF(close_4ago, 0) AS return_4h,
        (gbpeur_close - close_8ago) / NULLIF(close_8ago, 0) AS return_8h,
        (gbpeur_close - close_24ago) / NULLIF(close_24ago, 0) AS return_24h,
        (gbpeur_close - close_5dago) / NULLIF(close_5dago, 0) AS return_5d,
        (gbpeur_close - close_20dago) / NULLIF(close_20dago, 0) AS return_20d,
        gbpeur_close / NULLIF(ma_24h, 0) - 1 AS price_vs_24h_ma,
        gbpeur_close / NULLIF(ma_120h, 0) - 1 AS price_vs_120h_ma,
        gbpeur_close / NULLIF(ma_480h, 0) - 1 AS price_vs_480h_ma,
        CASE WHEN std_24h > 0 THEN (gbpeur_close - ma_24h) / (2 * std_24h) ELSE 0 END AS bollinger_position_24h,
        (gbpeur_close - high_480h) / NULLIF(high_480h - low_480h, 0) AS drawdown_from_480h_high,
        (gbpeur_close - low_480h) / NULLIF(high_480h - low_480h, 0) AS rally_from_480h_low,
        ABS(gbpeur_close - gbpeur_open) / NULLIF(gbpeur_high - gbpeur_low, 0) AS candle_body_pct,
        (gbpeur_high - GREATEST(gbpeur_open, gbpeur_close)) / NULLIF(gbpeur_high - gbpeur_low, 0) AS upper_wick_pct,
        (LEAST(gbpeur_open, gbpeur_close) - gbpeur_low) / NULLIF(gbpeur_high - gbpeur_low, 0) AS lower_wick_pct,
        (gbpeur_high - gbpeur_low) / {PIP_SIZE} AS candle_range_pips,
        tick_count / NULLIF(avg_ticks_24h, 0) AS tick_count_vs_avg,
        close_1ago
    FROM with_lags
    WHERE rn > 480
    ORDER BY datetime_utc
    """
    return conn.execute(query).fetchdf()


def _compute_rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _compute_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("datetime_utc").copy()
    hourly_ret = df["gbpeur_close"].pct_change()

    df["realized_vol_24h"] = hourly_ret.rolling(24).std() * np.sqrt(24 * 252)
    df["realized_vol_120h"] = hourly_ret.rolling(120).std() * np.sqrt(24 * 252)
    df["vol_ratio"] = df["realized_vol_24h"] / df["realized_vol_120h"].replace(0, np.nan)

    tr = pd.concat([
        df["gbpeur_high"] - df["gbpeur_low"],
        (df["gbpeur_high"] - df["close_1ago"]).abs(),
        (df["gbpeur_low"] - df["close_1ago"]).abs()
    ], axis=1).max(axis=1)
    df["atr_pips_24h"] = tr.rolling(24).mean() / PIP_SIZE
    df["range_expansion"] = (df["gbpeur_high"] - df["gbpeur_low"]) / tr.rolling(24).mean().replace(0, np.nan)

    df["rsi_14h"] = _compute_rsi(df["gbpeur_close"], 14)
    df["rsi_48h"] = _compute_rsi(df["gbpeur_close"], 48)

    avg_range = (df["gbpeur_high"] - df["gbpeur_low"]).rolling(24).mean()
    df["body_vs_avg_range"] = (df["gbpeur_close"] - df["gbpeur_open"]).abs() / avg_range.replace(0, np.nan)

    return df


def _compute_calendar_features(df: pd.DataFrame) -> pd.DataFrame:
    df["hour_sin"] = np.sin(2 * np.pi * df["hour_utc"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour_utc"] / 24)
    df["day_of_week"] = pd.to_datetime(df["date"]).dt.dayofweek
    df["is_london_open"] = (df["hour_utc"] >= LONDON_OPEN[0]) & (df["hour_utc"] < LONDON_OPEN[1])
    df["is_ny_open"] = (df["hour_utc"] >= NY_OPEN[0]) & (df["hour_utc"] < NY_OPEN[1])
    df["is_london_ny_overlap"] = (df["hour_utc"] >= NY_OPEN[0]) & (df["hour_utc"] < LONDON_OPEN[1])
    df["is_asian_session"] = (df["hour_utc"] >= 23) | (df["hour_utc"] < 7)
    return df


def _compute_pattern_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("datetime_utc").copy()

    daily_highs = df.groupby("date")["gbpeur_high"].transform("max")
    daily_lows = df.groupby("date")["gbpeur_low"].transform("min")
    daily_range = (daily_highs - daily_lows).replace(0, np.nan)
    df["distance_from_daily_high"] = (df["gbpeur_close"] - daily_highs) / daily_range
    df["distance_from_daily_low"] = (df["gbpeur_close"] - daily_lows) / daily_range

    # Cumulative intraday range used: current bar's contribution to daily range
    candle_range = df["gbpeur_high"] - df["gbpeur_low"]
    df["daily_range_pct_used"] = candle_range / daily_range

    df["prior_session_range_pips"] = (
        df["gbpeur_high"].rolling(8).max() - df["gbpeur_low"].rolling(8).min()
    ) / PIP_SIZE

    direction = np.sign(df["gbpeur_close"].values - df["gbpeur_open"].values)
    consec_up = np.zeros(len(df), dtype=int)
    consec_down = np.zeros(len(df), dtype=int)
    for i in range(len(direction)):
        if direction[i] > 0:
            consec_up[i] = (consec_up[i - 1] + 1) if i > 0 else 1
        elif direction[i] < 0:
            consec_down[i] = (consec_down[i - 1] + 1) if i > 0 else 1
    df["consecutive_up_hours"] = consec_up
    df["consecutive_down_hours"] = consec_down

    return df


def _compute_macro_features(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    macro = conn.execute("SELECT indicator, observation_date, value FROM fx_macro").fetchdf()

    if macro.empty:
        logger.warning("No macro data — macro features will be NULL")
        for col in ["boe_rate", "ecb_rate", "rate_differential", "eurusd_return_24h", "gbpusd_return_24h"]:
            df[col] = None
        return df

    df["date_key"] = pd.to_datetime(df["date"]).dt.date

    for indicator in ["boe_rate", "ecb_rate", "eurusd", "gbpusd"]:
        ind_data = macro[macro["indicator"] == indicator][["observation_date", "value"]].copy()
        ind_data["observation_date"] = pd.to_datetime(ind_data["observation_date"]).dt.date
        ind_data = ind_data.sort_values("observation_date").drop_duplicates("observation_date")
        ind_data = ind_data.rename(columns={"value": indicator, "observation_date": "date_key"})
        df = df.merge(ind_data, on="date_key", how="left")
        df[indicator] = df[indicator].ffill()

    if "boe_rate" in df.columns and "ecb_rate" in df.columns:
        df["rate_differential"] = df["boe_rate"] - df["ecb_rate"]
    else:
        df["rate_differential"] = None

    if "eurusd" in df.columns:
        df["eurusd_return_24h"] = df["eurusd"].pct_change(24)
    else:
        df["eurusd_return_24h"] = None

    if "gbpusd" in df.columns:
        df["gbpusd_return_24h"] = df["gbpusd"].pct_change(24)
    else:
        df["gbpusd_return_24h"] = None

    for col in ["eurusd", "gbpusd", "date_key"]:
        if col in df.columns:
            df.drop(columns=[col], inplace=True)

    return df


def compute_features(conn: duckdb.DuckDBPyConnection) -> int:
    create_all_tables(conn)

    logger.info("Computing price features...")
    df = _compute_price_features(conn)
    logger.info("  Price features: %d rows", len(df))

    logger.info("Computing volatility and RSI...")
    df = _compute_volatility_features(df)

    logger.info("Computing calendar features...")
    df = _compute_calendar_features(df)

    logger.info("Computing pattern features...")
    df = _compute_pattern_features(df)

    logger.info("Computing macro features...")
    df = _compute_macro_features(conn, df)

    conn.execute("DELETE FROM fx_ml_features")

    # Boolean columns to float
    bool_cols = ["is_london_open", "is_ny_open", "is_london_ny_overlap", "is_asian_session"]
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].astype(float)

    insert_cols = ["datetime_utc"] + FEATURE_COLS
    missing = [c for c in FEATURE_COLS if c not in df.columns]
    if missing:
        logger.warning("Missing feature columns (will be NULL): %s", missing)
        for c in missing:
            df[c] = None

    # Clean NaN/inf
    for col in FEATURE_COLS:
        if col in df.columns:
            df[col] = df[col].replace([np.inf, -np.inf], np.nan)
            df[col] = df[col].where(pd.notnull(df[col]), None)

    conn.execute("INSERT INTO fx_ml_features SELECT datetime_utc, " +
                 ", ".join(FEATURE_COLS) + " FROM df")

    total = conn.execute("SELECT COUNT(*) FROM fx_ml_features").fetchone()[0]
    logger.info("Total features computed: %d rows", total)
    return total
