"""Evaluation and reporting utilities for crypto ML models."""
from __future__ import annotations

import numpy as np


def print_walk_forward_results(results: list[dict], label_col: str, horizon: str):
    print(f"\n{'='*70}")
    print(f"CRYPTO WALK-FORWARD CV RESULTS — {horizon} / {label_col}")
    print(f"{'='*70}")

    valid = [r for r in results if "error" not in r]
    if not valid:
        print("No successful folds.")
        return

    print(f"\n{'Fold':>4} | {'N_train':>7} | {'N_test':>6} | {'Base%':>5} | {'Prec@Top':>8} | {'AUC':>5} | {'Lift':>5}")
    print("-" * 60)

    for r in valid:
        print(f"  {r['fold']:>2}  | {r['n_train']:>7,} | {r['n_test']:>6,} | "
              f"{r['base_rate']*100:>4.1f}% | {r['precision_top']*100:>7.1f}% | "
              f"{r['auc_roc']:>.3f} | {r['lift']:>4.2f}x")

    avg_auc = np.mean([r["auc_roc"] for r in valid])
    avg_lift = np.mean([r["lift"] for r in valid])
    avg_prec = np.mean([r["precision_top"] for r in valid])
    avg_base = np.mean([r["base_rate"] for r in valid])

    print("-" * 60)
    print(f"  AVG | {'':>7} | {'':>6} | {avg_base*100:>4.1f}% | {avg_prec*100:>7.1f}% | {avg_auc:>.3f} | {avg_lift:>4.2f}x")
    print(f"\nSuccess criterion: Lift > 1.3x consistently. {'PASS' if avg_lift > 1.3 else 'NEEDS REVIEW'}")

    if valid and "feature_importance" in valid[-1]:
        imp = valid[-1]["feature_importance"]
        top = sorted(imp.items(), key=lambda x: x[1], reverse=True)[:10]
        print(f"\nTop 10 features (last fold):")
        for name, val in top:
            print(f"  {name:<35} {val:.4f}")
