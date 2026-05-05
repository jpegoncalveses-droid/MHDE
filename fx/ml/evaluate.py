"""Evaluation and reporting for FX models."""
from __future__ import annotations

import numpy as np


def print_training_results(all_results: dict):
    print(f"\n{'='*70}")
    print("FX WALK-FORWARD CV RESULTS")
    print(f"{'='*70}")

    for model_key, results in all_results.items():
        valid = [r for r in results if "error" not in r]
        if not valid:
            print(f"\n{model_key}: NO SUCCESSFUL FOLDS")
            continue

        print(f"\n--- {model_key} ---")
        print(f"  {'Year':>5} | {'N_train':>7} | {'N_test':>6} | {'Base%':>5} | {'Prec@10':>7} | {'AUC':>5} | {'Lift':>5}")
        print(f"  {'-'*55}")

        for r in valid:
            print(f"  {r['test_year']:>5} | {r['n_train']:>7,} | {r['n_test']:>6,} | "
                  f"{r['base_rate']*100:>4.1f}% | {r['precision_top10']*100:>6.1f}% | "
                  f"{r['auc_roc']:>.3f} | {r['lift']:>4.2f}x")

        avg_auc = np.mean([r["auc_roc"] for r in valid])
        avg_lift = np.mean([r["lift"] for r in valid])
        print(f"  AVG: AUC={avg_auc:.3f}  Lift={avg_lift:.2f}x  "
              f"{'PASS' if avg_lift > 1.3 else 'MARGINAL' if avg_lift > 1.1 else 'FAIL'}")

    print(f"\n{'='*70}")
