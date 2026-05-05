"""Compute crypto ML features for all universe symbol-dates.

Features computed using only data available on or before each date.
Uses DuckDB window functions for price features, Python for RSI
and derived features, separate queries for derivatives features.
"""
from __future__ import annotations

import logging
import math

import duckdb
import numpy as np
import pandas as pd

from crypto.schema import create_all_tables
from crypto.config import FEATURE_COLS

logger = logging.getLogger("mhde.crypto.features")


def _compute_price_features(conn: duckdb.DuckDBPyConnection, symbols: list[str]) -> pd.DataFrame:
    placeholders = ",".join(f"'{s}'" for s in symbols)

    query = f"""
    WITH raw AS (
        SELECT symbol, trade_date, open, high, low, close, volume, taker_buy_volume,
               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date) AS rn
        FROM crypto_prices_daily
        WHERE symbol IN ({placeholders})
          AND close > 0
    ),
    with_lags AS (
        SELECT
            r.*,
            LAG(close, 1) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close,
            LAG(close, 3) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_3ago,
            LAG(close, 5) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_5ago,
            LAG(close, 10) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_10ago,
            LAG(close, 20) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_20ago,
            LAG(close, 60) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_60ago,
            MAX(close) OVER (
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN 90 PRECEDING AND CURRENT ROW
            ) AS high_90d,
            AVG(close) OVER (
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS sma_20,
            AVG(close) OVER (
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN 49 PRECEDING AND CURRENT ROW
            ) AS sma_50,
            STDDEV(close) OVER (
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS std_20,
            AVG(volume) OVER (
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS avg_vol_20d
        FROM raw r
    )
    SELECT
        symbol, trade_date, close, high, low, volume, taker_buy_volume,
        close / NULLIF(prev_close, 0) - 1 AS return_1d,
        close / NULLIF(close_3ago, 0) - 1 AS return_3d,
        close / NULLIF(close_5ago, 0) - 1 AS return_5d,
        close / NULLIF(close_10ago, 0) - 1 AS return_10d,
        close / NULLIF(close_20ago, 0) - 1 AS return_20d,
        close / NULLIF(close_60ago, 0) - 1 AS return_60d,
        close / NULLIF(high_90d, 0) - 1 AS drawdown_from_90d_high,
        close / NULLIF(sma_20, 0) - 1 AS price_vs_20d_ma,
        close / NULLIF(sma_50, 0) - 1 AS price_vs_50d_ma,
        CASE WHEN std_20 > 0 THEN (close - sma_20) / (2 * std_20) ELSE 0 END AS bollinger_position,
        CASE WHEN high - low > 0 THEN (close - low) / (high - low) ELSE 0.5 END AS close_in_range,
        LEAST(volume / NULLIF(avg_vol_20d, 0), 10) AS relative_volume_20d,
        CASE WHEN volume > 0 THEN taker_buy_volume / volume ELSE 0.5 END AS taker_buy_ratio,
        prev_close
    FROM with_lags
    WHERE rn > 60
    ORDER BY symbol, trade_date
    """
    return conn.execute(query).fetchdf()


def _compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _compute_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    results = []
    for sym, group in df.groupby("symbol"):
        group = group.sort_values("trade_date").copy()
        returns = group["return_1d"]
        group["realized_vol_10d"] = returns.rolling(10).std() * np.sqrt(365)
        group["realized_vol_30d"] = returns.rolling(30).std() * np.sqrt(365)
        group["vol_ratio"] = group["realized_vol_10d"] / group["realized_vol_30d"].replace(0, np.nan)

        tr = pd.concat([
            group["high"] - group["low"],
            (group["high"] - group["prev_close"]).abs(),
            (group["low"] - group["prev_close"]).abs()
        ], axis=1).max(axis=1)
        group["atr_pct_14d"] = tr.rolling(14).mean() / group["close"]

        group["rsi_14d"] = _compute_rsi(group["close"])

        vol_series = group["volume"].values
        slopes = []
        for i in range(len(vol_series)):
            if i < 4:
                slopes.append(np.nan)
            else:
                window = vol_series[i - 4:i + 1]
                if np.all(np.isfinite(window)) and np.std(window) > 0:
                    x = np.arange(5, dtype=float)
                    slope = np.polyfit(x, window, 1)[0]
                    mean_val = np.mean(window)
                    slopes.append(slope / mean_val if mean_val > 0 else 0)
                else:
                    slopes.append(np.nan)
        group["volume_trend_5d"] = slopes

        results.append(group)
    return pd.concat(results, ignore_index=True)


