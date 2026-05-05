"""
Hypothesis test: Do 8-K filings predict forward returns?
Is the signal stronger for smaller stocks?
"""

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

DB_PATH = "data/mhde.duckdb"

con = duckdb.connect(DB_PATH, read_only=True)

# Step 1 & 2: Get unique 8-K filings within the price window, join to prices
# Price window: 2025-05-02 to 2026-05-04
# We need 20 trading days of forward returns, so filing cutoff ~ 2026-04-04
filing_query = """
WITH unique_filings AS (
    SELECT DISTINCT ticker, filing_date
    FROM filings
    WHERE form_type = '8-K'
      AND filing_date >= '2025-05-02'
      AND filing_date <= '2026-04-04'
),
filing_with_close AS (
    SELECT
        f.ticker,
        f.filing_date,
        p.trade_date AS entry_date,
        p.adjusted_close AS filing_close,
        ROW_NUMBER() OVER (PARTITION BY f.ticker, f.filing_date ORDER BY p.trade_date ASC) AS rn
    FROM unique_filings f
    JOIN prices_daily p
        ON p.ticker = f.ticker
        AND p.trade_date >= f.filing_date
        AND p.trade_date <= f.filing_date + INTERVAL '5 days'
),
filing_entry AS (
    SELECT ticker, filing_date, entry_date, filing_close
    FROM filing_with_close
    WHERE rn = 1
),
future_prices AS (
    SELECT
        fe.ticker,
        fe.filing_date,
        fe.entry_date,
        fe.filing_close,
        p.trade_date AS exit_date,
        p.adjusted_close AS future_close,
        ROW_NUMBER() OVER (PARTITION BY fe.ticker, fe.filing_date ORDER BY p.trade_date ASC) AS day_rank
    FROM filing_entry fe
    JOIN prices_daily p
        ON p.ticker = fe.ticker
        AND p.trade_date > fe.entry_date
)
SELECT
    ticker,
    filing_date,
    entry_date,
    filing_close,
    exit_date,
    future_close,
    (future_close / filing_close) - 1 AS forward_return_20d
FROM future_prices
WHERE day_rank = 20
"""

print("Loading 8-K filing events and computing 20-day forward returns...")
df_filings = con.execute(filing_query).fetchdf()
print(f"  Found {len(df_filings)} 8-K filing events with valid 20-day forward returns")

# Step 3: Join to companies for market_cap bucketing
mktcap_query = "SELECT ticker, market_cap FROM companies"
df_companies = con.execute(mktcap_query).fetchdf()

df_filings = df_filings.merge(df_companies, on="ticker", how="left")


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


df_filings["bucket"] = df_filings["market_cap"].apply(bucket_market_cap)

# Step 4: Control group - random non-filing dates, same number per ticker
print("Building control group (random non-filing dates)...")

all_trade_dates_query = """
SELECT DISTINCT trade_date FROM prices_daily
WHERE trade_date >= '2025-05-02' AND trade_date <= '2026-04-04'
ORDER BY trade_date
"""
all_trade_dates = con.execute(all_trade_dates_query).fetchdf()["trade_date"].values

filing_dates_by_ticker = df_filings.groupby("ticker")["entry_date"].apply(set).to_dict()
counts_by_ticker = df_filings.groupby("ticker").size().to_dict()

np.random.seed(42)
control_records = []

for ticker, n_samples in counts_by_ticker.items():
    filing_set = filing_dates_by_ticker.get(ticker, set())
    candidate_dates = [d for d in all_trade_dates if pd.Timestamp(d) not in filing_set]
    if len(candidate_dates) < n_samples:
        continue
    chosen = np.random.choice(candidate_dates, size=n_samples, replace=False)
    for d in chosen:
        control_records.append({"ticker": ticker, "control_date": pd.Timestamp(d).date()})

df_control_input = pd.DataFrame(control_records)
print(f"  Generated {len(df_control_input)} control date samples across {len(counts_by_ticker)} tickers")

con.register("control_dates", df_control_input)

control_query = """
WITH control_entry AS (
    SELECT
        c.ticker,
        c.control_date,
        p.trade_date AS entry_date,
        p.adjusted_close AS entry_close,
        ROW_NUMBER() OVER (PARTITION BY c.ticker, c.control_date ORDER BY p.trade_date ASC) AS rn
    FROM control_dates c
    JOIN prices_daily p
        ON p.ticker = c.ticker
        AND p.trade_date >= c.control_date
        AND p.trade_date <= c.control_date + INTERVAL '5 days'
),
control_valid AS (
    SELECT ticker, control_date, entry_date, entry_close
    FROM control_entry
    WHERE rn = 1
),
control_future AS (
    SELECT
        cv.ticker,
        cv.control_date,
        cv.entry_date,
        cv.entry_close,
        p.adjusted_close AS future_close,
        ROW_NUMBER() OVER (PARTITION BY cv.ticker, cv.control_date ORDER BY p.trade_date ASC) AS day_rank
    FROM control_valid cv
    JOIN prices_daily p
        ON p.ticker = cv.ticker
        AND p.trade_date > cv.entry_date
)
SELECT
    ticker,
    control_date,
    entry_date,
    entry_close,
    future_close,
    (future_close / entry_close) - 1 AS forward_return_20d
FROM control_future
WHERE day_rank = 20
"""

df_control = con.execute(control_query).fetchdf()
df_control = df_control.merge(df_companies, on="ticker", how="left")
df_control["bucket"] = df_control["market_cap"].apply(bucket_market_cap)
print(f"  Control group: {len(df_control)} samples with valid 20-day forward returns")

con.close()

