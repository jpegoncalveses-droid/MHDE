# MHDE Learning Loop

## What MHDE Is

**MHDE is a current-evidence hypothesis discovery engine, not a historical pattern-matching engine.**

MHDE reads available data today — filings, fundamentals, prices, macro context, short interest,
events — and asks: "given what is observable right now, does this company show signs worth
investigating?" It does not back-fit rules to historical price patterns. It does not claim that
patterns from the past guarantee future returns.

Historical outcomes (candidate_outcomes) and human reviews (candidate_reviews) are used to
evaluate whether the current-evidence logic is useful — not to produce forecasts, and not to
guarantee future performance.

## Principle

MHDE learns whether it is surfacing **useful market hypotheses**, not merely whether a ticker
went up after being surfaced. A candidate can be a good hypothesis even if the stock falls.
A candidate can be a bad hypothesis even if the stock rises.

## The Learning Cycle

```
Candidate surfaced
    → evidence reviewed by human
    → outcome measured (forward returns, drawdowns)
    → error classified with structured taxonomy
    → source/feature/score/prompt weakness identified
    → improvement proposed as scorecard experiment
    → experiment tested
    → human approves
    → versioned release
```

## What MHDE Does NOT Do

- Does **not** automatically change score weights
- Does **not** automatically change prompts
- Does **not** automatically change features
- Does **not** automatically change source priorities
- Does **not** automatically change model behavior
- Does **not** interpret "stock went up" as "hypothesis was good"
- Does **not** implement paper trading or simulate brokerage execution

## Two Questions MHDE Asks

1. **Did the stock go up?** (forward return — useful but not sufficient)
2. **Was the hypothesis good given the evidence available at the time?** (human review — primary signal)

## Data Collected

### Candidate Outcomes (`candidate_outcomes`)
- Forward returns: 1d, 5d, 20d, 60d, 120d
- Max drawdown: 20d, 60d
- Max runup: 20d, 60d
- Hit 10%/20% before down 10% (binary)
- Review status (machine-updated): pending → validated / false_positive / needs_more_time / etc.

### Candidate Reviews (`candidate_reviews`)
Human assessments that answer: **was this a good hypothesis?**
- `usefulness_score` (1–5): Was this worth investigating?
- `thesis_quality_score` (1–5): Was the thesis well-constructed?
- `evidence_quality_score` (1–5): Was the evidence credible?
- `false_positive_reason`: Structured taxonomy of why it failed
- `missed_risk`, `missing_evidence`, `review_notes`: Free-form enrichment

### Scorecard Experiments (`scorecard_experiments`)
Proposals generated from patterns in outcomes and reviews.
- May be proposed automatically by `learning/insights.py`
- May be tested automatically (backtested against historical data)
- **Must NOT be applied automatically**
- Require explicit `approved_by` + `applied_at` fields before taking effect

## Error Taxonomy

Structured reasons for false positives:

| Code | Meaning |
|------|---------|
| `bad_data` | Data was wrong or corrupted |
| `stale_data` | Data was too old to be meaningful |
| `cheap_for_good_reason` | Low valuation justified by fundamentals |
| `weak_catalyst` | Catalyst was not real or not material |
| `poor_quality_business` | Business has structural problems |
| `macro_headwind` | Correct hypothesis, wrong macro environment |
| `llm_overstated_case` | LLM made the thesis sound stronger than it was |
| `missing_peer_context` | No sector/peer comparison available |
| `temporary_noise` | Signal was noise, not signal |
| `not_actionable` | Hypothesis was true but not actionable |
| `overfit_score` | Score was high due to scoring artifact |
| `insufficient_liquidity` | Ticker was too illiquid to be relevant |
| `missing_risk_factor` | A key risk was not captured |
| `source_failure` | Bad outcome due to source data failure, not hypothesis |
| `other` | None of the above |

## Insights and Experiment Proposals

`learning/insights.py` inspects outcome and review data and emits structured insights.
Each insight may suggest a scorecard experiment. Examples:

- A-tier candidates have low usefulness scores → propose tightening A-tier threshold
- Many `weak_catalyst` reviews → propose raising catalyst evidence requirement
- Many `llm_overstated_case` → propose adding a LLM critique pass
- Many `missing_peer_context` → propose adding peer comparison feature
- Source >30% error rate → flag source quality before changing score weights

## CLI

```bash
python main.py learn summarize
```

Generates:
- `outputs/learning_report_YYYY-MM-DD.md`
- `outputs/learning_report_YYYY-MM-DD.json`

## Dashboard

Page 13 — `dashboard/pages/13_learning_calibration.py`

Shows outcome calibration, review quality, false-positive taxonomy, score component
correlations, feature coverage, source reliability, LLM vs human review, and suggested
experiments. Includes a review submission form.

## What Counts as a Production Change

A production change to MHDE's scoring, features, prompts, or model behavior requires:

1. Evidence from `candidate_reviews` or `candidate_outcomes`
2. A scorecard experiment record in `scorecard_experiments` with status `proposed`
3. Testing (backtesting or manual review)
4. Human approval (`approved_by` field set, status → `approved`)
5. A versioned release with `docs/decision_log.md` entry

No change may be applied with only forward-return data as evidence. Human review quality
signals must also support the change.
