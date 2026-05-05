"""Compute ML features for all universe ticker-dates.

Features are computed using only data available on or before each date (no lookahead).
Batched by ticker to manage memory. Uses DuckDB window functions heavily.
"""
from __future__ import annotations

import logging
import math

import duckdb
import numpy as np
import pandas as pd

from ml.schema import create_all_tables

logger = logging.getLogger("mhde.ml.features")

_UNIVERSE_FILTER = """
    SELECT ticker, sector FROM companies
    WHERE market_cap >= 10000000000
      AND sector IS NOT NULL
      AND is_etf = false
      AND is_active = true
"""

SECTOR_ETF_MAP = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
}


def _compute_price_features(conn: duckdb.DuckDBPyConnection, tickers: list[str]) -> pd.DataFrame:
    """Compute all price-based features for a batch of tickers."""
    placeholders = ",".join(f"'{t}'" for t in tickers)

    query = f"""
    WITH raw AS (
        SELECT ticker, trade_date, open, high, low, close, volume, adjusted_close,
               ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
        FROM prices_daily
        WHERE ticker IN ({placeholders})
          AND adjusted_close > 0
    ),
    with_lags AS (
        SELECT
            r.*,
            LAG(adjusted_close, 1) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close,
            LAG(adjusted_close, 5) OVER (PARTITION BY ticker ORDER BY trade_date) AS close_5ago,
            LAG(adjusted_close, 10) OVER (PARTITION BY ticker ORDER BY trade_date) AS close_10ago,
            LAG(adjusted_close, 20) OVER (PARTITION BY ticker ORDER BY trade_date) AS close_20ago,
            LAG(adjusted_close, 60) OVER (PARTITION BY ticker ORDER BY trade_date) AS close_60ago,
            -- For 52-week high
            MAX(adjusted_close) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 252 PRECEDING AND CURRENT ROW
            ) AS high_252d,
            -- SMAs
            AVG(adjusted_close) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 49 PRECEDING AND CURRENT ROW
            ) AS sma_50,
            AVG(adjusted_close) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 199 PRECEDING AND CURRENT ROW
            ) AS sma_200,
            AVG(adjusted_close) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS sma_20,
            STDDEV(adjusted_close) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS std_20,
            -- Volume
            AVG(volume) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS avg_vol_20,
            -- Daily log returns for vol calc
            LN(adjusted_close / NULLIF(LAG(adjusted_close, 1) OVER (PARTITION BY ticker ORDER BY trade_date), 0)) AS log_return
        FROM raw r
    ),
    with_vol AS (
        SELECT
            wl.*,
            STDDEV(log_return) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) * SQRT(252) AS realized_vol_20d,
            STDDEV(log_return) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
            ) * SQRT(252) AS realized_vol_60d,
            -- ATR components
            GREATEST(high - low,
                     ABS(high - COALESCE(LAG(adjusted_close, 1) OVER (PARTITION BY ticker ORDER BY trade_date), high)),
                     ABS(low - COALESCE(LAG(adjusted_close, 1) OVER (PARTITION BY ticker ORDER BY trade_date), low))
            ) AS true_range
        FROM with_lags wl
    ),
    with_atr AS (
        SELECT
            wv.*,
            AVG(true_range) OVER (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS atr_20
        FROM with_vol wv
    )
    SELECT
        ticker,
        trade_date,
        adjusted_close,
        rn,
        -- Momentum
        CASE WHEN close_5ago > 0 THEN (adjusted_close / close_5ago) - 1 END AS return_5d,
        CASE WHEN close_10ago > 0 THEN (adjusted_close / close_10ago) - 1 END AS return_10d,
        CASE WHEN close_20ago > 0 THEN (adjusted_close / close_20ago) - 1 END AS return_20d,
        CASE WHEN close_60ago > 0 THEN (adjusted_close / close_60ago) - 1 END AS return_60d,
        -- Technical
        CASE WHEN high_252d > 0 THEN (adjusted_close / high_252d) - 1 END AS drawdown_from_52w_high,
        CASE WHEN sma_50 > 0 AND rn >= 50 THEN (adjusted_close / sma_50) - 1 END AS price_vs_50d_ma,
        CASE WHEN sma_200 > 0 AND rn >= 200 THEN (adjusted_close / sma_200) - 1 END AS price_vs_200d_ma,
        CASE WHEN std_20 > 0 AND rn >= 20 THEN (adjusted_close - sma_20) / (2 * std_20) END AS bollinger_position,
        CASE WHEN (high - low) > 0 THEN (close - low) / (high - low) END AS close_in_range,
        CASE WHEN prev_close > 0 THEN (open / prev_close) - 1 END AS gap_from_prev_close,
        -- Volatility
        realized_vol_20d,
        realized_vol_60d,
        CASE WHEN realized_vol_60d > 0 THEN realized_vol_20d / realized_vol_60d END AS vol_ratio,
        CASE WHEN adjusted_close > 0 THEN atr_20 / adjusted_close END AS atr_pct_20d,
        -- Volume
        CASE WHEN avg_vol_20 > 0 THEN LEAST(volume / avg_vol_20, 10.0) END AS relative_volume_20d,
        -- Log return for RSI calc
        log_return,
        prev_close,
        volume
    FROM with_atr
    WHERE rn >= 5
    ORDER BY ticker, trade_date
    """
    return conn.execute(query).fetchdf()


