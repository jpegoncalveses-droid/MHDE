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

## Pending decisions

1. **Run sensitivity grid on top 3** to test whether parameter perturbation pushes drawdown below 25% while preserving Sharpe.
2. **If sensitivity can't push DD below 25%**: decide whether to
   - relax the 25% threshold (which was set as a heuristic, not a hard rule) given otherwise-excellent metrics, or
   - pursue methodological changes (regime filter on BTC trend; smaller `deploy_fraction`; larger `max_positions` cap; per-coin position caps; volatility-targeted sizing).

## Pending command (sensitivity grid)

Build out the sensitivity infrastructure first:

1. Add `sensitivity_grid_configs(top_run_ids: list[str]) -> list[GridConfig]` in `crypto/execution/backtest/runner.py`. For each of the top 3 base winners, sweep:
   - `trail_pct` ∈ {0.30, 0.50, 0.70} — 3 variants
   - `activation_pct` ∈ {0.00, 0.01, 0.02, 0.03} — 4 variants
   - selection param: `n` ∈ {5, 6, 7, 8} for top_n configs **or** `threshold` ∈ {0.50, 0.55, 0.60, 0.65} for threshold configs — 4 variants
   - Total per top winner: 11 configs (3 + 4 + 4, since baselines naturally re-skipped via `skip_existing`).
   - **33 configs total** for the top 3 (some overlap with the base grid will be auto-skipped).
2. Update CLI: `--grid sensitivity` reads the top 3 from `crypto_backtest_summary ORDER BY sharpe_ratio DESC LIMIT 3` and dispatches to the sensitivity factory.
3. Add tests in `tests/crypto/test_backtest_runner.py`: factory determinism, axis-sweep coverage per top winner, skip-existing on overlap with base.

Once the above is in place and tests pass, run:

```bash
venv/bin/python main.py crypto backtest-grid --grid sensitivity
venv/bin/python main.py crypto backtest-report --top-n 5
```

Then generate per-axis sensitivity tables for each top winner — likely a `report.generate_sensitivity_table(conn, base_run_id, axis="trail_pct")` helper that joins runs/summary on shared (horizon, policy, selection) and varies one axis at a time.

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
