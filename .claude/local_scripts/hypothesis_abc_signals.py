"""
Three signal hypothesis tests:
A) Form 4 insider clusters (3+ filings in 7 days) vs 20d forward return
B) Trailing 20d momentum quintiles vs 20d forward return (continuation vs reversal)
C) 20d realized volatility quintiles vs 20d forward return (vol compression breakout)
"""

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

DB_PATH = "data/mhde.duckdb"
con = duckdb.connect(DB_PATH, read_only=True)

# Load companies for market cap bucketing
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


def compute_stats(returns):
    if len(returns) == 0:
        return {"n": 0, "mean": np.nan, "median": np.nan, "pct_5": np.nan, "pct_10": np.nan}
    r = np.array(returns)
    return {
        "n": len(r),
        "mean": np.mean(r) * 100,
        "median": np.median(r) * 100,
        "pct_5": (r > 0.05).mean() * 100,
        "pct_10": (r > 0.10).mean() * 100,
    }


def print_table(df_signal, df_control, label_col="bucket", title=""):
    bucket_order = ["Mega (>200B)", "Large (50-200B)", "Mid (10-50B)", "Small (2-10B)", "Micro (<2B)", "Unknown"]
    header = f"{label_col:<18} | {'N':>5} | {'Mean%':>7} | {'Med%':>7} | {'>5%':>5} | {'>10%':>5} || {'N':>5} | {'Mean%':>7} | {'Med%':>7} | {'>5%':>5} | {'>10%':>5} || {'Diff%':>6}"
    print(f"\n{'':18} | {'--- Signal Group ---':^37} || {'--- Control Group ---':^37} ||")
    print(header)
    print("-" * len(header))

    all_sig, all_ctl = [], []
    for bucket in bucket_order:
        s_ret = df_signal[df_signal["bucket"] == bucket]["forward_return_20d"].values
        c_ret = df_control[df_control["bucket"] == bucket]["forward_return_20d"].values
        fs = compute_stats(s_ret)
        cs = compute_stats(c_ret)
        diff = fs["mean"] - cs["mean"] if not (np.isnan(fs["mean"]) or np.isnan(cs["mean"])) else np.nan
        print(
            f"{bucket:<18} | {fs['n']:>5} | {fs['mean']:>+7.2f} | {fs['median']:>+7.2f} | {fs['pct_5']:>5.1f} | {fs['pct_10']:>5.1f} "
            f"|| {cs['n']:>5} | {cs['mean']:>+7.2f} | {cs['median']:>+7.2f} | {cs['pct_5']:>5.1f} | {cs['pct_10']:>5.1f} "
            f"|| {diff:>+6.2f}"
        )
        all_sig.extend(s_ret)
        all_ctl.extend(c_ret)

    print("-" * len(header))
    fs = compute_stats(all_sig)
    cs = compute_stats(all_ctl)
    diff = fs["mean"] - cs["mean"]
    print(
        f"{'ALL':<18} | {fs['n']:>5} | {fs['mean']:>+7.2f} | {fs['median']:>+7.2f} | {fs['pct_5']:>5.1f} | {fs['pct_10']:>5.1f} "
        f"|| {cs['n']:>5} | {cs['mean']:>+7.2f} | {cs['median']:>+7.2f} | {cs['pct_5']:>5.1f} | {cs['pct_10']:>5.1f} "
        f"|| {diff:>+6.2f}"
    )
    return np.array(all_sig), np.array(all_ctl)


def print_stat_tests(sig_returns, ctl_returns):
    t_stat, t_pval = stats.ttest_ind(sig_returns, ctl_returns, equal_var=False)
    u_stat, u_pval = stats.mannwhitneyu(sig_returns, ctl_returns, alternative="two-sided")
    print(f"\n  Welch's t-test:    t={t_stat:+.4f}, p={t_pval:.6f} {'*** SIG' if t_pval < 0.05 else ''}")
    print(f"  Mann-Whitney U:    U={u_stat:.0f}, p={u_pval:.6f} {'*** SIG' if u_pval < 0.05 else ''}")
    print(f"  Signal mean: {np.mean(sig_returns)*100:+.3f}%  Control mean: {np.mean(ctl_returns)*100:+.3f}%  Diff: {(np.mean(sig_returns)-np.mean(ctl_returns))*100:+.3f}%")


