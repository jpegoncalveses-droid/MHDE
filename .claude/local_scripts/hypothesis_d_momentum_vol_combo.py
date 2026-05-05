"""
Test D: Momentum + Volatility Combo Signal
Take stocks in momentum Q5 (strongest 20%) AND volatility Q4-Q5 (highest 40%).
Compare 20d forward return and +5% hit rate against base rate.
"""

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

DB_PATH = "data/mhde.duckdb"
con = duckdb.connect(DB_PATH, read_only=True)

df_companies = con.execute("SELECT ticker, market_cap FROM companies").fetchdf()


def bucket_market_cap(mc):
    if pd.isna(mc):
        return "Unknown"
    if mc > 200e9:
        return "Mega (>200B)"
    if mc > 50e9:
        return "Large (50-200B)"
    if mc > 10e9:
        return "Mid (10-50B)"
    if mc > 2e9:
        return "Small (2-10B)"
    return "Micro (<2B)"


query = """
WITH valid_prices AS (
    SELECT ticker, trade_date, adjusted_close
    FROM prices_daily
    WHERE trade_date >= '2025-04-15' AND trade_date <= '2026-05-04'
      AND adjusted_close > 0
),
daily_returns AS (
    SELECT
        ticker, trade_date, adjusted_close,
        LN(adjusted_close / LAG(adjusted_close) OVER (PARTITION BY ticker ORDER BY trade_date)) AS log_return,
        ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
    FROM valid_prices
),
numbered AS (
    SELECT ticker, trade_date, adjusted_close, rn
    FROM daily_returns
),
with_metrics AS (
    SELECT
        a.ticker,
        a.trade_date,
        a.adjusted_close,
        a.rn,
        (a.adjusted_close / b.adjusted_close) - 1 AS trailing_return_20d,
        STDDEV(dr.log_return) OVER (PARTITION BY a.ticker ORDER BY a.trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) * SQRT(252) AS realized_vol_20d
    FROM numbered a
    JOIN numbered b ON a.ticker = b.ticker AND b.rn = a.rn - 20
    JOIN daily_returns dr ON dr.ticker = a.ticker AND dr.trade_date = a.trade_date
    WHERE a.trade_date >= '2025-05-30'
      AND a.trade_date <= '2026-04-04'
),
filtered AS (
    SELECT ticker, trade_date, adjusted_close, trailing_return_20d, realized_vol_20d
    FROM with_metrics
    WHERE realized_vol_20d IS NOT NULL
      AND trailing_return_20d IS NOT NULL
),
with_forward AS (
    SELECT
        f.ticker, f.trade_date, f.trailing_return_20d, f.realized_vol_20d, f.adjusted_close,
        p.adjusted_close AS future_close,
        ROW_NUMBER() OVER (PARTITION BY f.ticker, f.trade_date ORDER BY p.trade_date ASC) AS fwd_rank
    FROM filtered f
    JOIN prices_daily p ON p.ticker = f.ticker AND p.trade_date > f.trade_date
)
SELECT
    ticker, trade_date, trailing_return_20d, realized_vol_20d,
    (future_close / adjusted_close) - 1 AS forward_return_20d
FROM with_forward
WHERE fwd_rank = 20
"""

print("Computing momentum + volatility metrics with 20d forward returns...")
df = con.execute(query).fetchdf()
con.close()
print(f"  Got {len(df)} observations")

df = df.merge(df_companies, on="ticker", how="left")
df["bucket"] = df["market_cap"].apply(bucket_market_cap)

