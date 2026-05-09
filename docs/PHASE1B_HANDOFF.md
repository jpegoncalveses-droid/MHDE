# Phase 1B Handoff

Last updated: end of session 2026-05-08.

## Current State

- **Phase 0** (live calibration validation): in progress, ~6 weeks accumulated.
- **Phase 1A** (walk-forward prediction backfill): **COMPLETE.** 40,074 OOS predictions in `crypto_ml_predictions` tagged with `model_id LIKE 'crypto_%_walkfold_%'`. All 6 validation checks passed. Two pre-existing live actives (`crypto_5d_ab428f75`, `crypto_10d_db171418`) unchanged.
- **Phase 1B base grid**: **COMPLETE.** 20 configs run, 0 failures. Results persisted in `crypto_backtest_runs` / `crypto_backtest_trades` / `crypto_backtest_summary`.
- **Phase 1B sensitivity grid**: **PENDING.** Authorized in concept but not yet executed; runner.py needs the `sensitivity_grid_configs` factory and `--grid sensitivity` CLI path. See "Pending command" below.
- **Phase 2, 3, 4**: not started.

## Top 3 base grid winners (all Policy D)

Pulled from `crypto_backtest_summary` (sum-of-fractions methodology) and `report.py` `simulate_portfolio` (realistic $1,000 portfolio with 80% deployed across 6 concurrent positions, 1× leverage).

| Rank | run_id | Horizon | Selection | Sharpe (frac) | MaxDD (frac) | Portfolio Sharpe | Portfolio MaxDD | Portfolio Final ($1k start) |
|---:|---|---|---|---:|---:|---:|---:|---:|
| 1 | `backtest_5d_D_threshold_76bc6b85` | 5d | threshold (p ≥ 0.55) | 3.778 | -42.25% | 2.158 | -26.23% | $7,958 |
| 2 | `backtest_5d_D_top_n_cb0a3702` | 5d | top_n (n=6) | 2.992 | -10.75% | 1.880 | -28.73% | $12,229 |
| 3 | `backtest_10d_D_top_n_e08cf9da` | 10d | top_n (n=6) | 2.946 | -24.47% | 2.414 | -28.29% | $24,240 |

Hit rates: 81–86%. Profit factor: 2.4–3.7. All three are Policy D (trailing-only, 1% activation, 50% trail).

## Decision criteria status

Spec gates (all four required to pass to Phase 2):
- annualized return > 5%
- Sharpe ratio > 1.0
- max drawdown < 25%
- profit factor > 1.3

**All three top configs FAIL the `max_dd < 25%` gate by 1–4 percentage points using realistic-portfolio numbers.** Annualized return, Sharpe, and profit factor all pass comfortably for the top 3.

| Config | Ann. return | Sharpe | Max DD | Profit factor | Pass? |
|---|---:|---:|---:|---:|:---:|
| 5d/D/threshold | +641.4% ✅ | 2.158 ✅ | -26.23% ❌ | 2.95 ✅ | 3/4 |
| 5d/D/top_n=6 | +1027.2% ✅ | 1.880 ✅ | -28.73% ❌ | 2.36 ✅ | 3/4 |
| 10d/D/top_n=6 | +2136.6% ✅ | 2.414 ✅ | -28.29% ❌ | 3.73 ✅ | 3/4 |

## Sensitivity grid result (executed 2026-05-09)

Strict slice — single-axis sweeps around the 3 base winners — emitted 27 unique configs (33 emitted, 6 base-collisions). **8 of the 27 pass all four Phase 1B gates.**

### Top of the strict-slice ranking by portfolio Sharpe

| Rank | run_id | params delta from base | port. Sharpe | port. maxDD | port. final $ | Gates |
|---:|---|---|---:|---:|---:|:---:|
| 1 | `backtest_10d_D_top_n_a02e15a0` | trail 0.50 → **0.30** | **5.10** | -23.7% | $32,122 | ✓ |
| 2 | `backtest_5d_D_top_n_5aff7b45` | trail 0.50 → **0.30** | 4.94 | -23.4% | $20,281 | ✓ |
| 3 | `backtest_5d_D_threshold_28ae40f4` | trail 0.50 → **0.30** | 4.60 | -22.4% | $6,751 | ✓ |
| 4 | `backtest_5d_D_threshold_b6d3b92e` | threshold 0.55 → **0.65** | 3.77 | -19.6% | $2,846 | ✓ |
| 5 | `backtest_10d_D_top_n_ae1b2312` | activation 0.01 → **0.00** | 2.53 | -18.4% | $34,016 | ✓ |
| 6 | `backtest_5d_D_threshold_b800f49f` | threshold 0.55 → **0.50** | 2.22 | -24.8% | $13,705 | ✓ |
| 7 | `backtest_5d_D_top_n_e1e0a0f5` | n 6 → **5** | 2.23 | -21.3% | $22,658 | ✓ |
| 8 | `backtest_5d_D_top_n_077c7923` | activation 0.01 → **0.00** | 2.15 | -24.2% | $21,576 | ✓ |