# =============================================================================
# TEST A: Form 4 Insider Clusters (3+ filings within 7 days)
# =============================================================================
print("\n" + "=" * 90)
print("TEST A: FORM 4 INSIDER CLUSTERS (3+ distinct filings within 7 days)")
print("=" * 90)

cluster_query = """
WITH unique_form4 AS (
    SELECT DISTINCT ticker, filing_date
    FROM filings
    WHERE form_type IN ('4', '4/A')
      AND filing_date >= '2025-05-02'
      AND filing_date <= '2026-04-04'
),
-- For each filing, count how many other filings the same ticker had within 7 days
filing_with_cluster AS (
    SELECT
        a.ticker,
        a.filing_date,
        COUNT(*) AS filings_in_window
    FROM unique_form4 a
    JOIN unique_form4 b
        ON a.ticker = b.ticker
        AND b.filing_date BETWEEN a.filing_date - INTERVAL '7 days' AND a.filing_date
    GROUP BY a.ticker, a.filing_date
),
-- Keep only cluster starts (3+ filings in trailing 7d window)
clusters AS (
    SELECT ticker, filing_date
    FROM filing_with_cluster
    WHERE filings_in_window >= 3
),
-- Get entry price (filing date or next trading day)
cluster_entry AS (
    SELECT
        c.ticker,
        c.filing_date,
        p.trade_date AS entry_date,
        p.adjusted_close AS entry_close,
        ROW_NUMBER() OVER (PARTITION BY c.ticker, c.filing_date ORDER BY p.trade_date ASC) AS rn
    FROM clusters c
    JOIN prices_daily p
        ON p.ticker = c.ticker
        AND p.trade_date >= c.filing_date
        AND p.trade_date <= c.filing_date + INTERVAL '5 days'
),
valid_entry AS (
    SELECT ticker, filing_date, entry_date, entry_close
    FROM cluster_entry WHERE rn = 1
),
-- Get exit price 20 trading days later
future AS (
    SELECT
        ve.ticker, ve.filing_date, ve.entry_date, ve.entry_close,
        p.adjusted_close AS future_close,
        ROW_NUMBER() OVER (PARTITION BY ve.ticker, ve.filing_date ORDER BY p.trade_date ASC) AS day_rank
    FROM valid_entry ve
    JOIN prices_daily p ON p.ticker = ve.ticker AND p.trade_date > ve.entry_date
)
SELECT
    ticker, filing_date, entry_date, entry_close, future_close,
    (future_close / entry_close) - 1 AS forward_return_20d
FROM future
WHERE day_rank = 20
"""

df_clusters = con.execute(cluster_query).fetchdf()
df_clusters = df_clusters.merge(df_companies, on="ticker", how="left")
df_clusters["bucket"] = df_clusters["market_cap"].apply(bucket_market_cap)
print(f"  Found {len(df_clusters)} insider cluster events")

# Control: random dates for the same tickers, same counts
np.random.seed(43)
all_trade_dates = con.execute(
    "SELECT DISTINCT trade_date FROM prices_daily WHERE trade_date >= '2025-05-02' AND trade_date <= '2026-04-04' ORDER BY trade_date"
).fetchdf()["trade_date"].values

counts_a = df_clusters.groupby("ticker").size().to_dict()
entry_dates_a = df_clusters.groupby("ticker")["entry_date"].apply(set).to_dict()
control_a_records = []
for ticker, n in counts_a.items():
    used = entry_dates_a.get(ticker, set())
    candidates = [d for d in all_trade_dates if pd.Timestamp(d) not in used]
    if len(candidates) < n:
        continue
    chosen = np.random.choice(candidates, size=n, replace=False)
    for d in chosen:
        control_a_records.append({"ticker": ticker, "control_date": pd.Timestamp(d).date()})

df_ctrl_a_input = pd.DataFrame(control_a_records)
con.register("control_a_dates", df_ctrl_a_input)

