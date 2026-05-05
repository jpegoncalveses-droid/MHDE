"""Run Step 1: Compute ML labels and print verification stats."""
import sys
import logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import duckdb
from ml.labels import compute_labels

con = duckdb.connect("data/mhde.duckdb")

print("=" * 70)
print("STEP 1: COMPUTING ML LABELS")
print("=" * 70)

total = compute_labels(con)

print(f"\nTotal rows inserted: {total:,}")

# Verification stats
print("\n" + "=" * 70)
print("VERIFICATION: Label summary stats")
print("=" * 70)

# Overall counts
row = con.execute("""
    SELECT
        COUNT(*) AS total,
        COUNT(fwd_return_5d) AS has_5d,
        COUNT(fwd_return_10d) AS has_10d,
        COUNT(fwd_return_20d) AS has_20d
    FROM ml_labels
""").fetchone()
print(f"\n  Total rows:          {row[0]:,}")
print(f"  With 5d returns:     {row[1]:,}")
print(f"  With 10d returns:    {row[2]:,}")
print(f"  With 20d returns:    {row[3]:,}")

# Positive rates per label
print("\n  Label positive rates (where label is not NULL):")
labels = [
    ("label_5d_3pct", "5d >= 3%"),
    ("label_5d_5pct", "5d >= 5%"),
    ("label_10d_5pct", "10d >= 5%"),
    ("label_10d_8pct", "10d >= 8%"),
    ("label_20d_5pct", "20d >= 5%"),
    ("label_20d_8pct", "20d >= 8%"),
    ("label_20d_10pct", "20d >= 10%"),
    ("label_20d_15pct", "20d >= 15%"),
]

print(f"  {'Label':<16} | {'Positive':>8} | {'Total':>8} | {'Rate':>6}")
print(f"  {'-'*50}")
for col, desc in labels:
    r = con.execute(f"""
        SELECT SUM(CASE WHEN {col} THEN 1 ELSE 0 END), COUNT(*)
        FROM ml_labels WHERE {col} IS NOT NULL
    """).fetchone()
    rate = r[0] / r[1] * 100 if r[1] > 0 else 0
    print(f"  {desc:<16} | {r[0]:>8,} | {r[1]:>8,} | {rate:>5.1f}%")

# Monthly breakdown for primary target (20d 5%)
print("\n  Monthly positive rate for label_20d_5pct:")
print(f"  {'Month':<10} | {'Positive':>8} | {'Total':>8} | {'Rate':>6}")
print(f"  {'-'*45}")
rows = con.execute("""
    SELECT
        STRFTIME(trade_date, '%Y-%m') AS month,
        SUM(CASE WHEN label_20d_5pct THEN 1 ELSE 0 END) AS pos,
        COUNT(*) AS total
    FROM ml_labels
    WHERE label_20d_5pct IS NOT NULL
    GROUP BY month
    ORDER BY month
""").fetchall()
for r in rows:
    rate = r[1] / r[2] * 100 if r[2] > 0 else 0
    print(f"  {r[0]:<10} | {r[1]:>8,} | {r[2]:>8,} | {rate:>5.1f}%")

# Return distribution
print("\n  Forward return distributions (20d):")
r = con.execute("""
    SELECT
        AVG(fwd_return_20d) * 100,
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY fwd_return_20d) * 100,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY fwd_return_20d) * 100,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY fwd_return_20d) * 100,
        AVG(fwd_max_return_20d) * 100,
        AVG(fwd_max_drawdown_20d) * 100
    FROM ml_labels WHERE fwd_return_20d IS NOT NULL
""").fetchone()
print(f"    Mean return:       {r[0]:+.2f}%")
print(f"    25th percentile:   {r[1]:+.2f}%")
print(f"    Median:            {r[2]:+.2f}%")
print(f"    75th percentile:   {r[3]:+.2f}%")
print(f"    Mean max return:   {r[4]:+.2f}%")
print(f"    Mean max drawdown: {r[5]:+.2f}%")

# Ticker coverage
r = con.execute("SELECT COUNT(DISTINCT ticker) FROM ml_labels").fetchone()
print(f"\n  Distinct tickers: {r[0]}")

con.close()