# Assign quintiles
df["mom_quintile"] = pd.qcut(df["trailing_return_20d"], 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
df["vol_quintile"] = pd.qcut(df["realized_vol_20d"], 5, labels=["V1", "V2", "V3", "V4", "V5"])

# Define groups
combo = df[(df["mom_quintile"] == "Q5") & (df["vol_quintile"].isin(["V4", "V5"]))]
mom_only = df[df["mom_quintile"] == "Q5"]
vol_only = df[df["vol_quintile"].isin(["V4", "V5"])]
base = df

print(f"\n{'=' * 90}")
print("TEST D: MOMENTUM Q5 + VOLATILITY Q4-Q5 COMBO SIGNAL")
print(f"{'=' * 90}")
print(f"\n  Universe: {len(base)} total observations")
print(f"  Momentum Q5 only:      {len(mom_only)} ({len(mom_only)/len(base)*100:.1f}%)")
print(f"  Volatility Q4-Q5 only: {len(vol_only)} ({len(vol_only)/len(base)*100:.1f}%)")
print(f"  Combo (both):           {len(combo)} ({len(combo)/len(base)*100:.1f}%)")

# Overall comparison
print(f"\n{'Group':<28} | {'N':>6} | {'Mean%':>7} | {'Med%':>7} | {'>+5%':>6} | {'>+10%':>6} | {'<-5%':>6}")
print("-" * 85)

groups = [
    ("Base rate (all)", base),
    ("Momentum Q5 only", mom_only),
    ("Volatility Q4-Q5 only", vol_only),
    ("COMBO (Mom Q5 + Vol Q4-Q5)", combo),
]

for name, grp in groups:
    r = grp["forward_return_20d"]
    print(
        f"{name:<28} | {len(r):>6} | {r.mean()*100:>+7.2f} | {r.median()*100:>+7.2f} "
        f"| {(r>0.05).mean()*100:>5.1f}% | {(r>0.10).mean()*100:>5.1f}% | {(r<-0.05).mean()*100:>5.1f}%"
    )

# By market cap bucket
bucket_order = ["Mega (>200B)", "Large (50-200B)", "Mid (10-50B)", "Small (2-10B)", "Micro (<2B)"]

print(f"\n{'=' * 90}")
print("COMBO SIGNAL BY MARKET CAP BUCKET")
print(f"{'=' * 90}")
print(f"\n{'Bucket':<18} | {'N combo':>7} | {'Combo Mean%':>11} | {'Base Mean%':>10} | {'Diff%':>6} | {'Combo >5%':>9} | {'Base >5%':>8} | {'Lift':>6}")
print("-" * 100)

for b in bucket_order:
    c_ret = combo[combo["bucket"] == b]["forward_return_20d"]
    b_ret = base[base["bucket"] == b]["forward_return_20d"]
    if len(c_ret) < 10:
        print(f"{b:<18} | {len(c_ret):>7} | {'n/a':>11} | {b_ret.mean()*100:>+10.2f} | {'n/a':>6} | {'n/a':>9} | {(b_ret>0.05).mean()*100:>7.1f}% | {'n/a':>6}")
        continue
    combo_hit = (c_ret > 0.05).mean() * 100
    base_hit = (b_ret > 0.05).mean() * 100
    lift = combo_hit - base_hit
    diff = c_ret.mean() * 100 - b_ret.mean() * 100
    print(
        f"{b:<18} | {len(c_ret):>7} | {c_ret.mean()*100:>+11.2f} | {b_ret.mean()*100:>+10.2f} | {diff:>+6.2f} "
        f"| {combo_hit:>8.1f}% | {base_hit:>7.1f}% | {lift:>+5.1f}%"
    )

# Statistical test: combo vs base
print(f"\n{'=' * 90}")
print("STATISTICAL TESTS")
print(f"{'=' * 90}")

combo_ret = combo["forward_return_20d"].values
base_ret = base["forward_return_20d"].values

t_stat, t_pval = stats.ttest_ind(combo_ret, base_ret, equal_var=False)
u_stat, u_pval = stats.mannwhitneyu(combo_ret, base_ret, alternative="two-sided")
print(f"\n  Combo vs Base (all observations):")
print(f"    Welch's t-test: t={t_stat:+.4f}, p={t_pval:.6f} {'*** SIG' if t_pval < 0.05 else ''}")
print(f"    Mann-Whitney:   U={u_stat:.0f}, p={u_pval:.6f} {'*** SIG' if u_pval < 0.05 else ''}")

# Combo vs momentum-only
t2, p2 = stats.ttest_ind(combo_ret, mom_only["forward_return_20d"].values, equal_var=False)
print(f"\n  Combo vs Momentum Q5 only:")
print(f"    Welch's t-test: t={t2:+.4f}, p={p2:.6f} {'*** SIG' if p2 < 0.05 else ''}")
print(f"    Does adding vol filter improve on momentum alone? {'YES' if combo["forward_return_20d"].mean() > mom_only["forward_return_20d"].mean() else 'NO'}")

# Risk-adjusted: Sharpe-like ratio (mean/std)
combo_sharpe = combo["forward_return_20d"].mean() / combo["forward_return_20d"].std()
base_sharpe = base["forward_return_20d"].mean() / base["forward_return_20d"].std()
mom_sharpe = mom_only["forward_return_20d"].mean() / mom_only["forward_return_20d"].std()

print(f"\n  Risk-adjusted (mean/std ratio, higher = better):")
print(f"    Base:        {base_sharpe:.4f}")
print(f"    Mom Q5:      {mom_sharpe:.4f}")
print(f"    Combo:       {combo_sharpe:.4f}")

# Downside analysis
print(f"\n  Downside comparison:")
print(f"    Base  — worst 5th pct: {np.percentile(base_ret, 5)*100:+.2f}%  worst 1st pct: {np.percentile(base_ret, 1)*100:+.2f}%")
print(f"    Mom Q5 — worst 5th pct: {np.percentile(mom_only['forward_return_20d'], 5)*100:+.2f}%  worst 1st pct: {np.percentile(mom_only['forward_return_20d'], 1)*100:+.2f}%")
print(f"    Combo — worst 5th pct: {np.percentile(combo_ret, 5)*100:+.2f}%  worst 1st pct: {np.percentile(combo_ret, 1)*100:+.2f}%")

print(f"\n{'=' * 90}")
print("INTERPRETATION")
print(f"{'=' * 90}")
combo_mean = combo["forward_return_20d"].mean() * 100
base_mean = base["forward_return_20d"].mean() * 100
combo_5pct = (combo["forward_return_20d"] > 0.05).mean() * 100
base_5pct = (base["forward_return_20d"] > 0.05).mean() * 100
print(f"  Combo signal mean return:  {combo_mean:+.2f}% vs base {base_mean:+.2f}% (diff: {combo_mean-base_mean:+.2f}%)")
print(f"  Combo >5% hit rate:        {combo_5pct:.1f}% vs base {base_5pct:.1f}% (lift: {combo_5pct-base_5pct:+.1f}%)")
print(f"  Combo observations:        {len(combo)} ({len(combo)/len(base)*100:.1f}% of universe)")
