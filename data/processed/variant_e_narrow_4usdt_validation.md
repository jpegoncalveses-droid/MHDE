# Variant E'' validation — entry-conditional shortened horizon for narrowed 4USDT-class

**Source:** `.claude/local_scripts/variant_e_narrow_4usdt_validation.py`
**Window:** 2025-04-05 -> 2026-05-07 (Phase-1B walk-fold window)
**Base config:** horizon=10d, Policy D, top_n=6, trail_pct=0.3, activation_pct=0.01, post-parabolic filter ON.
**Variant rule:** for entries matching
  `drawdown_from_90d_high < -0.40 AND return_10d > +0.10 AND return_60d < -0.20`
replace policy horizon_days=10 with horizon_days=N (N in {3, 5, 7}). Non-matching trades unchanged.
**Run mode:** dry_run=True (no DB writes). Monkey-patch on `H._build_position` is installed/uninstalled per run.

## ADR-032 gates

- **G1 Walkfold dominance:** variant beats baseline (Sharpe higher AND |max_dd_pct| smaller) in >= 4 of 6 folds.
- **G2 Portfolio gate:** portfolio Sharpe meaningfully above baseline AND portfolio max_dd_pct >= -25% (5pp safety margin under the deployed kill-switch at -30%, ADR-032 / KI-148).

## Cohort sizing (sanity check)

- Filter-matched (`crypto_ml_features` rows in window): **505** feature snapshots
- Filter-matched in baseline 941-trade selection: **46** trades
- Filter-matched cohort baseline performance:
  - mean net P&L: **+4.13%**, median +3.42%
  - hit rate: **87.0%**, avg winner +8.30%, avg loser -23.68%
  - mean days_held 3.72, pct_time_exits 13%, pct_trail_exits 87%

## Sensitivity probe — horizon ∈ {3, 5, 7}  (full window)

All metrics use the same prediction set; only filter-matched trades have their horizon overridden.

| run | n_trades | filter_matched | Sharpe | Max DD | Net P&L | hit_rate | PF | avg_w | avg_l |
|----|---------:|--------------:|------:|------:|--------:|---------:|---:|-----:|-----:|
| BASELINE (h=10) | 941 | 46 | 6.3223 | -16.98% | +5247.92% | 87.6% | 3.2018 | +9.26% | -20.37% |
| VARIANT (h=3) | 950 | 46 | 6.2744 | -16.88% | +5303.39% | 87.2% | 3.1949 | +9.32% | -19.81% |
| VARIANT (h=5) | 947 | 46 | 6.3293 | -17.28% | +5256.52% | 87.2% | 3.1995 | +9.26% | -19.75% |
| VARIANT (h=7) | 943 | 46 | 6.2962 | -19.00% | +5230.61% | 87.2% | 3.1824 | +9.28% | -19.81% |

**Δ vs BASELINE:**

| run | ΔSharpe | ΔMax DD (pp) | ΔNet P&L (pp) | Δhit_rate (pp) |
|----|--------:|------------:|--------------:|--------------:|
| VARIANT (h=3) | -0.0480 | +0.11 | +55.47 | -0.41 |
| VARIANT (h=5) | +0.0070 | -0.30 | +8.60 | -0.34 |
| VARIANT (h=7) | -0.0261 | -2.02 | -17.31 | -0.40 |

**Filter-matched-cohort metrics (variant horizon applied):**

| run | n_matched | mean_pnl | median | hit | avg_w | avg_l | mean_days |
|----|---------:|--------:|------:|----:|-----:|------:|---------:|
| BASELINE (h=10) | 46 | +4.13% | +3.42% | 87% | +8.30% | -23.68% | 3.7 |
| VARIANT (h=3) | 46 | +3.15% | +2.72% | 78% | +8.68% | -16.74% | 2.4 |
| VARIANT (h=5) | 46 | +3.40% | +3.15% | 80% | +8.04% | -15.66% | 2.9 |
| VARIANT (h=7) | 46 | +4.53% | +3.42% | 85% | +8.27% | -16.33% | 3.3 |

## Walkfold validation — horizon=5 (the central probe)

Per-fold baseline vs variant. Variant dominates if Sharpe higher AND |Max DD| smaller in that fold.

| fold | bl_n | v_n | bl_Sharpe | v_Sharpe | bl_DD | v_DD | bl_Net | v_Net | dominate? |
|------|----:|----:|----------:|---------:|------:|-----:|-------:|------:|:---------|
| F1_2025-04-05_2025-06-04 | 168 | 169 | 9.761 | 9.638 | -8.74% | -8.89% | +1033.94% | +1012.65% | worse |
| F2_2025-06-05_2025-08-04 | 150 | 150 | 5.515 | 5.515 | -64.92% | -64.92% | +458.49% | +458.49% | mixed |
| F3_2025-08-05_2025-10-04 | 142 | 143 | 4.807 | 4.755 | -19.81% | -19.66% | +631.59% | +632.77% | mixed |
| F4_2025-10-05_2025-12-04 | 151 | 152 | 5.622 | 6.065 | -23.63% | -23.63% | +632.69% | +655.85% | mixed |
| F5_2025-12-05_2026-02-04 | 149 | 149 | 3.316 | 3.200 | -39.40% | -39.69% | +535.61% | +527.91% | worse |
| F6_2026-02-05_2026-05-07 | 210 | 212 | 9.063 | 9.230 | -15.72% | -15.72% | +2311.89% | +2332.64% | mixed |

**Dominance count: 0 of 6 folds** (variant strictly better on both metrics)
**Strictly-worse count: 2 of 6 folds**
**Mixed: 4 of 6 folds**

## Both-gate decision

- **G1 walkfold dominance (≥4/6):** **FAIL** (0/6 folds)
- **G2a portfolio max_dd_pct ≥ -25%:** **PASS** (-17.28%)
- **G2b portfolio Sharpe gain > +0.10:** **FAIL** (Δ = +0.0070)

### Result: FAIL — both gates miss

Neither walkfold dominance nor portfolio gate clears. **KI-140 closes formally on this lens:** the narrowed 4USDT-class filter with entry-conditional shorter horizon does not produce a Sharpe-positive, DD-safe intervention.

This is consistent with ADR-028's broader Variant E rejection and the rescue-rate heatmap / Other-cohort findings that no entry-side or conditional-exit rule on the available features produces a Sharpe-positive filter for this regime.

**Recommendation:** stop probing exclusion / horizon-modifier rules. Redirect effort to:
1. **Probability haircut (Path E)** as proposed in KI-140 item 2 — multiplicative downscale on `predicted_probability` for matched entries, before Top-N selection; avoids the binary-filter Top-N backfill problem.
2. **Direction-aware label (KI-137)** as the long-term model fix that addresses all named loser classes at training time.

## Reproducibility

```
venv/bin/python .claude/local_scripts/variant_e_narrow_4usdt_validation.py
```

Reads `data/mhde.duckdb`. Pre-computes the filter set via SQL on `crypto_ml_features`, then re-runs `crypto.execution.backtest.harness` in dry_run mode for: full window with each of horizon ∈ {3, 5, 7}, and the 6 walkfold folds with horizon=5. Monkey-patch on `H._build_position` is installed/uninstalled per variant run; baseline runs use the unpatched function. No DB writes.
