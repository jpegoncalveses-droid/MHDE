# MHDE Scoring Model Breakdown

> Shadow-only milestone: production scores are read-only. This document describes
> the scoring model as implemented in `scoring/scorecard.py` and the feature modules.

---

## Score Formula

```
total_score = clamp(
    0.30 × cheap_score
  + 0.25 × quality_score
  + 0.25 × catalyst_score
  + 0.10 × momentum_score
  + 0.10 × sentiment_score
  - 0.20 × risk_penalty,
  0, 100
)
```

- Missing components are **skipped** (no zero penalty for absent data)
- `risk_penalty` defaults to 50 if the `risk` feature group is absent
- Final score is clamped to [0, 100]

---

## Component Weights

| Component      | Weight | Direction   | Source module           |
|---------------|--------|-------------|-------------------------|
| cheap          | 0.30   | positive    | `features/valuation.py` |
| quality        | 0.25   | positive    | `features/quality.py`   |
| catalyst       | 0.25   | positive    | `features/catalyst.py`  |
| momentum       | 0.10   | positive    | `features/momentum.py`  |
| sentiment      | 0.10   | positive    | `features/sentiment.py` |
| risk_penalty   | 0.20   | **negative** | `features/risk.py`     |

The positive weights sum to 1.0; risk is subtracted on top, so the theoretical maximum
(all positive components at 100, no risk) is 100 and the effective "neutral" is approximately 40.

---

## Component Breakdowns

### cheap (Valuation) — AVG of 4 features

| Feature            | Formula                               | Score ranges                     |
|-------------------|---------------------------------------|----------------------------------|
| price_vs_52w_high | `100 - (price/week52_high × 100)`    | 0–100 (higher = further below high) |
| ps_proxy          | Price / Revenue per share             | ps<1→90, <3→70, <10→50, ≥10→20  |
| pe_ratio          | Price / EPS                           | pe<10→90, <20→75, <30→55, <50→35, ≥50→15 |
| pb_ratio          | Price / (Equity/Shares)               | pb<1→80, <2→65, <5→45, ≥5→20   |

Bounds checks: ps ∈ [0.05, 100], pe ∈ (0, 150], pb ∈ (0, 50].
Requires ≥ 20 days of price history for `price_vs_52w_high`.

---

### quality (Profitability) — AVG of 4 features

| Feature           | Formula                               | Score ranges                          |
|------------------|---------------------------------------|---------------------------------------|
| net_income_positive | Latest net income               | ni>0→70, ni≤0→30                     |
| revenue_growth_yoy | `(rev_current - rev_prior) / |rev_prior| × 100` | >20%→90, >10%→75, ≥0%→60, <0%→30 |
| net_margin        | `(net_income / revenue) × 100`       | >20%→90, >10%→75, >0%→55, ≤0%→20  |
| dilution_rate     | `(shares_now - shares_prior) / shares_prior × 100` | <2%→85, <5%→65, ≥5%→30 |

Guards: growth >500% rejected as outlier; net income > revenue triggers concept mismatch guard.

---

### catalyst (Event Catalyst) — Point accumulation, capped at 100

| Signal                           | Points |
|----------------------------------|--------|
| Material 8-K in last 30d         | +30    |
| Routine 8-K in last 30d          | +15    |
| 10-Q or 10-K filed in last 45d   | +5     |
| Earnings event in next 14d       | +25    |
| Short interest change > 10%      | +15    |

Material 8-K = contains: earnings, acqui, merger, divestiture, agreement, guidance, revenue,
settlement, dividend, buyback, or restate (case-insensitive).

**Note:** This is the *pre-LLM* catalyst score. The shadow catalyst queue applies an LLM
adjustment on top (shadow-only, does not modify this score).

---

### momentum — AVG of 4 features

| Feature         | Formula                             | Score ranges                              |
|----------------|-------------------------------------|-------------------------------------------|
| return_20d      | `(price_now - price_20d) / price_20d × 100` | >15%→80, >5%→65, ≥-5%→50, <-5%→25 |
| return_60d      | `(price_now - price_60d) / price_60d × 100` | >25%→80, >10%→65, ≥-10%→50, <-10%→25 |
| volume_spike    | `vol_ratio = current_vol / avg_vol_20d` | `MIN(90, 50 + (vol_ratio - 1) × 20)` |
| drawdown_from_high | `(current - high_20d) / high_20d × 100` | `MAX(0, 50 + drawdown × 2)` |