# Step 5: Summary table
print("\n" + "=" * 90)
print("HYPOTHESIS TEST: 8-K FILINGS vs FORWARD 20-DAY RETURNS")
print("=" * 90)

bucket_order = ["Mega (>200B)", "Large (50-200B)", "Mid (10-50B)", "Small (2-10B)", "Micro (<2B)", "Unknown"]


def compute_stats(group):
    if len(group) == 0:
        return {"n": 0, "mean": np.nan, "median": np.nan, "pct_5": np.nan, "pct_10": np.nan}
    returns = group["forward_return_20d"]
    return {
        "n": len(returns),
        "mean": returns.mean() * 100,
        "median": returns.median() * 100,
        "pct_5": (returns > 0.05).mean() * 100,
        "pct_10": (returns > 0.10).mean() * 100,
    }


header = f"{'Bucket':<18} | {'N':>5} | {'Mean%':>7} | {'Med%':>7} | {'>5%':>5} | {'>10%':>5} || {'N':>5} | {'Mean%':>7} | {'Med%':>7} | {'>5%':>5} | {'>10%':>5} || {'Diff%':>6}"
print(f"\n{'':18} | {'--- 8-K Filing Group ---':^37} || {'--- Control Group ---':^37} ||")
print(header)
print("-" * len(header))

overall_filing_returns = []
overall_control_returns = []

for bucket in bucket_order:
    f_group = df_filings[df_filings["bucket"] == bucket]
    c_group = df_control[df_control["bucket"] == bucket]

    fs = compute_stats(f_group)
    cs = compute_stats(c_group)

    diff = fs["mean"] - cs["mean"] if not (np.isnan(fs["mean"]) or np.isnan(cs["mean"])) else np.nan

    print(
        f"{bucket:<18} | {fs['n']:>5} | {fs['mean']:>+7.2f} | {fs['median']:>+7.2f} | {fs['pct_5']:>5.1f} | {fs['pct_10']:>5.1f} "
        f"|| {cs['n']:>5} | {cs['mean']:>+7.2f} | {cs['median']:>+7.2f} | {cs['pct_5']:>5.1f} | {cs['pct_10']:>5.1f} "
        f"|| {diff:>+6.2f}"
    )

    overall_filing_returns.extend(f_group["forward_return_20d"].tolist())
    overall_control_returns.extend(c_group["forward_return_20d"].tolist())

# Overall row
print("-" * len(header))
f_all = {"forward_return_20d": pd.Series(overall_filing_returns)}
c_all = {"forward_return_20d": pd.Series(overall_control_returns)}
fs = compute_stats(pd.DataFrame(f_all))
cs = compute_stats(pd.DataFrame(c_all))
diff = fs["mean"] - cs["mean"]
print(
    f"{'ALL':<18} | {fs['n']:>5} | {fs['mean']:>+7.2f} | {fs['median']:>+7.2f} | {fs['pct_5']:>5.1f} | {fs['pct_10']:>5.1f} "
    f"|| {cs['n']:>5} | {cs['mean']:>+7.2f} | {cs['median']:>+7.2f} | {cs['pct_5']:>5.1f} | {cs['pct_10']:>5.1f} "
    f"|| {diff:>+6.2f}"
)

# Step 6: Statistical tests
print("\n" + "=" * 90)
print("STATISTICAL TESTS")
print("=" * 90)

filing_returns = np.array(overall_filing_returns)
control_returns = np.array(overall_control_returns)

# T-test
t_stat, t_pval = stats.ttest_ind(filing_returns, control_returns, equal_var=False)
print(f"\nWelch's t-test (filing vs control):")
print(f"  t-statistic: {t_stat:.4f}")
print(f"  p-value:     {t_pval:.6f}")
print(f"  Significant at 5%: {'YES' if t_pval < 0.05 else 'NO'}")

# Mann-Whitney U test
u_stat, u_pval = stats.mannwhitneyu(filing_returns, control_returns, alternative="two-sided")
print(f"\nMann-Whitney U test (non-parametric):")
print(f"  U-statistic: {u_stat:.0f}")
print(f"  p-value:     {u_pval:.6f}")
print(f"  Significant at 5%: {'YES' if u_pval < 0.05 else 'NO'}")

# Per-bucket tests
print(f"\nPer-bucket Mann-Whitney tests (filing vs control):")
print(f"{'Bucket':<18} | {'U-stat':>10} | {'p-value':>10} | {'Sig?':>5}")
print("-" * 55)

for bucket in bucket_order:
    f_ret = df_filings[df_filings["bucket"] == bucket]["forward_return_20d"].values
    c_ret = df_control[df_control["bucket"] == bucket]["forward_return_20d"].values
    if len(f_ret) < 5 or len(c_ret) < 5:
        print(f"{bucket:<18} | {'--':>10} | {'--':>10} | {'N/A':>5}  (n<5)")
        continue
    u, p = stats.mannwhitneyu(f_ret, c_ret, alternative="two-sided")
    sig = "YES" if p < 0.05 else "no"
    print(f"{bucket:<18} | {u:>10.0f} | {p:>10.6f} | {sig:>5}")

print("\n" + "=" * 90)
print("INTERPRETATION")
print("=" * 90)
print(f"  Filing group mean return:  {np.mean(filing_returns)*100:+.3f}%")
print(f"  Control group mean return: {np.mean(control_returns)*100:+.3f}%")
print(f"  Difference:                {(np.mean(filing_returns) - np.mean(control_returns))*100:+.3f}%")
print(f"  Total filing events:       {len(filing_returns)}")
print(f"  Total control samples:     {len(control_returns)}")