ctrl_a_query = """
WITH entry AS (
    SELECT c.ticker, c.control_date, p.trade_date AS entry_date, p.adjusted_close AS entry_close,
        ROW_NUMBER() OVER (PARTITION BY c.ticker, c.control_date ORDER BY p.trade_date ASC) AS rn
    FROM control_a_dates c
    JOIN prices_daily p ON p.ticker = c.ticker AND p.trade_date >= c.control_date AND p.trade_date <= c.control_date + INTERVAL '5 days'
),
valid AS (SELECT ticker, control_date, entry_date, entry_close FROM entry WHERE rn = 1),
future AS (
    SELECT v.ticker, v.entry_date, v.entry_close, p.adjusted_close AS future_close,
        ROW_NUMBER() OVER (PARTITION BY v.ticker, v.control_date ORDER BY p.trade_date ASC) AS day_rank
    FROM valid v JOIN prices_daily p ON p.ticker = v.ticker AND p.trade_date > v.entry_date
)
SELECT ticker, entry_close, future_close, (future_close / entry_close) - 1 AS forward_return_20d
FROM future WHERE day_rank = 20
"""
df_ctrl_a = con.execute(ctrl_a_query).fetchdf()
df_ctrl_a = df_ctrl_a.merge(df_companies, on="ticker", how="left")
df_ctrl_a["bucket"] = df_ctrl_a["market_cap"].apply(bucket_market_cap)
print(f"  Control group: {len(df_ctrl_a)} samples")

sig_a, ctl_a = print_table(df_clusters, df_ctrl_a)
print_stat_tests(sig_a, ctl_a)


# =============================================================================
# TEST B: MOMENTUM QUINTILES
# =============================================================================
print("\n\n" + "=" * 90)
print("TEST B: TRAILING 20-DAY MOMENTUM QUINTILES vs 20-DAY FORWARD RETURN")
print("=" * 90)

momentum_query = """
WITH numbered AS (
    SELECT
        ticker, trade_date, adjusted_close,
        ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
    FROM prices_daily
    WHERE trade_date >= '2025-05-02' AND trade_date <= '2026-05-04'
),
with_trailing AS (
    SELECT
        a.ticker, a.trade_date, a.adjusted_close, a.rn,
        b.adjusted_close AS trailing_close,
        (a.adjusted_close / b.adjusted_close) - 1 AS trailing_return_20d
    FROM numbered a
    JOIN numbered b ON a.ticker = b.ticker AND b.rn = a.rn - 20
    WHERE a.trade_date <= '2026-04-04'
),
with_forward AS (
    SELECT
        wt.ticker, wt.trade_date, wt.trailing_return_20d, wt.adjusted_close,
        f.adjusted_close AS future_close,
        ROW_NUMBER() OVER (PARTITION BY wt.ticker, wt.trade_date ORDER BY f.trade_date ASC) AS fwd_rank
    FROM with_trailing wt
    JOIN prices_daily f ON f.ticker = wt.ticker AND f.trade_date > wt.trade_date
)
SELECT
    ticker, trade_date, trailing_return_20d,
    (future_close / adjusted_close) - 1 AS forward_return_20d
FROM with_forward
WHERE fwd_rank = 20
"""

print("  Computing trailing & forward returns (this may take a moment)...")
df_momentum = con.execute(momentum_query).fetchdf()
print(f"  Got {len(df_momentum)} ticker-date observations with both trailing and forward 20d returns")

df_momentum["quintile"] = pd.qcut(df_momentum["trailing_return_20d"], 5, labels=["Q1 (weakest)", "Q2", "Q3", "Q4", "Q5 (strongest)"])
df_momentum = df_momentum.merge(df_companies, on="ticker", how="left")
df_momentum["bucket"] = df_momentum["market_cap"].apply(bucket_market_cap)

quintile_order = ["Q1 (weakest)", "Q2", "Q3", "Q4", "Q5 (strongest)"]

print(f"\n{'Quintile':<16} | {'N':>6} | {'Trail Mean%':>11} | {'Fwd Mean%':>9} | {'Fwd Med%':>8} | {'>+5%':>5} | {'>+10%':>5} | {'<-5%':>5}")
print("-" * 90)

