"""Hypothesis tests for FX signal validation.

CHECKPOINT: If fewer than 2 of 6 tests show p < 0.01, STOP the FX build.
"""
from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

from fx.config import PIP_SIZE
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.hypothesis_tests")


def _load_forward_data(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load hourly bars with forward 24h max up/down pips computed in Python."""
    df = conn.execute("""
        SELECT datetime_utc, date, weekday, hour_utc,
               gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count
        FROM fx_prices_hourly
        WHERE data_quality = 'OK'
        ORDER BY datetime_utc
    """).fetchdf()

    closes = df["gbpeur_close"].values
    highs = df["gbpeur_high"].values
    lows = df["gbpeur_low"].values
    n = len(df)

    fwd_max_up = np.full(n, np.nan)
    fwd_max_down = np.full(n, np.nan)
    fwd_close_24h = np.full(n, np.nan)

    for i in range(n - 24):
        window_highs = highs[i + 1:i + 25]
        window_lows = lows[i + 1:i + 25]
        fwd_max_up[i] = (np.max(window_highs) - closes[i]) / PIP_SIZE
        fwd_max_down[i] = (closes[i] - np.min(window_lows)) / PIP_SIZE
        fwd_close_24h[i] = (closes[i + 24] - closes[i]) / PIP_SIZE

    df["fwd_max_up_pips_24h"] = fwd_max_up
    df["fwd_max_down_pips_24h"] = fwd_max_down
    df["fwd_close_pips_24h"] = fwd_close_24h

    return df.dropna(subset=["fwd_max_up_pips_24h"])


def test_a_london_session(conn: duckdb.DuckDBPyConnection) -> dict:
    """Test A: London session has larger max moves than Asian session."""
    df = _load_forward_data(conn)

    london = df[(df["hour_utc"] >= 7) & (df["hour_utc"] < 16)]
    asian = df[(df["hour_utc"] >= 23) | (df["hour_utc"] < 7)]

    london_moves = london["fwd_max_up_pips_24h"].dropna()
    asian_moves = asian["fwd_max_up_pips_24h"].dropna()

    if len(london_moves) < 100 or len(asian_moves) < 100:
        return {"test": "A: London Session Volatility", "p_value": 1.0,
                "significant": False, "error": f"too few samples (L={len(london_moves)}, A={len(asian_moves)})"}

    t_stat, p_value = stats.ttest_ind(london_moves, asian_moves)
    return {
        "test": "A: London Session Volatility",
        "london_mean_pips": float(london_moves.mean()),
        "asian_mean_pips": float(asian_moves.mean()),
        "n_london": len(london_moves),
        "n_asian": len(asian_moves),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "significant": p_value < 0.01,
    }


def test_b_rate_differential(conn: duckdb.DuckDBPyConnection) -> dict:
    """Test B: Rate differential extremes predict directional drift."""
    macro = conn.execute("""
        SELECT indicator, observation_date, value FROM fx_macro
        WHERE indicator IN ('boe_rate', 'ecb_rate')
    """).fetchdf()

    if macro.empty:
        return {"test": "B: Rate Differential", "p_value": 1.0,
                "significant": False, "error": "no macro data"}

    boe = macro[macro["indicator"] == "boe_rate"][["observation_date", "value"]].rename(
        columns={"value": "boe"})
    ecb = macro[macro["indicator"] == "ecb_rate"][["observation_date", "value"]].rename(
        columns={"value": "ecb"})

    boe["observation_date"] = pd.to_datetime(boe["observation_date"])
    ecb["observation_date"] = pd.to_datetime(ecb["observation_date"])
    boe = boe.set_index("observation_date").resample("D").ffill().reset_index()
    ecb = ecb.set_index("observation_date").resample("D").ffill().reset_index()

    rates = boe.merge(ecb, on="observation_date", how="inner")
    rates["diff"] = rates["boe"] - rates["ecb"]
    rates["observation_date"] = rates["observation_date"].dt.date

    df = _load_forward_data(conn)
    noon_bars = df[df["hour_utc"] == 12].copy()
    noon_bars["date_key"] = pd.to_datetime(noon_bars["date"]).dt.date

    merged = noon_bars.merge(rates, left_on="date_key", right_on="observation_date", how="inner")

    if len(merged) < 100:
        return {"test": "B: Rate Differential", "p_value": 1.0,
                "significant": False, "error": f"too few samples ({len(merged)})"}

    high_diff = merged[merged["diff"] > merged["diff"].quantile(0.8)]["fwd_close_pips_24h"].dropna()
    low_diff = merged[merged["diff"] < merged["diff"].quantile(0.2)]["fwd_close_pips_24h"].dropna()

    t_stat, p_value = stats.ttest_ind(high_diff, low_diff)
    return {
        "test": "B: Rate Differential",
        "high_diff_mean_pips": float(high_diff.mean()),
        "low_diff_mean_pips": float(low_diff.mean()),
        "n_high": len(high_diff),
        "n_low": len(low_diff),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "significant": p_value < 0.01,
    }


def test_c_momentum(conn: duckdb.DuckDBPyConnection) -> dict:
    """Test C: Strong 24h returns predict continuation."""
    df = _load_forward_data(conn)
    df = df.sort_values("datetime_utc")
    df["return_24h_pips"] = df["gbpeur_close"].diff(24) / PIP_SIZE

    valid = df.dropna(subset=["return_24h_pips", "fwd_close_pips_24h"])
    strong_up = valid[valid["return_24h_pips"] > 30]["fwd_close_pips_24h"]
    neutral = valid[valid["return_24h_pips"].abs() < 10]["fwd_close_pips_24h"]

    if len(strong_up) < 50 or len(neutral) < 100:
        return {"test": "C: Momentum Persistence", "p_value": 1.0,
                "significant": False, "error": f"too few samples (up={len(strong_up)}, neutral={len(neutral)})"}

    t_stat, p_value = stats.ttest_ind(strong_up, neutral)
    return {
        "test": "C: Momentum Persistence",
        "strong_up_mean_pips": float(strong_up.mean()),
        "neutral_mean_pips": float(neutral.mean()),
        "n_up": len(strong_up),
        "n_neutral": len(neutral),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "significant": p_value < 0.01,
    }


def test_d_volatility_clustering(conn: duckdb.DuckDBPyConnection) -> dict:
    """Test D: High realized vol predicts larger forward moves (non-directional)."""
    df = _load_forward_data(conn)
    df = df.sort_values("datetime_utc")
    df["abs_max_pips"] = df[["fwd_max_up_pips_24h", "fwd_max_down_pips_24h"]].max(axis=1)

    df["hourly_return"] = df["gbpeur_close"].pct_change()
    df["vol_24h"] = df["hourly_return"].rolling(24).std()

    valid = df.dropna(subset=["vol_24h", "abs_max_pips"])
    high_vol = valid[valid["vol_24h"] > valid["vol_24h"].quantile(0.8)]["abs_max_pips"]
    low_vol = valid[valid["vol_24h"] < valid["vol_24h"].quantile(0.2)]["abs_max_pips"]

    if len(high_vol) < 100 or len(low_vol) < 100:
        return {"test": "D: Volatility Clustering", "p_value": 1.0,
                "significant": False, "error": "too few samples"}

    t_stat, p_value = stats.ttest_ind(high_vol, low_vol)
    return {
        "test": "D: Volatility Clustering",
        "high_vol_mean_pips": float(high_vol.mean()),
        "low_vol_mean_pips": float(low_vol.mean()),
        "n_high": len(high_vol),
        "n_low": len(low_vol),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "significant": p_value < 0.01,
    }


def test_e_mean_reversion(conn: duckdb.DuckDBPyConnection) -> dict:
    """Test E: Distance from 480h high/low predicts reversal."""
    df = _load_forward_data(conn)
    df = df.sort_values("datetime_utc")
    df["high_480h"] = df["gbpeur_high"].rolling(480).max()
    df["low_480h"] = df["gbpeur_low"].rolling(480).min()
    rng = df["high_480h"] - df["low_480h"]
    df["dist_from_high"] = (df["gbpeur_close"] - df["high_480h"]) / rng.replace(0, np.nan)

    valid = df.dropna(subset=["dist_from_high", "fwd_close_pips_24h"])
    near_high = valid[valid["dist_from_high"] > -0.1]["fwd_close_pips_24h"]
    near_low = valid[valid["dist_from_high"] < -0.9]["fwd_close_pips_24h"]

    if len(near_high) < 50 or len(near_low) < 50:
        return {"test": "E: Mean Reversion", "p_value": 1.0,
                "significant": False, "error": f"too few (high={len(near_high)}, low={len(near_low)})"}

    t_stat, p_value = stats.ttest_ind(near_high, near_low)
    return {
        "test": "E: Mean Reversion from Extremes",
        "near_high_fwd_mean_pips": float(near_high.mean()),
        "near_low_fwd_mean_pips": float(near_low.mean()),
        "n_near_high": len(near_high),
        "n_near_low": len(near_low),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "significant": p_value < 0.01,
    }


def test_f_range_exhaustion(conn: duckdb.DuckDBPyConnection) -> dict:
    """Test F: When daily range is >80% used, further extension diminishes."""
    df = _load_forward_data(conn)
    df = df.sort_values("datetime_utc")

    # Compute each candle's range relative to its daily range
    daily_range = conn.execute("""
        SELECT date, MAX(gbpeur_high) - MIN(gbpeur_low) AS daily_range
        FROM fx_prices_hourly WHERE data_quality = 'OK'
        GROUP BY date
    """).fetchdf()
    daily_range["date"] = pd.to_datetime(daily_range["date"]).dt.date
    df["date_key"] = pd.to_datetime(df["date"]).dt.date

    df = df.merge(daily_range, left_on="date_key", right_on="date", how="left", suffixes=("", "_dr"))
    candle_range = df["gbpeur_high"] - df["gbpeur_low"]
    df["candle_vs_daily"] = candle_range / df["daily_range"].replace(0, np.nan)

    valid = df.dropna(subset=["candle_vs_daily", "fwd_max_up_pips_24h"])
    exhausted = valid[valid["candle_vs_daily"] > 0.6]["fwd_max_up_pips_24h"]
    fresh = valid[valid["candle_vs_daily"] < 0.2]["fwd_max_up_pips_24h"]

    if len(exhausted) < 50 or len(fresh) < 50:
        return {"test": "F: Range Exhaustion", "p_value": 1.0,
                "significant": False, "error": f"too few (exhausted={len(exhausted)}, fresh={len(fresh)})"}

    t_stat, p_value = stats.ttest_ind(fresh, exhausted)
    return {
        "test": "F: Range Exhaustion",
        "fresh_mean_pips": float(fresh.mean()),
        "exhausted_mean_pips": float(exhausted.mean()),
        "n_fresh": len(fresh),
        "n_exhausted": len(exhausted),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "significant": p_value < 0.01,
    }


def run_all_tests(conn: duckdb.DuckDBPyConnection) -> dict:
    create_all_tables(conn)

    tests = [
        ("A: London Session", test_a_london_session),
        ("B: Rate Differential", test_b_rate_differential),
        ("C: Momentum", test_c_momentum),
        ("D: Volatility Clustering", test_d_volatility_clustering),
        ("E: Mean Reversion", test_e_mean_reversion),
        ("F: Range Exhaustion", test_f_range_exhaustion),
    ]

    all_results = {}
    significant_count = 0

    for name, fn in tests:
        try:
            result = fn(conn)
            all_results[name] = result
            if result.get("significant", False):
                significant_count += 1
                logger.info("  %s: SIGNIFICANT (p=%.6f)", name, result["p_value"])
            else:
                logger.info("  %s: not significant (p=%.6f)", name, result.get("p_value", 1.0))
        except Exception as e:
            logger.error("  %s: FAILED - %s", name, e)
            all_results[name] = {"test": name, "error": str(e), "significant": False}

    go = significant_count >= 2
    return {
        "tests_run": len(tests),
        "significant_count": significant_count,
        "go_decision": go,
        "results": all_results,
    }


def print_test_results(summary: dict):
    print(f"\n{'='*70}")
    print("FX HYPOTHESIS TEST RESULTS")
    print(f"{'='*70}")

    for test_name, r in summary["results"].items():
        sig = "***" if r.get("significant") else ""
        print(f"\n--- {test_name} ---")
        if "error" in r and not r.get("significant"):
            print(f"  SKIPPED: {r['error']}")
        else:
            print(f"  p={r.get('p_value', 1):.6f} {sig}")
            for k, v in r.items():
                if k not in ("test", "p_value", "significant", "t_stat", "error"):
                    if isinstance(v, float):
                        print(f"    {k}: {v:.4f}")
                    else:
                        print(f"    {k}: {v}")

    print(f"\n{'='*70}")
    go = summary["go_decision"]
    n_sig = summary["significant_count"]
    print(f"DECISION: {'GO' if go else 'NO-GO'} -- {n_sig}/6 tests significant (threshold: 2)")
    print(f"{'='*70}")
