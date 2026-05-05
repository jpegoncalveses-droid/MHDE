"""Run Step 2: Compute ML features and print verification stats."""
import sys
import logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import duckdb
from ml.features import compute_features

con = duckdb.connect("data/mhde.duckdb")

print("=" * 70)
print("STEP 2: COMPUTING ML FEATURES")
print("=" * 70)

total = compute_features(con)

print(f"\nTotal rows: {total:,}")

# Verification
print("\n" + "=" * 70)
print("VERIFICATION: Feature coverage stats")
print("=" * 70)

# Non-null rates per feature
cols = con.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_name = 'ml_features' AND column_name NOT IN ('ticker', 'trade_date')
    ORDER BY ordinal_position
""").fetchall()

total_rows = con.execute("SELECT COUNT(*) FROM ml_features").fetchone()[0]
print(f"\n  Total rows: {total_rows:,}")
print(f"\n  {'Feature':<25} | {'Non-NULL':>8} | {'%':>6} | {'Mean':>10} | {'Std':>10}")
print(f"  {'-'*75}")

for (col,) in cols:
    r = con.execute(f"""
        SELECT COUNT({col}), AVG({col}::DOUBLE), STDDEV({col}::DOUBLE)
        FROM ml_features
    """).fetchone()
    non_null = r[0]
    pct = non_null / total_rows * 100 if total_rows > 0 else 0
    mean_val = f"{r[1]:.4f}" if r[1] is not None else "n/a"
    std_val = f"{r[2]:.4f}" if r[2] is not None else "n/a"
    print(f"  {col:<25} | {non_null:>8,} | {pct:>5.1f}% | {mean_val:>10} | {std_val:>10}")

# Spot-check a few tickers
print("\n  Spot check (AAPL, 2025-12-15):")
row = con.execute("""
    SELECT return_20d, realized_vol_20d, rsi_14d, return_vs_spy_20d, beta_60d,
           vix_level, filing_8k_count_30d, market_cap_log
    FROM ml_features
    WHERE ticker = 'AAPL' AND trade_date = '2025-12-15'
""").fetchone()
if row:
    labels = ["return_20d", "real_vol_20d", "rsi_14d", "vs_spy_20d", "beta_60d",
              "vix_level", "8k_30d", "mktcap_log"]
    for lbl, val in zip(labels, row):
        print(f"    {lbl:<14}: {val}")
else:
    print("    (no data for AAPL on 2025-12-15)")

con.close()
