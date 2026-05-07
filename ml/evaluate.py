"""Evaluation utilities for ML model results."""
from __future__ import annotations

import numpy as np


def print_walk_forward_results(results: list[dict], label_col: str, horizon: str):
    """Print formatted walk-forward CV results."""
    fold_results = [r for r in results if "fold" in r]
    final = next((r["final_model"] for r in results if "final_model" in r), None)

    print(f"\n{'='*90}")
    print(f"WALK-FORWARD RESULTS: {label_col} (horizon={horizon})")
    print(f"{'='*90}")

    print(f"\n{'Fold':<6} | {'Test Period':<23} | {'Base%':>5} | {'Prec@20':>7} | {'Prec@T':>7} | "
          f"{'Recall':>6} | {'Lift':>5} | {'AUC':>5} | {'Thresh':>6} | {'N flag':>6}")
    print("-" * 105)

    for r in fold_results:
        print(f"  {r['fold']:<4} | {r['test_start']} -> {r['test_end']} | "
              f"{r['base_rate']*100:>4.1f}% | {r['precision_top_20']:>7.3f} | "
              f"{r['precision_at_threshold']*100:>6.1f}% | {r['recall_at_threshold']*100:>5.1f}% | "
              f"{r['lift_over_base']:>5.2f} | {r['auc_roc']:>5.3f} | "
              f"{r['optimal_threshold']:>6.2f} | {r['n_flagged']:>6}")

    # Averages
    if fold_results:
        print("-" * 105)
        avg_base = np.mean([r["base_rate"] for r in fold_results])
        avg_p20 = np.mean([r["precision_top_20"] for r in fold_results])
        avg_prec = np.mean([r["precision_at_threshold"] for r in fold_results])
        avg_rec = np.mean([r["recall_at_threshold"] for r in fold_results])
        avg_lift = np.mean([r["lift_over_base"] for r in fold_results])
        avg_auc = np.mean([r["auc_roc"] for r in fold_results])
        print(f"  {'AVG':<4} | {'':23} | {avg_base*100:>4.1f}% | {avg_p20:>7.3f} | "
              f"{avg_prec*100:>6.1f}% | {avg_rec*100:>5.1f}% | {avg_lift:>5.2f} | {avg_auc:>5.3f} |")

    # Success criteria check (skipped when no folds completed)
    if fold_results:
        print(f"\n  SUCCESS CRITERIA:")
        print(f"    Lift > 1.3 consistently: ", end="")
        lifts = [r["lift_over_base"] for r in fold_results]
        if all(l > 1.3 for l in lifts):
            print(f"PASS (min={min(lifts):.2f})")
        elif np.mean(lifts) > 1.3:
            print(f"PARTIAL (avg={np.mean(lifts):.2f}, min={min(lifts):.2f})")
        else:
            print(f"FAIL (avg={np.mean(lifts):.2f})")

        print(f"    AUC > 0.55:              ", end="")
        aucs = [r["auc_roc"] for r in fold_results]
        if all(a > 0.55 for a in aucs):
            print(f"PASS (min={min(aucs):.3f})")
        else:
            print(f"{'PASS' if np.mean(aucs) > 0.55 else 'FAIL'} (avg={np.mean(aucs):.3f})")

    # Feature importance
    if final and "feature_importance" in final:
        print(f"\n  TOP 10 FEATURES (gain-based importance):")
        imp = final["feature_importance"]
        sorted_imp = sorted(imp.items(), key=lambda x: x[1], reverse=True)
        total_imp = sum(v for _, v in sorted_imp)
        for feat, val in sorted_imp[:10]:
            pct = val / total_imp * 100 if total_imp > 0 else 0
            bar = "#" * int(pct / 2)
            print(f"    {feat:<25} {pct:>5.1f}%  {bar}")

    if final:
        print(f"\n  FINAL MODEL: {final['model_id']}")
        print(f"    Path:          {final['model_path']}")
        print(f"    Avg precision: {final['avg_precision']*100:.1f}%")
        print(f"    Avg recall:    {final['avg_recall']*100:.1f}%")
        print(f"    Avg AUC:       {final['avg_auc']:.3f}")
        print(f"    Avg lift:      {final['avg_lift']:.2f}x")