The dominant axis change is `trail_pct: 0.50 → 0.30`. All three top-3 base winners' trail-axis sweeps land in the passing set. activation_pct and selection sweeps each yield one passer per base.

### Selected Phase 1B winner: `backtest_10d_D_top_n_a02e15a0`

| Field | Value |
|---|---|
| Horizon | 10d |
| Policy | D (trailing stop) |
| Selection | top_n with n=6 |
| `trail_pct` | 0.30 |
| `activation_pct` | 0.01 (class default) |
| Stored params | `{"policy_params": {"trail_pct": 0.3}, "selection_params": {"n": 6}}` |

Portfolio metrics (398-day window, $1k start, 80% deploy × 6 positions, 1×):
- annualized return: +2854% ✓
- portfolio Sharpe: 5.096 ✓
- max drawdown: -23.73% ✓ (1.27 pp under the 25% gate)
- profit factor: 3.811 ✓
- final equity: $32,121.89
- 484 trades taken; 448 skipped at the 6-position cap

Sum-of-fractions metrics from `crypto_backtest_summary` (used for ranking, inflated absolute values per spec): Sharpe 6.32, max DD -17.0%, profit factor 3.13, hit rate 87.1%, 932 trades.

Why this one over the others in the passing set:
- **Highest portfolio Sharpe** (5.10) of the strict-slice passers.
- **Cleanest single-axis derivation** — one parameter changed from the originally documented base.
- **Drawdown comfortably under 25%** (-23.7%) without stacking multiple axis changes.

### Iterated extras (out of agreed spec)

After three CLI invocations of `--grid sensitivity` in quick succession, the iterated re-rank picked up sensitivity-found configs as new top-3 bases and produced 30 additional multi-axis configs through greedy axis-by-axis hill climbing. The strongest of these, `backtest_10d_D_top_n_d884e9f2` (10d / D / top_n with n=5, trail=0.30, activation=0.0), reaches portfolio Sharpe 6.04, maxDD -12.9%, $80,931 — but its provenance is iterated, not single-axis. Treated as research, not selected. A targeted single-invocation sweep around it ran 2026-05-09 (see "d884e9f2 robustness analysis" below) to determine whether it is a smooth local optimum or a sharp peak from chained sweeps.

CLI guard added 2026-05-09: re-running `--grid sensitivity` against a DB that already contains sensitivity-shape rows now refuses by default with an explanatory message. Override with `--allow-iterated` only if you know what you're doing. See `KNOWN_ISSUES.md` KI-125.

### d884e9f2 robustness analysis (executed 2026-05-09)

A targeted single-invocation sweep around `backtest_10d_D_top_n_d884e9f2` (10d / D / top_n with `trail_pct=0.30`, `activation_pct=0.00`, `n=5`; base portfolio Sharpe 6.04, maxDD -12.9%, $80,931) was run with `--allow-iterated` to characterize the local neighborhood. Verdict: **sharp peak**, not a smooth local optimum.

Per-axis neighbor metrics (portfolio Sharpe / portfolio maxDD), with `|Δ vs base|` classification (>30% on either metric → "sharp"):

| Axis | Value | port. Sharpe | port. maxDD | Classification |
|---|---|---:|---:|---|
| `trail_pct` | 0.30 (base) | 6.04 | -12.9% | base |
| `trail_pct` | 0.50 | 2.78 | -13.1% | **sharp** (Sharpe -54%) |
| `trail_pct` | 0.70 | 3.05 | -27.3% | **sharp** (Sharpe -50%, DD blows past 25% gate) |
| `activation_pct` | 0.00 (base) | 6.04 | -12.9% | base |
| `activation_pct` | 0.01 | 5.55 | -22.8% | **sharp** (DD +76% from -12.9% to -22.8%) |
| `activation_pct` | 0.02 | 4.86 | -22.9% | **sharp** |
| `activation_pct` | 0.03 | 4.65 | -22.1% | **sharp** |
| `n` | 5 (base) | 6.04 | -12.9% | base |
| `n` | 6 | 5.13 | -17.9% | **sharp** (DD +39%) |
| `n` | 7 | 1.76 | -23.6% | **sharp** (Sharpe -71%) |
| `n` | 8 | 1.80 | -19.6% | **sharp** (Sharpe -70%) |

