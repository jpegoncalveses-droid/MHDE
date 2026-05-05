"""Run Step 3: Walk-forward training and evaluation for all horizons."""
import sys
import logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import duckdb
from ml.train import train_walk_forward
from ml.evaluate import print_walk_forward_results

con = duckdb.connect("data/mhde.duckdb")

print("=" * 90)
print("STEP 3: WALK-FORWARD TRAINING AND VALIDATION")
print("=" * 90)

# Primary target
configs = [
    ("label_20d_5pct", "20d", 0.05),
    ("label_10d_5pct", "10d", 0.05),
    ("label_5d_3pct", "5d", 0.03),
]

all_results = {}

for label_col, horizon, threshold in configs:
    print(f"\n{'='*90}")
    print(f"Training: {label_col} (horizon={horizon}, threshold={threshold})")
    print(f"{'='*90}")

    results = train_walk_forward(con, label_col=label_col, horizon=horizon, threshold=threshold)
    all_results[label_col] = results
    print_walk_forward_results(results, label_col, horizon)

# Final comparison
print(f"\n\n{'='*90}")
print("COMPARISON ACROSS HORIZONS")
print(f"{'='*90}")
print(f"\n{'Target':<20} | {'Avg Lift':>8} | {'Avg AUC':>7} | {'Avg Prec':>8} | {'Consistent?'}")
print("-" * 65)

for label_col, horizon, threshold in configs:
    results = all_results[label_col]
    fold_results = [r for r in results if "fold" in r]
    if fold_results:
        avg_lift = sum(r["lift_over_base"] for r in fold_results) / len(fold_results)
        avg_auc = sum(r["auc_roc"] for r in fold_results) / len(fold_results)
        avg_prec = sum(r["precision_at_threshold"] for r in fold_results) / len(fold_results)
        min_lift = min(r["lift_over_base"] for r in fold_results)
        consistent = "YES" if min_lift > 1.1 else "PARTIAL" if avg_lift > 1.3 else "NO"
        print(f"{label_col:<20} | {avg_lift:>8.2f} | {avg_auc:>7.3f} | {avg_prec:>7.1f}% | {consistent}")

con.close()