Requires price history from `prices_daily` table (Stooq historical ingestor).

---

### sentiment — AVG of available features

| Feature            | Formula                   | Score ranges                          |
|-------------------|---------------------------|---------------------------------------|
| short_interest_proxy | Latest short interest count | si>20M→40, si>5M→60, si≤5M→70    |
| social_attention   | (stubbed — not implemented) | null                                |

---

### risk_penalty — Additive, capped at 100 (subtracted from score)

| Condition                           | Penalty |
|-------------------------------------|---------|
| Null rate > 70% across features     | +35     |
| Null rate 40–70%                    | +15     |
| Net income ≤ 0                      | +20     |
| Price < $2.00 (micro-cap)           | +20     |
| < 20 days of price history          | +10     |
| No SEC filings on record            | +15     |

---

## Tier Thresholds

Assigned by `scoring/tiers.py::assign_tier()`:

| Tier       | Conditions                                                           |
|-----------|----------------------------------------------------------------------|
| A          | total ≥ 75 AND catalyst ≥ 50 AND risk ≤ 50 AND coverage ≥ 0.80     |
| B          | total ≥ 60 AND coverage ≥ 0.65                                      |
| C          | total ≥ 45                                                           |
| Reject     | total < 45 OR risk_penalty > 75                                     |
| Incomplete | observed_count < 2 (fewer than 2 of 5 major components present)     |

**Coverage** = fraction of positive-weight component mass that is non-null.

The catalyst queue focuses on tickers in the Reject tier with score 40–44.9 (near-threshold),
where an LLM-validated catalyst might push them to C or above.

---

## Coverage and Confidence

```
coverage = (sum of weights for non-null positive components) / (sum of all positive weights)
```

Coverage thresholds for confidence labels:
- ≥ 0.80 → **high**
- ≥ 0.50 → **medium**
- > 0   → **low**
- 0     → **none**

---

## Score Decomposition Artifact

`data/processed/latest_score_components.csv` — generated by `export_score_components()` in
`scoring/scorecard.py`. Contains one row per ticker with all component scores.

Also exposed per-entry in the daily catalyst queue CSV:
`cheap_score`, `quality_score`, `catalyst_score`, `momentum_score`, `sentiment_score`,
`risk_penalty_score`, `major_positives`, `major_negatives`.

---

## Example Calculations

### Hypothetical CTRA (near-threshold, M&A pending)
```
cheap=70, quality=65, catalyst=80, momentum=55, sentiment=60, risk=15

total = 0.30×70 + 0.25×65 + 0.25×80 + 0.10×55 + 0.10×60 − 0.20×15
      = 21.0 + 16.25 + 20.0 + 5.5 + 6.0 − 3.0
      = 65.75 → Tier B

major_positives: cheap (70), quality (65), catalyst (80)
major_negatives: (none — risk is only 15)
```

### Hypothetical VG (near-threshold, settlement pending)
```
cheap=45, quality=55, catalyst=60, momentum=40, sentiment=50, risk=25

total = 0.30×45 + 0.25×55 + 0.25×60 + 0.10×40 + 0.10×50 − 0.20×25
      = 13.5 + 13.75 + 15.0 + 4.0 + 5.0 − 5.0
      = 46.25 → Tier C

major_positives: catalyst (60 ≥ 65? → no, borderline)
major_negatives: cheap (45), momentum (40)
```

---

## Shadow Catalyst Adjustment (LLM layer)

For near-threshold tickers, the LLM shadow queue applies an adjustment on top:

```
shadow_score = original_score + llm_adjustment
```

Default static adjustments (shadow-only, does not modify `total_score` in DB):

| Condition                | Adjustment |
|--------------------------|-----------|
| High confidence bullish  | +5         |
| Medium confidence bullish| +3         |
| High confidence bearish  | -5         |
| Medium confidence bearish| -3         |

A scaled adjustment model (`missed/catalyst_adjustment.py`) will replace this
in a future milestone with priced-in, time-decay, and scope adjustments.