def _compute_rsi(df: pd.DataFrame) -> pd.Series:
    """Compute 14-day RSI using Wilder smoothing."""
    log_returns = df["log_return"].values
    gains = np.where(log_returns > 0, log_returns, 0.0)
    losses = np.where(log_returns < 0, -log_returns, 0.0)

    rsi_values = np.full(len(log_returns), np.nan)
    period = 14

    if len(log_returns) < period + 1:
        return pd.Series(rsi_values, index=df.index)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(log_returns)):
        if i == period:
            avg_gain = np.mean(gains[:period])
            avg_loss = np.mean(losses[:period])
        else:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            rsi_values[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_values[i] = 100.0 - (100.0 / (1.0 + rs))

    return pd.Series(rsi_values, index=df.index)


def _compute_volume_trend(df: pd.DataFrame) -> pd.Series:
    """Linear regression slope of volume over 5 days, normalized by mean volume."""
    volumes = df["volume"].values.astype(float)
    result = np.full(len(volumes), np.nan)
    x = np.arange(5, dtype=float)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()

    for i in range(4, len(volumes)):
        window = volumes[i-4:i+1]
        if np.any(np.isnan(window)) or np.any(window == 0):
            continue
        mean_vol = window.mean()
        if mean_vol == 0:
            continue
        y = window
        slope = ((x - x_mean) * (y - y.mean())).sum() / x_var
        result[i] = slope / mean_vol

    return pd.Series(result, index=df.index)


def _load_reference_prices(conn: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    """Load SPY + sector ETF prices as DataFrames indexed by trade_date."""
    ref_tickers = ["SPY"] + list(SECTOR_ETF_MAP.values())
    placeholders = ",".join(f"'{t}'" for t in ref_tickers)
    df = conn.execute(f"""
        SELECT ticker, trade_date, adjusted_close
        FROM prices_daily
        WHERE ticker IN ({placeholders})
        ORDER BY ticker, trade_date
    """).fetchdf()

    result = {}
    for ticker, group in df.groupby("ticker"):
        g = group.set_index("trade_date").sort_index()
        g["return_5d"] = g["adjusted_close"] / g["adjusted_close"].shift(5) - 1
        g["return_20d"] = g["adjusted_close"] / g["adjusted_close"].shift(20) - 1
        result[ticker] = g
    return result


def _load_vix(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load VIX prices."""
    df = conn.execute("""
        SELECT trade_date, adjusted_close AS vix_close
        FROM prices_daily WHERE ticker = 'VIX'
        ORDER BY trade_date
    """).fetchdf()
    df = df.set_index("trade_date").sort_index()
    df["vix_change_5d"] = df["vix_close"] / df["vix_close"].shift(5) - 1
    return df


def _load_yield_curve(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load 10Y-2Y spread from macro_series."""
    df = conn.execute("""
        SELECT
            d10.as_of_date AS trade_date,
            d10.value - d2.value AS spread
        FROM macro_series d10
        JOIN macro_series d2 ON d10.as_of_date = d2.as_of_date AND d2.series_id = 'DGS2'
        WHERE d10.series_id = 'DGS10'
        ORDER BY d10.as_of_date
    """).fetchdf()
    df = df.set_index("trade_date").sort_index()
    return df


def _load_filing_counts(conn: duckdb.DuckDBPyConnection, tickers: list[str]) -> pd.DataFrame:
    """Compute filing event counts for each ticker-date."""
    placeholders = ",".join(f"'{t}'" for t in tickers)

    query = f"""
    WITH universe_dates AS (
        SELECT DISTINCT ticker, trade_date
        FROM prices_daily
        WHERE ticker IN ({placeholders})
    ),
    deduped_filings AS (
        SELECT DISTINCT ticker, form_type, filing_date
        FROM filings
        WHERE ticker IN ({placeholders})
    )
    SELECT
        ud.ticker,
        ud.trade_date,
        (SELECT COUNT(*) FROM deduped_filings f
         WHERE f.ticker = ud.ticker AND f.form_type = '8-K'
           AND f.filing_date BETWEEN ud.trade_date - INTERVAL '7 days' AND ud.trade_date
        ) AS filing_8k_count_7d,
        (SELECT COUNT(*) FROM deduped_filings f
         WHERE f.ticker = ud.ticker AND f.form_type = '8-K'
           AND f.filing_date BETWEEN ud.trade_date - INTERVAL '30 days' AND ud.trade_date
        ) AS filing_8k_count_30d,
        (SELECT COUNT(*) FROM deduped_filings f
         WHERE f.ticker = ud.ticker AND f.form_type = '4'
           AND f.filing_date BETWEEN ud.trade_date - INTERVAL '7 days' AND ud.trade_date
        ) AS filing_form4_count_7d,
        (SELECT COUNT(*) FROM deduped_filings f
         WHERE f.ticker = ud.ticker AND f.form_type = '4'
           AND f.filing_date BETWEEN ud.trade_date - INTERVAL '14 days' AND ud.trade_date
        ) AS filing_form4_count_14d,
        (SELECT LEAST(
            CASE WHEN MAX(f.filing_date) IS NOT NULL
                 THEN ud.trade_date - MAX(f.filing_date)
                 ELSE 180 END, 180)
         FROM deduped_filings f
         WHERE f.ticker = ud.ticker AND f.form_type IN ('10-Q', '10-K')
           AND f.filing_date <= ud.trade_date
        ) AS days_since_last_10q
    FROM universe_dates ud
    """
    return conn.execute(query).fetchdf()


def _load_fundamentals(conn: duckdb.DuckDBPyConnection, tickers: list[str]) -> pd.DataFrame:
    """Load latest fundamental data (market_cap_log, pb_ratio) per ticker."""
    placeholders = ",".join(f"'{t}'" for t in tickers)

    query = f"""
    WITH latest_shares AS (
        SELECT ticker, value AS shares, as_of_date,
               ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY as_of_date DESC) AS rn
        FROM fundamentals_raw
        WHERE ticker IN ({placeholders})
          AND concept = 'us-gaap/CommonStockSharesOutstanding'
          AND value > 0
    ),
    latest_equity AS (
        SELECT ticker, value AS equity, as_of_date,
               ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY as_of_date DESC) AS rn
        FROM fundamentals_raw
        WHERE ticker IN ({placeholders})
          AND concept = 'us-gaap/StockholdersEquity'
          AND value > 0
    )
    SELECT
        s.ticker,
        s.shares,
        e.equity
    FROM latest_shares s
    LEFT JOIN latest_equity e ON s.ticker = e.ticker AND e.rn = 1
    WHERE s.rn = 1
    """
    return conn.execute(query).fetchdf()


def compute_features(conn: duckdb.DuckDBPyConnection, batch_size: int = 30) -> int:
    """Compute and insert ML features for all universe ticker-dates.

    Returns total rows inserted.
    """
    create_all_tables(conn)

    universe = conn.execute(_UNIVERSE_FILTER).fetchdf()
    tickers = universe["ticker"].tolist()
    sector_map = dict(zip(universe["ticker"], universe["sector"]))
    logger.info("Computing features for %d tickers", len(tickers))

    # Load reference data (shared across all tickers)
    logger.info("  Loading reference data (SPY, sector ETFs, VIX, yield curve)...")
    ref_prices = _load_reference_prices(conn)
    vix_df = _load_vix(conn)
    yield_df = _load_yield_curve(conn)

    # Load fundamentals (one query for all tickers)
    logger.info("  Loading fundamentals...")
    fund_df = _load_fundamentals(conn, tickers)
    fund_map = {}
    for _, row in fund_df.iterrows():
        fund_map[row["ticker"]] = {"shares": row["shares"], "equity": row["equity"]}

    conn.execute("DELETE FROM ml_features")

    total_inserted = 0

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]

        # Price-based features
        price_df = _compute_price_features(conn, batch)

        # Filing counts
        filing_df = _load_filing_counts(conn, batch)

        # Process each ticker
        batch_rows = []

        for ticker in batch:
            tk_df = price_df[price_df["ticker"] == ticker].copy()
            if tk_df.empty:
                continue

            # RSI
            tk_df["rsi_14d"] = _compute_rsi(tk_df)

            # Volume trend
            tk_df["volume_trend_5d"] = _compute_volume_trend(tk_df)

            # Market-relative features
            sector = sector_map.get(ticker)
            sector_etf = SECTOR_ETF_MAP.get(sector, "SPY")
            spy_data = ref_prices.get("SPY")
            sector_data = ref_prices.get(sector_etf, spy_data)

            for idx, row in tk_df.iterrows():
                td = row["trade_date"]

                # SPY relative
                ret_vs_spy_5d = None
                ret_vs_spy_20d = None
                if spy_data is not None and td in spy_data.index:
                    spy_row = spy_data.loc[td]
                    if row["return_5d"] is not None and not pd.isna(row["return_5d"]) and not pd.isna(spy_row["return_5d"]):
                        ret_vs_spy_5d = row["return_5d"] - spy_row["return_5d"]
                    if row["return_20d"] is not None and not pd.isna(row["return_20d"]) and not pd.isna(spy_row["return_20d"]):
                        ret_vs_spy_20d = row["return_20d"] - spy_row["return_20d"]

                # Sector relative
                ret_vs_sector_5d = None
                ret_vs_sector_20d = None
                if sector_data is not None and td in sector_data.index:
                    sec_row = sector_data.loc[td]
                    if row["return_5d"] is not None and not pd.isna(row["return_5d"]) and not pd.isna(sec_row["return_5d"]):
                        ret_vs_sector_5d = row["return_5d"] - sec_row["return_5d"]
                    if row["return_20d"] is not None and not pd.isna(row["return_20d"]) and not pd.isna(sec_row["return_20d"]):
                        ret_vs_sector_20d = row["return_20d"] - sec_row["return_20d"]

                # VIX
                vix_level = None
                vix_change = None
                if td in vix_df.index:
                    vix_level = vix_df.loc[td, "vix_close"]
                    vix_change = vix_df.loc[td, "vix_change_5d"]
                elif len(vix_df) > 0:
                    prior = vix_df.index[vix_df.index <= td]
                    if len(prior) > 0:
                        vix_level = vix_df.loc[prior[-1], "vix_close"]

                # Yield curve
                yc = None
                if td in yield_df.index:
                    yc = yield_df.loc[td, "spread"]
                elif len(yield_df) > 0:
                    prior = yield_df.index[yield_df.index <= td]
                    if len(prior) > 0:
                        yc = yield_df.loc[prior[-1], "spread"]

                # Fundamentals
                market_cap_log = None
                pb_ratio = None
                fund = fund_map.get(ticker)
                if fund and fund["shares"] and row["adjusted_close"]:
                    mc = row["adjusted_close"] * fund["shares"]
                    if mc > 0:
                        market_cap_log = math.log10(mc)
                    if fund["equity"] and fund["equity"] > 0:
                        bvps = fund["equity"] / fund["shares"]
                        if bvps > 0:
                            pb_ratio = row["adjusted_close"] / bvps

                batch_rows.append((
                    ticker, td,
                    _safe(row["return_5d"]),
                    _safe(row["return_10d"]),
                    _safe(row["return_20d"]),
                    _safe(row["return_60d"]),
                    _safe(row["rsi_14d"]),
                    _safe(row["drawdown_from_52w_high"]),
                    _safe(row["price_vs_50d_ma"]),
                    _safe(row["price_vs_200d_ma"]),
                    _safe(row["bollinger_position"]),
                    _safe(row["close_in_range"]),
                    _safe(row["gap_from_prev_close"]),
                    _safe(row["realized_vol_20d"]),
                    _safe(row["realized_vol_60d"]),
                    _safe(row["vol_ratio"]),
                    _safe(row["atr_pct_20d"]),
                    _safe(row["relative_volume_20d"]),
                    _safe(row["volume_trend_5d"]),
                    _safe(ret_vs_spy_5d),
                    _safe(ret_vs_spy_20d),
                    _safe(ret_vs_sector_5d),
                    _safe(ret_vs_sector_20d),
                    None,  # beta_60d placeholder - computed in second pass
                    _safe(vix_level),
                    _safe(vix_change),
                    _safe(yc),
                    None, None, None, None, None,  # filing counts - joined after
                    _safe(market_cap_log),
                    _safe(pb_ratio),
                ))

        # Bulk insert price-based features
        if batch_rows:
            conn.executemany("""
                INSERT INTO ml_features VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, batch_rows)
            total_inserted += len(batch_rows)

        if (i // batch_size + 1) % 3 == 0 or i + batch_size >= len(tickers):
            logger.info("  Price features: %d/%d tickers (%d rows)",
                        min(i + batch_size, len(tickers)), len(tickers), total_inserted)

    # Second pass: filing counts and beta
    logger.info("  Computing filing counts and beta (SQL updates)...")
    _update_filing_counts(conn, tickers)
    _update_beta(conn, tickers)

    final_count = conn.execute("SELECT COUNT(*) FROM ml_features").fetchone()[0]
    logger.info("Features complete: %d total rows", final_count)
    return final_count


def _safe(val):
    """Convert numpy/pandas NaN to None for DuckDB."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    return float(val)


def _update_filing_counts(conn: duckdb.DuckDBPyConnection, tickers: list[str]):
    """Update filing count columns via SQL join."""
    conn.execute("""
        UPDATE ml_features f SET
            filing_8k_count_7d = sub.cnt_8k_7d,
            filing_8k_count_30d = sub.cnt_8k_30d,
            filing_form4_count_7d = sub.cnt_f4_7d,
            filing_form4_count_14d = sub.cnt_f4_14d,
            days_since_last_10q = sub.days_10q
        FROM (
            SELECT
                ud.ticker,
                ud.trade_date,
                (SELECT COUNT(DISTINCT accession_number) FROM filings fi
                 WHERE fi.ticker = ud.ticker AND fi.form_type = '8-K'
                   AND fi.filing_date BETWEEN ud.trade_date - INTERVAL '7 days' AND ud.trade_date
                ) AS cnt_8k_7d,
                (SELECT COUNT(DISTINCT accession_number) FROM filings fi
                 WHERE fi.ticker = ud.ticker AND fi.form_type = '8-K'
                   AND fi.filing_date BETWEEN ud.trade_date - INTERVAL '30 days' AND ud.trade_date
                ) AS cnt_8k_30d,
                (SELECT COUNT(DISTINCT accession_number) FROM filings fi
                 WHERE fi.ticker = ud.ticker AND fi.form_type = '4'
                   AND fi.filing_date BETWEEN ud.trade_date - INTERVAL '7 days' AND ud.trade_date
                ) AS cnt_f4_7d,
                (SELECT COUNT(DISTINCT accession_number) FROM filings fi
                 WHERE fi.ticker = ud.ticker AND fi.form_type = '4'
                   AND fi.filing_date BETWEEN ud.trade_date - INTERVAL '14 days' AND ud.trade_date
                ) AS cnt_f4_14d,
                LEAST(COALESCE(
                    ud.trade_date - (SELECT MAX(fi.filing_date) FROM filings fi
                                     WHERE fi.ticker = ud.ticker
                                       AND fi.form_type IN ('10-Q', '10-K')
                                       AND fi.filing_date <= ud.trade_date),
                    180), 180) AS days_10q
            FROM ml_features ud
        ) sub
        WHERE f.ticker = sub.ticker AND f.trade_date = sub.trade_date
    """)


def _update_beta(conn: duckdb.DuckDBPyConnection, tickers: list[str]):
    """Compute 60-day rolling beta to SPY and update in place."""
    conn.execute("DROP TABLE IF EXISTS _tmp_beta")
    conn.execute("""
        CREATE TEMPORARY TABLE _tmp_beta AS
        WITH spy_ret AS (
            SELECT trade_date,
                   LN(adjusted_close / LAG(adjusted_close) OVER (ORDER BY trade_date)) AS spy_return
            FROM prices_daily
            WHERE ticker = 'SPY' AND adjusted_close > 0
        ),
        tk_ret AS (
            SELECT ticker, trade_date,
                   LN(adjusted_close / LAG(adjusted_close) OVER (PARTITION BY ticker ORDER BY trade_date)) AS tk_return
            FROM prices_daily
            WHERE ticker IN (SELECT DISTINCT ticker FROM ml_features)
              AND adjusted_close > 0
        ),
        paired AS (
            SELECT t.ticker, t.trade_date, t.tk_return, s.spy_return
            FROM tk_ret t
            JOIN spy_ret s ON t.trade_date = s.trade_date
            WHERE t.tk_return IS NOT NULL AND s.spy_return IS NOT NULL
        ),
        rolling_beta AS (
            SELECT
                ticker,
                trade_date,
                CASE WHEN COUNT(*) OVER w >= 55
                     AND VAR_POP(spy_return) OVER w > 0
                     THEN COVAR_POP(tk_return, spy_return) OVER w / VAR_POP(spy_return) OVER w
                END AS beta
            FROM paired
            WINDOW w AS (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
        )
        SELECT ticker, trade_date, beta
        FROM rolling_beta
        WHERE beta IS NOT NULL
    """)
    conn.execute("""
        UPDATE ml_features f SET
            beta_60d = sub.beta
        FROM _tmp_beta sub
        WHERE f.ticker = sub.ticker AND f.trade_date = sub.trade_date
    """)
    conn.execute("DROP TABLE IF EXISTS _tmp_beta")