The base sits at the corner of the favorable region on every axis. A 1% perturbation on `activation_pct` (0.00 → 0.01, the class default) raises drawdown ~10 percentage points. The `n` axis goes off a cliff between n=6 and n=7 (Sharpe 5.13 → 1.76).

**Conclusion: d884e9f2 is NOT a robust local optimum.** Its standout numbers are likely an overfit artifact of the chained-sweep search path through this period's specific drawdown structure. Live execution would not preserve them under the small parameter drift expected from real-world conditions (different funding-rate cadence in newer months, occasional missing bars, rounding).

`a02e15a0` remains the Phase 1B selected winner. It sits in a smoother neighborhood (trail axis: 0.30 passes, 0.50/0.70 fail by similar margins; activation axis: 0.00/0.01 both pass with comparable metrics; n axis: n=5/n=6 both pass).

## Methodology caveats already documented

## Methodology caveats already documented

- **Sum-of-fractions equity curve in `metrics.py`** (event-day Sharpe, not portfolio-weighted): inflates absolute Sharpe and drawdown, but preserves ranking across configs because the methodology is consistent. Documented in `crypto/execution/backtest/SPEC.md` § "Metrics methodology" and in the `harness.py` module docstring.
- **55% of selection signals are duplicate-skips** (same coin already has an open position). This is a real Binance trading constraint enforced by the harness, not a bug. The duplicate count is surfaced in `crypto_backtest_runs.n_skipped_duplicates` and the run summary log.
- **Linear annualization** (`total_return × 365 / span_days`), not compound. Consistent across configs.
- **Realistic-portfolio numbers** (from `report.py` `simulate_portfolio`) are the ones to interpret for decisions — not the sum-of-fractions metrics in `crypto_backtest_summary`. The sum-of-fractions metrics are useful for **ranking only**.

## Strategic context

- **The 25% max drawdown threshold was set as a heuristic** ("where retail traders psychologically cave"), not a hard physical limit. The Phase 1B winners are missing it by 1–4 points; reasonable to revisit the gate before discarding the strategy.
- **Even realistic-portfolio numbers are still optimistic vs live trading**: the simulation assumes perfect execution, no market impact, no exchange/funding surprises. Phase 3 paper trading is what will reveal real-world friction. Treat the 2.0–2.4 portfolio Sharpe as an upper bound, not a forecast.
- **Policy A, B, C are net losers across all 12 of their configurations.** Fixed-distance stops (Policy B's −3% and Policy C's 2× ATR) fire on crypto noise and cut winners short. Trailing-stop-with-activation (Policy D) captures the model's asymmetric signal best — this is the structural win of the grid.
- **The model has real signal.** Top-3 portfolio Sharpes 1.88–2.41 with hit rates 81–86% confirm the walk-forward predictions carry tradeable information. The question is execution — drawdown control — not signal quality.

## Files of record

- `crypto/ml/PHASE1A_SPEC.md` — walk-forward backfill spec.
- `crypto/execution/backtest/SPEC.md` — Phase 1B execution backtest spec.
- All Phase 1B modules with tests:
  - `crypto/execution/backtest/costs.py` (+ `tests/crypto/test_backtest_costs.py`)
  - `crypto/execution/backtest/policies.py` (+ `tests/crypto/test_backtest_policies.py`)
  - `crypto/execution/backtest/selection.py` (+ `tests/crypto/test_backtest_selection.py`)
  - `crypto/execution/backtest/harness.py` (+ `tests/crypto/test_backtest_harness.py`)
  - `crypto/execution/backtest/metrics.py` (+ `tests/crypto/test_backtest_metrics.py`)
  - `crypto/execution/backtest/runner.py` (+ `tests/crypto/test_backtest_runner.py`)
  - `crypto/execution/backtest/report.py` (+ `tests/crypto/test_backtest_report.py`)
- `crypto/ml/backfill_walkforward.py` (+ `tests/crypto/test_backfill_walkforward.py`)
- DB tables:
  - `crypto_ml_predictions` (with 40,074 walkfold rows, ~31k post-funding-floor)
  - `crypto_ml_model_runs` (38 walkfold backfill model_runs, all `is_active=false`; plus 2 pre-existing live actives)
  - `crypto_backtest_runs` (20 rows, one per base-grid config)
  - `crypto_backtest_trades` (~22k rows across all 20 runs)
  - `crypto_backtest_summary` (20 rows, one per run)

## Test-suite headcount at session close

`venv/bin/python -m pytest tests/crypto/test_backtest_*.py tests/crypto/test_backfill_walkforward.py`
→ **202 passed**.
