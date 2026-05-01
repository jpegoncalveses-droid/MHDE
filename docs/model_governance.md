# MHDE Model Governance

## Predictive Philosophy

**MHDE is a current-evidence hypothesis discovery engine, not a historical pattern-matching engine.**

Predictions are based on observable signals as of today. MHDE does not fit rules to past price
returns and project them forward. Historical outcomes (forward returns, drawdowns) are used to
evaluate whether the current-evidence logic is producing useful hypotheses — they are feedback
on logic quality, not the basis of the logic itself.

Any model (including XGBoost) that is trained on historical forward returns is subject to the
following governance constraints before it may influence production outputs. Using past returns
as a training target does not mean the engine is pattern-matching — but it does require
validation that the model has learned generalizable current-evidence signals, not price
artifacts.

## XGBoost Ranker — Quarantine Policy

The XGBoost model (`models/xgboost_ranker.py`) is experimental. It is subject to the following
constraints until explicitly validated and promoted:

**Current status: QUARANTINED — experimental only.**

1. It is not used for alerts, tier assignments, or rankings.
2. It is not used in the daily radar pipeline.
3. Its outputs are logged to `model_runs` for inspection only.
4. Training requires ≥30 labeled examples. Without this, training is skipped.
5. Every training run emits: "Experimental only. Not used for alerts or rankings."

## Graduation Criteria

The XGBoost model may be promoted to influence scoring if ALL of the following are met:

- [ ] At least 90 days of daily runs with candidate outcomes
- [ ] At least 200 labeled examples (forward_return_20d not null)
- [ ] AUC ≥ 0.60 on held-out test set
- [ ] Positive rate in test set is between 20% and 80% (not degenerate)
- [ ] Manual review of top-10 feature importances confirms they are sensible
- [ ] Explicit decision logged in `docs/decision_log.md`

## No Paper Trading Policy

MHDE does not include paper trading. Candidate outcome tracking (`candidate_outcomes` table)
is the evaluation mechanism. Simulating positions, portfolio returns, stop losses, or exits
is outside scope.

## LLM Governance

- Default provider is MockProvider. Mock outputs must be clearly labeled `[Mock]`.
- LLM outputs are informational. They are not used to compute scores or trigger alerts automatically.
- All LLM calls are logged to `llm_runs` with input/output hashes for auditability.
- Prompt versions are tracked in `llm/prompts/` and the prompt registry.
