# MHDE Learning Report — 2026-05-01

**Generated:** 2026-05-01T23:56:53.199927
**Outcomes tracked:** 970
**Reviews completed:** 7

---

## Outcome by Tier

| Tier | Count | Avg 20d Return | Avg 60d Return | Avg Drawdown 20d |
|------|-------|---------------|----------------|-----------------|
| C | 290 | N/A | N/A | N/A |
| Incomplete | 680 | N/A | N/A | N/A |

## Outcome by Score Bucket

| Score Bucket | Count | Avg 20d Return | Avg 60d Return |
|-------------|-------|----------------|----------------|
| <50 | 902 | N/A | N/A |
| 50-59 | 68 | N/A | N/A |

## Outcome by Review Status

| Review Status | Count | Avg Usefulness | Avg Thesis Quality | Avg Evidence Quality |
|--------------|-------|----------------|-------------------|---------------------|
| needs_more_evidence | 4 | 3.2 | 3.0 | 2.8 |
| invalid_due_to_data_issue | 3 | 1.3 | 1.0 | 1.0 |

## False-Positive Reasons

- **bad_data**: 3

## Score Components vs Outcome

No linked score/outcome data.

## Feature Coverage

- Avg feature coverage: 87%
- Min coverage: 86%
- Max coverage: 88%

## Source Reliability

| Source | Runs | Error Rate | Last Run |
|--------|------|-----------|---------|
| fred | 15 | 0% | 2026-05-01 23:53:21.085336 |
| events | 14 | 0% | 2026-05-01 23:55:58.460308 |
| finra | 14 | 0% | 2026-05-01 23:55:44.988820 |
| sec_edgar | 16 | 0% | 2026-05-01 23:52:47.037231 |
| polygon | 15 | 0% | 2026-05-01 23:53:19.322035 |
| cftc | 14 | 0% | 2026-05-01 23:55:48.031864 |
| stooq | 8 | 0% | 2026-05-01 23:53:19.329186 |

## LLM vs Human Review

- **mock** (2 reviewed): avg usefulness 3.0, avg thesis quality 3.0

## Suggested Experiments & Insights

### 1. [HIGH] data_quality
3 candidates failed due to bad or stale data. Address XBRL concept selection, foreign filer currency, and industry-specific financial logic before changing score weights. See experiment history.

## Experiment History

| ID | Status | Hypothesis | Approved By |
|----|--------|-----------|------------|
| c129101b591b43a4 | applied | Broken shares, equity, or denominator values can create fake... | jp_goncalves |
| 5c873dcb5ac24a80 | applied | Banks and insurers need industry-specific revenue and qualit... | jp_goncalves |
| 3fa9a31cc3704b85 | applied | Foreign private issuers using 20-F/6-K and non-USD reporting... | jp_goncalves |
| ef3fcd8180574d8f | rejected | Increase stale_data penalty in risk_score when fundamentals ... | — |


---
> Research purposes only. Not investment advice.
> MHDE does not automatically apply scorecard changes.
> All experiments require human approval before being applied.