for q in quintile_order:
    qdf = df_momentum[df_momentum["quintile"] == q]
    trail_mean = qdf["trailing_return_20d"].mean() * 100
    fwd_mean = qdf["forward_return_20d"].mean() * 100
    fwd_med = qdf["forward_return_20d"].median() * 100
    pct_up5 = (qdf["forward_return_20d"] > 0.05).mean() * 100
    pct_up10 = (qdf["forward_return_20d"] > 0.10).mean() * 100
    pct_dn5 = (qdf["forward_return_20d"] < -0.05).mean() * 100
    print(f"{q:<16} | {len(qdf):>6} | {trail_mean:>+11.2f} | {fwd_mean:>+9.2f} | {fwd_med:>+8.2f} | {pct_up5:>5.1f} | {pct_up10:>5.1f} | {pct_dn5:>5.1f}")

# Test: Q5 vs Q1
q5_ret = df_momentum[df_momentum["quintile"] == "Q5 (strongest)"]["forward_return_20d"].values
q1_ret = df_momentum[df_momentum["quintile"] == "Q1 (weakest)"]["forward_return_20d"].values
print(f"\n  Q5 (strongest) vs Q1 (weakest) forward returns:")
t_stat, t_pval = stats.ttest_ind(q5_ret, q1_ret, equal_var=False)
u_stat, u_pval = stats.mannwhitneyu(q5_ret, q1_ret, alternative="two-sided")
print(f"    Welch's t-test: t={t_stat:+.4f}, p={t_pval:.6f} {'*** SIG' if t_pval < 0.05 else ''}")
print(f"    Mann-Whitney:   U={u_stat:.0f}, p={u_pval:.6f} {'*** SIG' if u_pval < 0.05 else ''}")
print(f"    Q5 mean: {np.mean(q5_ret)*100:+.3f}%  Q1 mean: {np.mean(q1_ret)*100:+.3f}%")

# Breakdown by market cap for Q5 vs Q1
print(f"\n  Momentum signal by market cap (Q5 mean - Q1 mean):")
bucket_order = ["Mega (>200B)", "Large (50-200B)", "Mid (10-50B)", "Small (2-10B)", "Micro (<2B)"]
for b in bucket_order:
    q5b = df_momentum[(df_momentum["quintile"] == "Q5 (strongest)") & (df_momentum["bucket"] == b)]["forward_return_20d"]
    q1b = df_momentum[(df_momentum["quintile"] == "Q1 (weakest)") & (df_momentum["bucket"] == b)]["forward_return_20d"]
    if len(q5b) > 10 and len(q1b) > 10:
        diff = q5b.mean() - q1b.mean()
        print(f"    {b:<18}: Q5={q5b.mean()*100:+.2f}%  Q1={q1b.mean()*100:+.2f}%  Diff={diff*100:+.2f}%  (n={len(q5b)}+{len(q1b)})")


# =============================================================================
# TEST C: VOLATILITY COMPRESSION
# =============================================================================
print("\n\n" + "=" * 90)
print("TEST C: 20-DAY REALIZED VOLATILITY QUINTILES vs FORWARD RETURNS")
print("=" * 90)

vol_query = """
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
with_vol AS (
    SELECT
        ticker, trade_date, adjusted_close, rn,
        STDDEV(log_return) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) * SQRT(252) AS realized_vol_20d
    FROM daily_returns
),
filtered AS (
    SELECT ticker, trade_date, adjusted_close, realized_vol_20d
    FROM with_vol
    WHERE realized_vol_20d IS NOT NULL
      AND trade_date >= '2025-05-30'
      AND trade_date <= '2026-04-04'
),
with_forward AS (
    SELECT
        f.ticker, f.trade_date, f.realized_vol_20d, f.adjusted_close,
        p.adjusted_close AS future_close,
        ROW_NUMBER() OVER (PARTITION BY f.ticker, f.trade_date ORDER BY p.trade_date ASC) AS fwd_rank
    FROM filtered f
    JOIN prices_daily p ON p.ticker = f.ticker AND p.trade_date > f.trade_date
)
SELECT
    ticker, trade_date, realized_vol_20d,
    (future_close / adjusted_close) - 1 AS forward_return_20d
FROM with_forward
WHERE fwd_rank = 20
"""

