# Phase 1A: Walk-Forward Prediction Backfill

## Purpose

Generate legitimate walk-forward out-of-sample predictions across the full historical window so Phase 1B (execution backtest) can produce valid policy comparisons. The existing walk-forward CV in `crypto/ml/train.py` already produces these predictions internally per fold but discards them after aggregate metric computation. This phase captures and persists them.

## Isolation Guarantees

Phase 1A will not affect:

- **The active live crypto model.** Existing joblib artifacts remain untouched. New `is_active=false` model rows are added; existing actives are not flipped.
- **The daily prediction pipeline.** `predict.py` filters on `is_active=true`. Backfill model_ids will be ignored by daily predict.
- **Equities, FX, or shared `ml/` modules.** Only `crypto/ml/` files are modified.
- **The dashboard or any monitoring layer.**
- **Crypto Phase 0 calibration validation.** Live predictions and metrics continue unaffected.

## Scope

**Horizons in scope:** 5d and 10d (matching the existing `retrain.py` config).

**Horizons deferred:** 20d. If Phase 1B's winner is at the 10d boundary, 20d can be added as a separate task.

**Universe:** existing 50-coin crypto universe. No changes.

## The Fix

**Current behavior (train.py:108):** each fold computes calibrated `test_probs` (OOS probabilities), uses them to compute precision/AUC/lift, then discards.

**New behavior:** capture each fold's `(symbol, prediction_date, predicted_probability)` rows and persist them to `crypto_ml_predictions` tagged with a fold-specific `model_id`.

Concretely:

1. Modify the per-fold logic in `train.py` to return both metrics AND the full OOS prediction DataFrame.
2. Add a persister function that, given fold predictions + fold metadata, writes:
   - One row to `crypto_ml_model_runs` (`is_active=false`, with `train_start` and `train_end` matching the fold's training window)
   - All prediction rows to `crypto_ml_predictions`, tagged with the fold's model_id
   - Outcomes (`actual_max_return`, `actual_max_drawdown`, `actual_hit`, `outcome_filled_at`) populated at insert time using historical prices from `crypto_prices_daily`
3. Wrap fold persistence in a transaction. Rollback on any insert failure.
4. Bypass `predict.py`'s LOW_THRESHOLD filter for backfill writes. Full universe must be persisted so Phase 1B's `selection.py` can apply its own filters.

## Walk-Forward Configuration

Use existing config from `train.py`. No changes:

- **Train window:** expanding, starting 2024-01-01 (`TRAIN_START`)
- **First test fold:** train_start + 6 months
- **Test window:** 1 calendar month
- **Cadence:** monthly slide
- **Expected fold count:** ~19 per horizon (as of today)

## Storage Strategy

**Destination table:** existing `crypto_ml_predictions`. Phase 1B spec already references this table read-only.

**Model ID naming convention:** `crypto_{horizon}_walkfold_{YYYY_MM}`

Examples:
- `crypto_5d_walkfold_2024_10`
- `crypto_10d_walkfold_2025_03`

This naming makes provenance grep-able and ensures no PK collision with the two existing live model_ids (`crypto_5d_ab428f75`, `crypto_10d_db171418`).

**`crypto_ml_model_runs` rows for backfill:**

| Field | Value |
|---|---|
| model_id | `crypto_{horizon}_walkfold_{YYYY_MM}` |
| is_active | false |
| train_start | fold's training window start |
| train_end | fold's training window end |
| horizon | 5d or 10d |
| label_col | matches existing config |

## Output Volume Estimate

- ~50 coins × ~22 trading days/month × 19 folds × 2 horizons ≈ **41,800 prediction rows**
- ~38 backfill `model_runs` entries
- Single backfill run: ~6-10 minutes (per inspection estimate)

## CLI Entry Point

New command: `python main.py crypto backfill-walkforward-predictions [--horizons 5d,10d] [--dry-run]`

- `--horizons`: comma-separated, defaults to all configured horizons
- `--dry-run`: runs walk-forward, logs row counts and validation summary, writes nothing to DB

## Implementation Tasks

1. Refactor per-fold logic in `crypto/ml/train.py` to capture and return OOS predictions alongside metrics. Minimal change: surface what's already computed.
2. Create `crypto/ml/backfill_walkforward.py` containing:
   - Fold orchestrator (calls existing walk-forward, captures predictions per fold)
   - Persister (transactional writes to `crypto_ml_model_runs` and `crypto_ml_predictions`)
   - Outcome computer (uses `crypto_prices_daily` to fill `actual_*` columns)
   - Validation runner (post-backfill checks, see below)
3. Add CLI sub-command in `main.py`.
4. Tests covering: persister transaction rollback, outcome computation correctness, model_id format, no-collision with existing actives.

## Validation (must pass before Phase 1B resumes)

After backfill completes, run validation queries:

1. **No leakage:** for every backfill prediction, confirm `prediction_date > model_run.train_end`. Zero violations allowed.
2. **Coverage:** total backfill prediction rows ≈ expected (~41k). Allow ±10% for fold edge effects.
3. **Outcomes filled:** `actual_hit IS NOT NULL` for all rows where `prediction_date + horizon ≤ today`.
4. **Distinct model_ids:** each fold has its own model_id. No PK collisions.
5. **is_active integrity:** all backfill model_runs have `is_active=false`. The two existing actives remain `is_active=true`.
6. **Live pipeline unaffected:** running daily predict after backfill produces the same output as before backfill (same active models picked).

Validation queries should be re-runnable for spot checks.

## Decision Gate (proceed to Phase 1B)

Phase 1A complete when:

- All folds successfully written for both horizons
- All 6 validation queries pass
- ~40k+ rows queryable from `crypto_ml_predictions WHERE model_id LIKE 'crypto_%_walkfold_%'`
- Live daily predict still functioning normally

## What Does NOT Change

Per inspection findings:

- Feature pipeline (`crypto/ml/features.py`)
- Label computation (`crypto/ml/labels.py`)
- Model class (`XGBClassifier` + Platt)
- Model hyperparameters (`DEFAULT_PARAMS`)
- Joblib artifact format
- Daily prediction CLI
- Dashboard
- Equity / FX engines

## Out of Scope for Phase 1A

- Adding 20d horizon (deferred)
- Refactoring beyond minimum needed for prediction persistence
- Visualization or dashboard updates
- Modifying live prediction pipeline beyond bypass of LOW_THRESHOLD for backfill writes
- Per-fold trained model artifacts (joblib files): not needed since only predictions are required
