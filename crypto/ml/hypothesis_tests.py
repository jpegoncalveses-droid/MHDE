"""Hypothesis tests for crypto signal validation.

Tests A-E from the architecture doc. Each test splits coin-dates into quintiles
by a candidate signal, measures forward returns per quintile, and reports
statistical significance.

CHECKPOINT: If fewer than 2 of 5 tests show p < 0.01, STOP the crypto build.
"""
from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.hypothesis_tests")


def _load_price_returns(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load daily prices and compute forward returns for all universe coins."""
    df = conn.execute("""
        SELECT p.symbol, p.trade_date, p.close,
               LEAD(p.close, 1) OVER w / p.close - 1 AS fwd_1d,
               LEAD(p.close, 3) OVER w / p.close - 1 AS fwd_3d,
               LEAD(p.close, 5) OVER w / p.close - 1 AS fwd_5d,
               LEAD(p.close, 10) OVER w / p.close - 1 AS fwd_10d,
               LAG(p.close, 1) OVER w AS prev_close,
               LAG(p.close, 3) OVER w AS close_3ago,
               LAG(p.close, 5) OVER w AS close_5ago,
               LAG(p.close, 7) OVER w AS close_7ago,
               LAG(p.close, 10) OVER w AS close_10ago
        FROM crypto_prices_daily p
        WHERE p.symbol IN (SELECT symbol FROM crypto_universe WHERE is_active = true)
          AND p.close > 0
        WINDOW w AS (PARTITION BY p.symbol ORDER BY p.trade_date)
        ORDER BY p.symbol, p.trade_date
    """).fetchdf()
    df["return_1d"] = df["close"] / df["prev_close"] - 1
    df["return_3d"] = df["close"] / df["close_3ago"] - 1
    df["return_5d"] = df["close"] / df["close_5ago"] - 1
    df["return_7d"] = df["close"] / df["close_7ago"] - 1
    df["return_10d"] = df["close"] / df["close_10ago"] - 1
    return df.dropna(subset=["fwd_5d"])


def _quintile_test(df: pd.DataFrame, signal_col: str, fwd_col: str, test_name: str) -> dict:
    """Split by quintile on signal_col, measure forward returns, test monotonicity."""
    valid = df.dropna(subset=[signal_col, fwd_col])
    if len(valid) < 100:
        return {"test": test_name, "signal": signal_col, "horizon": fwd_col,
                "n": len(valid), "p_value": 1.0, "significant": False, "error": "too few samples"}

    valid = valid.copy()
    valid["quintile"] = pd.qcut(valid[signal_col], 5, labels=False, duplicates="drop")
    group_means = valid.groupby("quintile")[fwd_col].mean()

    q1_returns = valid[valid["quintile"] == 0][fwd_col]
    q5_returns = valid[valid["quintile"] == valid["quintile"].max()][fwd_col]
    if len(q1_returns) < 10 or len(q5_returns) < 10:
        return {"test": test_name, "signal": signal_col, "horizon": fwd_col,
                "n": len(valid), "p_value": 1.0, "significant": False, "error": "quintile too small"}

    t_stat, p_value = stats.ttest_ind(q1_returns, q5_returns)

    return {
        "test": test_name,
        "signal": signal_col,
        "horizon": fwd_col,
        "n": len(valid),
        "q1_mean": float(q1_returns.mean()),
        "q5_mean": float(q5_returns.mean()),
        "spread": float(q1_returns.mean() - q5_returns.mean()),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "significant": p_value < 0.01,
        "quintile_means": group_means.to_dict(),
    }


def test_a_funding_rate(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Test A: Funding rate mean reversion. Negative funding -> higher forward returns."""
    prices = _load_price_returns(conn)

    funding_7d = conn.execute("""
        WITH daily AS (
            SELECT symbol, DATE(funding_time) AS trade_date, AVG(funding_rate) AS daily_rate
            FROM crypto_funding_rates GROUP BY symbol, DATE(funding_time)
        )
        SELECT symbol, trade_date,
               AVG(daily_rate) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS funding_avg_7d
        FROM daily
    """).fetchdf()

    merged = prices.merge(funding_7d, on=["symbol", "trade_date"], how="inner")
    results = []
    for fwd in ["fwd_5d", "fwd_10d"]:
        results.append(_quintile_test(merged, "funding_avg_7d", fwd, "A: Funding Rate Mean Reversion"))
    return results


def test_b_oi_divergence(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Test B: OI-price divergence predicts larger moves."""
    prices = _load_price_returns(conn)

    oi = conn.execute("""
        SELECT symbol, trade_date, open_interest_value,
               (open_interest_value / NULLIF(LAG(open_interest_value, 3) OVER (PARTITION BY symbol ORDER BY trade_date), 0)) - 1 AS oi_change_3d
        FROM crypto_open_interest
    """).fetchdf()

    merged = prices.merge(oi[["symbol", "trade_date", "oi_change_3d"]], on=["symbol", "trade_date"], how="inner")
    merged["oi_price_div"] = merged["oi_change_3d"] - merged["return_3d"]

    results = []
    for fwd in ["fwd_5d", "fwd_10d"]:
        results.append(_quintile_test(merged, "oi_price_div", fwd, "B: OI-Price Divergence"))
    return results


def test_c_momentum(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Test C: Does momentum continue in crypto?"""
    prices = _load_price_returns(conn)
    results = []
    for fwd in ["fwd_5d", "fwd_10d"]:
        results.append(_quintile_test(prices, "return_10d", fwd, "C: Momentum Continuation"))
    return results


def test_d_volume_spike(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Test D: Volume spikes precede large moves (absolute magnitude)."""
    df = conn.execute("""
        SELECT p.symbol, p.trade_date, p.close, p.volume,
               AVG(p.volume) OVER (PARTITION BY p.symbol ORDER BY p.trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg_vol_20d,
               LEAD(p.close, 5) OVER (PARTITION BY p.symbol ORDER BY p.trade_date) / p.close - 1 AS fwd_5d,
               LEAD(p.close, 10) OVER (PARTITION BY p.symbol ORDER BY p.trade_date) / p.close - 1 AS fwd_10d
        FROM crypto_prices_daily p
        WHERE p.symbol IN (SELECT symbol FROM crypto_universe WHERE is_active = true)
          AND p.close > 0
    """).fetchdf()
    df["rel_volume"] = df["volume"] / df["avg_vol_20d"]
    df = df.dropna(subset=["rel_volume", "fwd_5d"])

    results = []
    for fwd in ["fwd_5d", "fwd_10d"]:
        spike = df[df["rel_volume"] > 2.0][fwd].dropna()
        control = df[df["rel_volume"] <= 2.0][fwd].dropna()
        if len(spike) < 20:
            results.append({"test": "D: Volume Spike", "horizon": fwd, "n": len(spike),
                           "p_value": 1.0, "significant": False, "error": "too few spikes"})
            continue
        t_stat, p_value = stats.ttest_ind(spike.abs(), control.abs())
        results.append({
            "test": "D: Volume Spike",
            "horizon": fwd,
            "n_spike": len(spike),
            "n_control": len(control),
            "spike_abs_mean": float(spike.abs().mean()),
            "control_abs_mean": float(control.abs().mean()),
            "t_stat": float(t_stat),
            "p_value": float(p_value),
            "significant": p_value < 0.01,
        })
    return results


def test_e_btc_regime(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Test E: Altcoins perform better when BTC is trending up."""
    prices = _load_price_returns(conn)

    btc = prices[prices["symbol"] == "BTCUSDT"][["trade_date", "return_7d"]].rename(
        columns={"return_7d": "btc_return_7d"})

    altcoins = prices[prices["symbol"] != "BTCUSDT"].merge(btc, on="trade_date", how="inner")

    results = []
    for fwd in ["fwd_5d", "fwd_10d"]:
        valid = altcoins.dropna(subset=["btc_return_7d", fwd])
        bullish = valid[valid["btc_return_7d"] > 0.03][fwd]
        bearish = valid[valid["btc_return_7d"] < -0.03][fwd]
        if len(bullish) < 20 or len(bearish) < 20:
            results.append({"test": "E: BTC Regime", "horizon": fwd,
                           "p_value": 1.0, "significant": False, "error": "too few samples"})
            continue
        t_stat, p_value = stats.ttest_ind(bullish, bearish)
        results.append({
            "test": "E: BTC Regime",
            "horizon": fwd,
            "n_bullish": len(bullish),
            "n_bearish": len(bearish),
            "bull_mean": float(bullish.mean()),
            "bear_mean": float(bearish.mean()),
            "t_stat": float(t_stat),
            "p_value": float(p_value),
            "significant": p_value < 0.01,
        })
    return results


def run_all_tests(conn: duckdb.DuckDBPyConnection) -> dict:
    """Run all hypothesis tests and return summary with go/no-go decision."""
    all_results = {}

    tests = [
        ("A: Funding Rate", test_a_funding_rate),
        ("B: OI Divergence", test_b_oi_divergence),
        ("C: Momentum", test_c_momentum),
        ("D: Volume Spike", test_d_volume_spike),
        ("E: BTC Regime", test_e_btc_regime),
    ]

    significant_tests = 0
    for name, fn in tests:
        try:
            results = fn(conn)
            all_results[name] = results
            if any(r.get("significant", False) for r in results):
                significant_tests += 1
                logger.info("  %s: SIGNIFICANT", name)
            else:
                logger.info("  %s: not significant", name)
        except Exception as e:
            logger.error("  %s: FAILED — %s", name, e)
            all_results[name] = [{"test": name, "error": str(e)}]

    go = significant_tests >= 2
    summary = {
        "tests_run": len(tests),
        "significant_count": significant_tests,
        "go_decision": go,
        "results": all_results,
    }

    if go:
        logger.info("CHECKPOINT PASSED: %d/5 tests significant. Proceeding.", significant_tests)
    else:
        logger.warning("CHECKPOINT FAILED: Only %d/5 tests significant. STOP BUILD.", significant_tests)

    return summary


def print_test_results(summary: dict):
    """Pretty-print hypothesis test results."""
    print(f"\n{'='*70}")
    print("CRYPTO HYPOTHESIS TEST RESULTS")
    print(f"{'='*70}")

    for test_name, results in summary["results"].items():
        print(f"\n--- {test_name} ---")
        for r in results:
            if "error" in r and r.get("p_value", 1) == 1.0:
                print(f"  {r.get('horizon', 'N/A')}: SKIPPED ({r['error']})")
                continue
            sig = "***" if r.get("significant") else ""
            print(f"  {r.get('horizon', 'N/A')}: p={r.get('p_value', 1):.4f} {sig}")
            if "q1_mean" in r:
                print(f"    Q1 mean: {r['q1_mean']*100:+.2f}%  Q5 mean: {r['q5_mean']*100:+.2f}%  "
                      f"Spread: {r['spread']*100:+.2f}%")
            if "spike_abs_mean" in r:
                print(f"    Spike |mean|: {r['spike_abs_mean']*100:.2f}%  Control |mean|: {r['control_abs_mean']*100:.2f}%")
            if "bull_mean" in r:
                print(f"    Bull mean: {r['bull_mean']*100:+.2f}%  Bear mean: {r['bear_mean']*100:+.2f}%")

    print(f"\n{'='*70}")
    go = summary["go_decision"]
    n_sig = summary["significant_count"]
    print(f"DECISION: {'GO' if go else 'NO-GO'} — {n_sig}/5 tests significant (threshold: 2)")
    print(f"{'='*70}")