print("  Computing realized volatility and forward returns...")
df_vol = con.execute(vol_query).fetchdf()
print(f"  Got {len(df_vol)} observations")

df_vol["vol_quintile"] = pd.qcut(df_vol["realized_vol_20d"], 5, labels=["Q1 (lowest vol)", "Q2", "Q3", "Q4", "Q5 (highest vol)"])
df_vol = df_vol.merge(df_companies, on="ticker", how="left")
df_vol["bucket"] = df_vol["market_cap"].apply(bucket_market_cap)

vol_quintile_order = ["Q1 (lowest vol)", "Q2", "Q3", "Q4", "Q5 (highest vol)"]

print(f"\n{'Vol Quintile':<18} | {'N':>6} | {'Avg Vol':>7} | {'Fwd Mean%':>9} | {'Fwd Med%':>8} | {'>+5%':>5} | {'>+10%':>5} | {'<-5%':>5}")
print("-" * 90)

for q in vol_quintile_order:
    qdf = df_vol[df_vol["vol_quintile"] == q]
    avg_vol = qdf["realized_vol_20d"].mean() * 100
    fwd_mean = qdf["forward_return_20d"].mean() * 100
    fwd_med = qdf["forward_return_20d"].median() * 100
    pct_up5 = (qdf["forward_return_20d"] > 0.05).mean() * 100
    pct_up10 = (qdf["forward_return_20d"] > 0.10).mean() * 100
    pct_dn5 = (qdf["forward_return_20d"] < -0.05).mean() * 100
    print(f"{q:<18} | {len(qdf):>6} | {avg_vol:>6.1f}% | {fwd_mean:>+9.2f} | {fwd_med:>+8.2f} | {pct_up5:>5.1f} | {pct_up10:>5.1f} | {pct_dn5:>5.1f}")

# Test: Q1 (lowest vol) vs Q5 (highest vol)
q1_vol = df_vol[df_vol["vol_quintile"] == "Q1 (lowest vol)"]["forward_return_20d"].values
q5_vol = df_vol[df_vol["vol_quintile"] == "Q5 (highest vol)"]["forward_return_20d"].values
print(f"\n  Q1 (lowest vol) vs Q5 (highest vol):")
t_stat, t_pval = stats.ttest_ind(q1_vol, q5_vol, equal_var=False)
u_stat, u_pval = stats.mannwhitneyu(q1_vol, q5_vol, alternative="two-sided")
print(f"    Welch's t-test: t={t_stat:+.4f}, p={t_pval:.6f} {'*** SIG' if t_pval < 0.05 else ''}")
print(f"    Mann-Whitney:   U={u_stat:.0f}, p={u_pval:.6f} {'*** SIG' if u_pval < 0.05 else ''}")
print(f"    Q1 (low vol) mean: {np.mean(q1_vol)*100:+.3f}%  Q5 (high vol) mean: {np.mean(q5_vol)*100:+.3f}%")

# Frequency of +5% moves by vol quintile and market cap
print(f"\n  Frequency of >+5% moves by vol quintile and market cap:")
print(f"  {'Bucket':<18} | {'Q1(lo)':>6} | {'Q2':>6} | {'Q3':>6} | {'Q4':>6} | {'Q5(hi)':>6}")
print(f"  {'-'*70}")
for b in bucket_order:
    row = f"  {b:<18} |"
    for q in vol_quintile_order:
        subset = df_vol[(df_vol["vol_quintile"] == q) & (df_vol["bucket"] == b)]
        if len(subset) > 10:
            pct = (subset["forward_return_20d"] > 0.05).mean() * 100
            row += f" {pct:>5.1f}% |"
        else:
            row += f"   n/a |"
    print(row)

con.close()

print("\n\n" + "=" * 90)
print("SUMMARY")
print("=" * 90)
print("  Test A (Insider Clusters): Do 3+ Form 4 filings in 7 days predict returns?")
print("  Test B (Momentum):         Does trailing 20d return predict forward 20d return?")
print("  Test C (Vol Compression):  Does low realized vol predict big forward moves?")
print("=" * 90)