def _compute_btc_relative(df: pd.DataFrame) -> pd.DataFrame:
    btc = df[df["symbol"] == "BTCUSDT"][["trade_date", "return_1d", "return_5d", "return_10d"]].rename(
        columns={"return_1d": "btc_return_1d", "return_5d": "btc_return_5d", "return_10d": "btc_return_10d"})

    df = df.merge(btc, on="trade_date", how="left")
    df["return_vs_btc_1d"] = df["return_1d"] - df["btc_return_1d"].fillna(0)
    df["return_vs_btc_5d"] = df["return_5d"] - df["btc_return_5d"].fillna(0)
    df["return_vs_btc_10d"] = df["return_10d"] - df["btc_return_10d"].fillna(0)

    btc_data = df[df["symbol"] == "BTCUSDT"][["trade_date", "return_1d"]].sort_values("trade_date").copy()
    btc_data["btc_return_7d"] = btc_data["return_1d"].rolling(7).sum()
    btc_data["btc_vol_30d"] = btc_data["return_1d"].rolling(30).std() * np.sqrt(365)
    df = df.merge(btc_data[["trade_date", "btc_return_7d", "btc_vol_30d"]],
                  on="trade_date", how="left", suffixes=("", "_regime"))

    results = []
    for sym, group in df.groupby("symbol"):
        group = group.sort_values("trade_date").copy()
        if sym == "BTCUSDT":
            group["beta_to_btc_30d"] = 1.0
        else:
            betas = []
            coin_rets = group["return_1d"].values
            btc_rets = group["btc_return_1d"].values
            for i in range(len(coin_rets)):
                if i < 29:
                    betas.append(np.nan)
                else:
                    cr = coin_rets[i - 29:i + 1]
                    br = btc_rets[i - 29:i + 1]
                    mask = np.isfinite(cr) & np.isfinite(br)
                    if mask.sum() > 10:
                        var_btc = np.var(br[mask])
                        if var_btc > 0:
                            betas.append(np.cov(cr[mask], br[mask])[0, 1] / var_btc)
                        else:
                            betas.append(np.nan)
                    else:
                        betas.append(np.nan)
            group["beta_to_btc_30d"] = betas
        results.append(group)
    return pd.concat(results, ignore_index=True)


def _compute_derivatives_features(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    funding = conn.execute("""
        WITH daily_funding AS (
            SELECT symbol, DATE(funding_time) AS trade_date,
                   LAST(funding_rate ORDER BY funding_time) AS funding_rate_current,
                   AVG(funding_rate) AS daily_avg_rate
            FROM crypto_funding_rates
            GROUP BY symbol, DATE(funding_time)
        )
        SELECT symbol, trade_date, funding_rate_current,
               AVG(daily_avg_rate) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS funding_rate_avg_3d,
               AVG(daily_avg_rate) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS funding_rate_avg_7d,
               AVG(daily_avg_rate) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS funding_mean_30d,
               STDDEV(daily_avg_rate) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS funding_std_30d
        FROM daily_funding
    """).fetchdf()
    funding["funding_rate_zscore"] = (
        (funding["funding_rate_current"] - funding["funding_mean_30d"])
        / funding["funding_std_30d"].replace(0, np.nan)
    )
    funding = funding.drop(columns=["funding_mean_30d", "funding_std_30d"])

    df = df.merge(funding, on=["symbol", "trade_date"], how="left")

    oi = conn.execute("""
        SELECT symbol, trade_date,
               (open_interest_value / NULLIF(LAG(open_interest_value, 1) OVER (PARTITION BY symbol ORDER BY trade_date), 0)) - 1 AS oi_change_1d,
               (open_interest_value / NULLIF(LAG(open_interest_value, 3) OVER (PARTITION BY symbol ORDER BY trade_date), 0)) - 1 AS oi_change_3d,
               (open_interest_value / NULLIF(LAG(open_interest_value, 7) OVER (PARTITION BY symbol ORDER BY trade_date), 0)) - 1 AS oi_change_7d
        FROM crypto_open_interest
    """).fetchdf()
    df = df.merge(oi[["symbol", "trade_date", "oi_change_1d", "oi_change_3d", "oi_change_7d"]],
                  on=["symbol", "trade_date"], how="left")
    df["oi_price_divergence_3d"] = df["oi_change_3d"] - df["return_3d"]

    return df


def _compute_market_structure(df: pd.DataFrame) -> pd.DataFrame:
    daily_total_vol = df.groupby("trade_date")["volume"].sum().reset_index().rename(columns={"volume": "total_vol"})
    btc_vol = df[df["symbol"] == "BTCUSDT"][["trade_date", "volume"]].rename(columns={"volume": "btc_vol"})
    dom = daily_total_vol.merge(btc_vol, on="trade_date", how="left")
    dom["btc_dominance"] = dom["btc_vol"] / dom["total_vol"]
    df = df.merge(dom[["trade_date", "btc_dominance"]], on="trade_date", how="left")

    df["market_cap_log"] = np.log10((df["volume"] * df["close"]).clip(lower=1))

    return df


def compute_features(conn: duckdb.DuckDBPyConnection, batch_size: int = 20) -> int:
    create_all_tables(conn)

    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY rank_by_volume"
    ).fetchall()]

    if not symbols:
        logger.warning("No symbols in universe.")
        return 0

    conn.execute("DELETE FROM crypto_ml_features")

    logger.info("Computing price features for %d symbols...", len(symbols))
    df = _compute_price_features(conn, symbols)
    logger.info("  Price features: %d rows", len(df))

    logger.info("Computing volatility and RSI...")
    df = _compute_volatility_features(df)

    logger.info("Computing BTC-relative features...")
    df = _compute_btc_relative(df)

    logger.info("Computing derivatives features...")
    df = _compute_derivatives_features(conn, df)

    logger.info("Computing market structure features...")
    df = _compute_market_structure(df)

    for col in FEATURE_COLS:
        if col not in df.columns:
            logger.warning("Missing feature column (will be NULL): %s", col)
            df[col] = None

    insert_cols = ["symbol", "trade_date"] + FEATURE_COLS
    placeholders = ", ".join(["?"] * len(insert_cols))
    insert_sql = f"INSERT INTO crypto_ml_features ({', '.join(insert_cols)}) VALUES ({placeholders})"

    rows_data = df[insert_cols].values.tolist()
    for row in rows_data:
        cleaned = []
        for v in row:
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                cleaned.append(None)
            else:
                cleaned.append(v)
        conn.execute(insert_sql, cleaned)

    total = len(rows_data)
    logger.info("Total features computed: %d rows", total)
    return total
