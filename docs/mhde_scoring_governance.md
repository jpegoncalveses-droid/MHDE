# MHDE Scoring Governance

This document explains the governance model for changing the scoring model: the shadow-only principle, feature flags, the signal proposal workflow, rollback criteria, and what operators must not do.

---

## The Shadow-Only Principle

MHDE's central safety invariant:

> **Production scoring never changes unless a feature flag is explicitly set to `true` in `config/settings.yaml`.**

All flags default to `false`. No experiment can affect the production score unless an operator makes a deliberate, documented edit to `config/settings.yaml`.

This means:

- The production score field in `scores` and `candidate_outcomes` is always computed from the current, stable, flag-off weights.
- Shadow scores (which reflect enabled experimental adjustments) are computed separately and never overwrite the production score.
- The gap between production score and shadow score is logged for each run when flags are enabled, so operators can compare them in `/learning`.

The implementation is in `governance/feature_flags.py`. The function `apply_shadow_adjustments()` enforces the invariant:

```python
return {
    "production_score": base_score,   # never changed
    "shadow_score": shadow,            # reflects enabled adjustments
    "adjustments": adjustments,        # dict of what was applied
}
```

---

## Feature Flags

### Where they live

Two files work together:

1. **`config/settings.yaml`** — the on/off switch for each flag:

```yaml
feature_flags:
  scaled_catalyst_adjustment: false
  sector_momentum_boost: false
  earnings_surprise_boost: false
  news_contract_boost: false
  risk_haircut: false
```

2. **`governance/feature_flags.py`** — the code registry. The `FeatureFlag` enum lists all valid flags. `load_flags_from_config()` reads the YAML and builds a `FeatureFlagRegistry`. `FeatureFlagRegistry.is_enabled(flag)` is called at scoring time.

### How to enable a flag

1. Ensure a governance proposal has been created and approved first (see below).
2. Edit `config/settings.yaml` and change the flag's value from `false` to `true`:

```yaml
feature_flags:
  earnings_surprise_boost: true
```

3. The flag takes effect on the next pipeline run (no server restart required — the config is read fresh each run).

4. Monitor `/learning` for at least 10 trading days after enabling.

### What each flag does

| Flag | Effect when enabled |
|---|---|
| `scaled_catalyst_adjustment` | Adds a catalyst adjustment to the shadow score, scaled by LLM confidence |
| `sector_momentum_boost` | Adds a sector relative-strength boost when the ticker's sector ETF is outperforming |
| `earnings_surprise_boost` | Adds a boost for tickers with recent earnings beats above consensus |
| `news_contract_boost` | Adds a boost when high-confidence positive news catalysts are detected |
| `risk_haircut` | Subtracts a risk penalty component from the shadow score |

All adjustments are clamped to the `[0, 100]` range after application.

---

## Signal Proposal Workflow

This is the required process for promoting any experimental signal to production.

### Step 1 — Gather evidence in shadow mode

Run the system with the feature flag disabled (the default) for at least 20 trading days. The `/learning` route shows prediction-vs-actual metrics for the current scoring configuration. Keep records of:

- Number of candidates evaluated (sample size)
- Precision (fraction of predicted winners that actually moved positively)
- Recall (fraction of actual winners that were predicted)
- Average forward return at 20 days for Tier A candidates

### Step 2 — Create a proposal

Use the CLI to create a formal proposal. This writes an entry to the audit log:

```bash
venv/bin/python main.py learn propose-signal \
    --signal-name earnings_surprise_boost \
    --evidence-period "2026-01-01 to 2026-04-30" \
    --sample-size 180 \
    --precision 0.61 \
    --recall 0.55 \
    --avg-return 0.082 \
    --rollback-criteria "precision < 0.50 over 10 consecutive days"
```

The output includes a `proposal_id` (8-character UUID prefix). Record it.

### Step 3 — Review metrics

Before approving, verify that:

- Sample size is adequate (minimum 20 trading days, at least 30 candidates)
- Precision is above the rollback threshold by a meaningful margin
- Average return is positive and economically significant
- The rollback criteria are specific and measurable

### Step 4 — Approve the proposal

```bash
venv/bin/python main.py learn approve-signal --proposal-id <id>
```

This appends an approval entry to the audit log with a timestamp and the operator's actor name. The approval record includes a note: "To activate: set the corresponding flag to true in config/settings.yaml under feature_flags."

**Approval alone does not activate the flag.** The operator must still edit `config/settings.yaml`.

### Step 5 — Enable the flag

Edit `config/settings.yaml` and set the approved flag to `true`. This is the moment the experimental signal becomes active.

### Step 6 — Monitor post-activation

Watch `/learning` daily for at least 10 trading days after enabling. Compare precision and average return against the pre-activation baseline.

### Step 7 — Rollback if performance drops

If the signal underperforms against the rollback criteria:

```bash
venv/bin/python main.py learn rollback-signal \
    --proposal-id <id> \
    --reason "precision fell to 0.47 over 11 consecutive days (threshold: 0.50)"
```

Then disable the flag in `config/settings.yaml`:

```yaml
feature_flags:
  earnings_surprise_boost: false
```

---

## Rollback Criteria

Each proposal must include explicit rollback criteria stated as a measurable condition. Examples of good rollback criteria:

- "Precision below 0.50 over 10 consecutive trading days"
- "Average 20-day forward return for Tier A negative over any rolling 30-day window"
- "More than 3 false positives in Tier A within 5 trading days"

Examples of bad rollback criteria (too vague):

- "If performance is poor" — not measurable
- "If the operator decides" — no threshold

The rollback command records the reason in the audit log. After rollback, disable the flag and do not re-enable it without a new proposal based on fresh evidence.

---

## Audit Log

All governance events are written to:

```
data/processed/signal_governance_audit.jsonl
```

This is an append-only JSONL file. Each line is a JSON object with at minimum:

| Field | Description |
|---|---|
| `proposal_id` | 8-character UUID prefix, unique per proposal |
| `signal_name` | Name of the feature flag being governed |
| `status` | `proposed`, `approved`, `rolled_back`, or `rejected` |
| `actor` | Who performed the action |
| `timestamp` | UTC ISO 8601 timestamp |

Proposal entries additionally include: `evidence_period`, `sample_size`, `precision`, `recall`, `avg_return`, `rollback_criteria`.

Rollback entries additionally include: `reason`.

The audit log must not be edited manually. If a correction is needed, append a new entry with a note field explaining the correction.

---

## What NOT to Do

### Never change `scoring/scorecard.py` weights directly

Changing the weights in `scoring/scorecard.py` without going through the governance workflow bypasses all auditability. There is no audit trail, no rollback mechanism, and no way to know which run used which weights.

If you want to test a weight change, implement it as a feature flag adjustment in `governance/feature_flags.py`, propose it through the governance workflow, and only merge the weight change into `scorecard.py` after the proposal is approved and validated.

### Never enable a flag without a proposal

Enabling a flag in `config/settings.yaml` before a proposal exists leaves no audit trail. If something goes wrong, there is no rollback record, no evidence period, and no documented criteria for when to stop.

### Never delete or edit the audit log

The audit log is append-only. Do not truncate it, edit existing entries, or delete it. It is the only permanent record of what changes were made to the scoring model and why.

### Never commit `.env` or API keys

All secrets must stay in `.env` (git-ignored). Committing an API key requires rotating it immediately, regardless of how quickly the commit is reverted.
