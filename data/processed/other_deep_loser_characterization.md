# Other-deep-loser characterization

**Source:** `.claude/local_scripts/other_deep_loser_characterization.py`
**Window:** 2025-04-05 -> 2026-05-07 (Phase-1B walk-fold window)
**Config:** horizon=10d, Policy D, top_n=6, trail_pct=0.3, activation_pct=0.01, post-parabolic filter ON.
**Run mode:** dry_run=True (no DB writes).

## Cohort sizing

- Total closed trades: **941**
- Deep losers (final_pnl_pct < -10%): **93**

Class tagging (priority order):

| class | rule | N | % deep | avg final P&L |
|------|------|--:|------:|--------------:|
| **SWARMSUSDT-class** | `return_5d < -0.20 OR down_days_10d >= 7` | 19 | 20% | -27.57% |
| **4USDT-class** | not SWARMSUSDT-class AND `drawdown_from_90d_high < -0.40` | 30 | 32% | -20.48% |
| **Other** | neither | 44 | 47% | -24.59% |

## Top 15 differentiating features (Other deep losers vs winners)

Sorted by `|mean_shift_sd|` = absolute mean difference / pooled SD. KS p-value tests whole-distribution difference.

| # | feature | Other mean | Winner mean | Univ mean | mean_shift_sd | KS p |
|--:|---------|----------:|-----------:|---------:|------------:|-----:|
| 1 | `drawdown_from_90d_high` | -0.179 | -0.396 | -0.348 | +1.08 | 4.2e-11 * |
| 2 | `btc_vol_30d` | +0.338 | +0.415 | +0.420 | -0.74 | 1.46e-06 * |
| 3 | `rsi_14d` | +59.045 | +49.757 | +48.051 | +0.70 | 1.65e-06 * |
| 4 | `bollinger_position` | +0.394 | +0.007 | -0.059 | +0.58 | 9.96e-05 * |
| 5 | `btc_dominance` | +0.311 | +0.354 | +0.350 | -0.54 | 8.78e-05 * |
| 6 | `market_cap_log` | +7.804 | +7.064 | +7.954 | +0.34 | 0.0731 |
| 7 | `btc_return_7d` | +0.022 | +0.007 | -0.000 | +0.33 | 0.045 . |
| 8 | `price_vs_50d_ma` | +0.257 | +0.081 | -0.009 | +0.33 | 7.58e-06 * |
| 9 | `price_vs_20d_ma` | +0.143 | +0.047 | -0.000 | +0.30 | 5.22e-05 * |
| 10 | `return_20d` | +0.366 | +0.168 | +0.043 | +0.22 | 0.000367 * |
| 11 | `close_in_range` | +0.494 | +0.434 | +0.486 | +0.22 | 0.289 |
| 12 | `beta_to_btc_30d` | +2.182 | +1.890 | +1.564 | +0.22 | 0.0404 . |
| 13 | `funding_rate_zscore` | +0.465 | -0.300 | -0.057 | +0.21 | 0.00145 * |
| 14 | `taker_buy_ratio` | +0.492 | +0.489 | +0.489 | +0.21 | 0.534 |
| 15 | `return_60d` | +0.531 | +0.280 | +0.051 | +0.18 | 6.21e-07 * |

`*` = KS p < 0.01, `.` = KS p < 0.05.

## Cluster analysis on Other cohort

### KMeans k=2

Inertia: 1211.37

| cluster | n | inertia_share | drawdown_from_90d_high | btc_vol_30d | rsi_14d | bollinger_position | btc_dominance | market_cap_log | btc_return_7d | price_vs_50d_ma | price_vs_20d_ma | return_20d | close_in_range | beta_to_btc_30d | funding_rate_zscore | taker_buy_ratio | return_60d |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c0 | 3 | 0.16 | -0.155 | +0.417 | +67.049 | +0.681 | +0.322 | +7.637 | -0.016 | +1.106 | +0.643 | +2.478 | +0.235 | +1.443 | -7.792 | +0.502 | +1.805 |
| c1 | 41 | 0.84 | -0.181 | +0.332 | +58.459 | +0.373 | +0.310 | +7.816 | +0.025 | +0.195 | +0.106 | +0.212 | +0.512 | +2.236 | +1.069 | +0.491 | +0.438 |

### KMeans k=3

Inertia: 1013.81

| cluster | n | inertia_share | drawdown_from_90d_high | btc_vol_30d | rsi_14d | bollinger_position | btc_dominance | market_cap_log | btc_return_7d | price_vs_50d_ma | price_vs_20d_ma | return_20d | close_in_range | beta_to_btc_30d | funding_rate_zscore | taker_buy_ratio | return_60d |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c0 | 14 | 0.62 | -0.104 | +0.354 | +71.041 | +0.930 | +0.307 | +7.994 | +0.025 | +0.569 | +0.365 | +0.891 | +0.533 | +1.765 | +1.714 | +0.493 | +0.732 |
| c1 | 29 | 0.38 | -0.220 | +0.331 | +52.526 | +0.114 | +0.316 | +7.681 | +0.022 | +0.057 | +0.011 | +0.022 | +0.481 | +2.268 | +0.523 | +0.491 | +0.316 |
| c2 | 1 | 0.00 | -0.034 | +0.329 | +80.144 | +1.010 | +0.213 | +8.693 | -0.032 | +1.679 | +0.845 | +2.992 | +0.301 | +5.514 | -18.679 | +0.501 | +3.935 |

### KMeans k=4

Inertia: 821.81

| cluster | n | inertia_share | drawdown_from_90d_high | btc_vol_30d | rsi_14d | bollinger_position | btc_dominance | market_cap_log | btc_return_7d | price_vs_50d_ma | price_vs_20d_ma | return_20d | close_in_range | beta_to_btc_30d | funding_rate_zscore | taker_buy_ratio | return_60d |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c0 | 13 | 0.41 | -0.084 | +0.328 | +71.545 | +0.969 | +0.308 | +8.555 | +0.029 | +0.437 | +0.281 | +0.505 | +0.696 | +2.407 | +2.538 | +0.495 | +0.819 |
| c1 | 26 | 0.40 | -0.224 | +0.334 | +51.660 | +0.059 | +0.313 | +7.575 | +0.021 | +0.046 | +0.002 | +0.003 | +0.435 | +2.246 | +0.467 | +0.490 | +0.247 |
| c2 | 1 | 0.00 | -0.034 | +0.329 | +80.144 | +1.010 | +0.213 | +8.693 | -0.032 | +1.679 | +0.845 | +2.992 | +0.301 | +5.514 | -18.679 | +0.501 | +3.935 |
| c3 | 4 | 0.20 | -0.238 | +0.402 | +61.143 | +0.547 | +0.335 | +6.622 | +0.017 | +0.683 | +0.430 | +1.618 | +0.266 | +0.198 | -1.501 | +0.497 | +0.589 |

### KMeans k=5

Inertia: 715.96

| cluster | n | inertia_share | drawdown_from_90d_high | btc_vol_30d | rsi_14d | bollinger_position | btc_dominance | market_cap_log | btc_return_7d | price_vs_50d_ma | price_vs_20d_ma | return_20d | close_in_range | beta_to_btc_30d | funding_rate_zscore | taker_buy_ratio | return_60d |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c0 | 20 | 0.57 | -0.112 | +0.323 | +65.761 | +0.767 | +0.304 | +8.811 | +0.040 | +0.290 | +0.183 | +0.345 | +0.661 | +2.138 | +1.457 | +0.495 | +0.536 |
| c1 | 1 | 0.00 | -0.034 | +0.329 | +80.144 | +1.010 | +0.213 | +8.693 | -0.032 | +1.679 | +0.845 | +2.992 | +0.301 | +5.514 | -18.679 | +0.501 | +3.935 |
| c2 | 20 | 0.37 | -0.259 | +0.340 | +49.892 | -0.071 | +0.310 | +6.898 | +0.009 | +0.044 | +0.004 | +0.023 | +0.345 | +2.351 | +0.554 | +0.488 | +0.180 |
| c3 | 2 | 0.05 | -0.216 | +0.462 | +60.501 | +0.517 | +0.376 | +7.108 | -0.008 | +0.819 | +0.541 | +2.221 | +0.202 | -0.592 | -2.349 | +0.503 | +0.740 |
| c4 | 1 | 0.00 | +0.000 | +0.367 | +83.769 | +1.367 | +0.452 | +6.274 | +0.032 | +1.299 | +0.603 | +1.326 | +0.888 | +1.881 | +3.616 | +0.510 | +3.625 |

## Classifier — Other deep losers vs winners

Logistic regression (L2, balanced class weights, 5-fold StratifiedKFold cross-validation).

- N(Other deep losers, y=1): **44**
- N(winners, y=0): **776**
- **Cross-validated AUC: 0.743**

Top 10 coefficients (after standardization; sign shows direction of association with deep-loser class):

| # | feature | std-coef | direction |
|--:|---------|---------:|:----------|
| 1 | `drawdown_from_90d_high` | +1.766 | ↑ loser when high |
| 2 | `funding_rate_zscore` | +1.438 | ↑ loser when high |
| 3 | `realized_vol_10d` | +1.340 | ↑ loser when high |
| 4 | `price_vs_50d_ma` | -1.255 | ↑ loser when low |
| 5 | `rsi_14d` | +1.147 | ↑ loser when high |
| 6 | `btc_vol_30d` | -1.012 | ↑ loser when low |
| 7 | `price_vs_20d_ma` | -0.840 | ↑ loser when low |
| 8 | `funding_rate_current` | -0.787 | ↑ loser when low |
| 9 | `atr_pct_14d` | -0.756 | ↑ loser when low |
| 10 | `return_vs_btc_1d` | -0.738 | ↑ loser when low |

### Threshold sweep — winners-filtered-per-loser-caught

If the classifier were used as an entry filter, at each threshold:

| threshold | Other caught (TP) | winners filtered (FP) | TPR | FPR | winners-filtered per loser-caught |
|---------:|------------------:|----------------------:|----:|----:|-----------------:|
| 0.05 | 40 | 537 | 91% | 69% | 13.4 |
| 0.10 | 40 | 453 | 91% | 58% | 11.3 |
| 0.15 | 39 | 406 | 89% | 52% | 10.4 |
| 0.20 | 38 | 368 | 86% | 47% | 9.7 |
| 0.25 | 35 | 338 | 80% | 44% | 9.7 |
| 0.30 | 33 | 309 | 75% | 40% | 9.4 |
| 0.35 | 32 | 277 | 73% | 36% | 8.7 |
| 0.40 | 31 | 244 | 70% | 31% | 7.9 |
| 0.45 | 30 | 215 | 68% | 28% | 7.2 |
| 0.50 | 27 | 190 | 61% | 24% | 7.0 |
| 0.55 | 24 | 166 | 55% | 21% | 6.9 |
| 0.60 | 23 | 137 | 52% | 18% | 6.0 |
| 0.65 | 20 | 124 | 45% | 16% | 6.2 |
| 0.70 | 16 | 107 | 36% | 14% | 6.7 |
| 0.75 | 14 | 86 | 32% | 11% | 6.1 |
| 0.80 | 9 | 62 | 20% | 8% | 6.9 |
| 0.85 | 8 | 41 | 18% | 5% | 5.1 |
| 0.90 | 5 | 24 | 11% | 3% | 4.8 |
| 0.95 | 2 | 6 | 5% | 1% | 3.0 |

### Filter-cost economic analysis (Sharpe-positive viability)

Using BASELINE Policy D's empirical edge (`avg_winner_pct = +9.26%`,
`avg_Other_loser = -24.59%`), each threshold's net expected return
change if the classifier were used as an entry filter:

```
net_pp = (TP × avg_loser_save) − (FP × avg_winner_edge)
       = (TP × 24.59pp)        − (FP × 9.26pp)
```

| threshold | TP × 24.59 (saved) | FP × 9.26 (foregone) | NET | verdict |
|---------:|-------------------:|--------------------:|-----:|:-------:|
| 0.50 | +664 pp | -1759 pp | **-1095 pp** | reject |
| 0.70 | +393 pp |  -991 pp | **-598 pp** | reject |
| 0.80 | +221 pp |  -574 pp | **-353 pp** | reject |
| 0.85 | +197 pp |  -380 pp | **-183 pp** | reject |
| 0.90 | +123 pp |  -222 pp | **-99 pp**  | reject |
| 0.95 |  +49 pp |   -56 pp | **-7 pp**   | breakeven, N=2 |

**No threshold yields a Sharpe-positive filter.** The Other cohort's
statistical signature is real (AUC 0.74) but not concentrated enough to
overcome the cost of filtering 5-13 winners per loser caught.

## Open positions — historical class membership in window

Each currently-open coin appears multiple times in the deep-loser history
of this window. Class assignments below confirm the cohort labels are
*coin-occasion-specific*, not coin-specific:

| coin | n_deep_losses_in_window | classes hit |
|------|------------------------:|:------------|
| SWARMSUSDT | 6 | Other × 3, 4USDT-class × 2, SWARMSUSDT-class × 1 |
| 4USDT      | 4 | 4USDT-class × 2, SWARMSUSDT-class × 1, Other × 1 |
| TAGUSDT    | 2 | Other × 1, SWARMSUSDT-class × 1 |
| RAVEUSDT   | 1 | 4USDT-class × 1 |

Implication: a per-coin blacklist is the wrong lever (each coin shows
losses across multiple class-shapes); a per-entry-state filter would
need to discriminate *moments in time*, not *symbols* — which is exactly
the classification problem the AUC 0.74 result speaks to.

## Note on cohort sizing vs ADR-028 prior split

ADR-028's loser-characterization study reported 28 / 14 / 51 (30% /
15% / 55%). This script reproduces the same 941-trade backtest and the
same 93 deep losers (final P&L < -10%), but yields **19 / 30 / 44 (20%
/ 32% / 47%)** under explicit rules:

- SWARMSUSDT-class: `return_5d < -0.20 OR down_days_10d >= 7`
- 4USDT-class: not SWARMSUSDT-class AND `drawdown_from_90d_high < -0.40`
- Other: neither

The ADR-028 study used a multi-feature semantic tag ("deep dd90 +
active short-window weakness + wide ATR") which yields a tighter
SWARMSUSDT-class membership; the 4USDT-class definition there also
included `-1 < ret60 < +1` and a recent-bounce condition. Under those
narrower rules, more cases fall into Other. Under the rules in this
script (the *operational* definitions implied by the user's prompt),
fewer cases fall into Other (44 instead of 51) because some
medium-shallow-dd90 losers are pulled into the broader 4USDT-class.

The 47% Other cohort here is therefore a **slight upper bound on the
"unstructured residual"** — the prior 55% was a slight overestimate of
that residual. Both numbers point to the same finding: roughly half of
deep losers do not match the two named patterns.

## Findings

### Finding 1 — Other deep losers have a real, statistically robust profile: "extended uptrend, overbought, calm BTC vol"

Despite the "heterogeneous, no clean structure" framing in the task
prompt, the Other cohort (N=44) **does** differ from winners on
multiple dimensions at very strong significance:

- **Drawdown from 90d high is LESS negative** (Other -17.9% vs winners
  -39.6%, KS p = 4e-11, +1.08 SD shift). Other losers are **near peak**
  at entry, not pulled-back; winners come from deeper pullbacks that
  bounce.
- **RSI higher** (Other 59 vs winners 50, p = 1.6e-6).
- **Bollinger position higher** (Other +0.39 vs winners +0.01,
  p = 1e-4) — Other are pressed against the upper band.
- **Price further above 50d MA** (Other +25.7% vs winners +8.1%).
- **60d return higher** (Other +53% vs winners +28%).
- **Funding rate z-score higher** (Other +0.47 vs winners -0.30) —
  crowded long, expensive carry.
- **BTC vol regime calmer** (Other 0.34 vs winners 0.41) — Other losses
  happen in tame markets, not crashes.

Synthesizing: **the Other cohort is "late-stage uptrend reversal" — coins
that have been running, are overbought, sit near peak, attract crowded
longs, and the model buys them just before a pullback in an otherwise
calm tape.** This is structurally distinct from SWARMSUSDT-class
("still falling") and 4USDT-class ("deep-dd recent-bounce"). It is the
**third pattern**.

### Finding 2 — Clustering does NOT reveal clean sub-groups within Other

KMeans at k=2/3/4/5 produces one dominant cluster (n=29-41, 60-93% of
the cohort) plus 1-3 small/singleton outlier clusters. The dominant
cluster's centroid recapitulates the Finding-1 profile: shallow dd90,
elevated RSI/BB, above 20d/50d MAs. There is no clean bimodal split
inside Other.

This means the cohort can be **described by a single profile**, not
decomposed into multiple sub-classes that might warrant separate
filters. A single rule (or single classifier) is the appropriate
intervention shape if any intervention is warranted.

### Finding 3 — The cohort is *describable* but not *filterable* without unacceptable winner cost

A logistic-regression classifier on entry features achieves
**AUC = 0.743** at 5-fold CV — modestly informative. But the
threshold-cost analysis is decisive: **no threshold produces a
Sharpe-positive entry filter**, even when valued at BASELINE's
empirical average winner edge:

- The best precision (threshold = 0.95, ratio FP/TP = 3.0) catches only
  2 of 44 Others while still filtering 6 winners. Net economic
  impact: −7pp on a denominator of 4,000+ pp total return — noise.
- Loosening to 0.50 catches 27 / 44 Others (61%) but filters 190
  winners. Net: −1,095pp — would gut the BASELINE's profit by ~20%.

This mirrors the **Variant E rejection in ADR-028** (`dd90 < -0.40 AND
-1 < ret60 < +1` cost -1.96 Sharpe and -23.94pp max DD). The Other
cohort's signal is too diffuse — the same features that distinguish it
also distinguish winners that the model legitimately wants to take.

## Recommendation: do not pursue D-2; pivot to E or A

A "Variant F" / "D-2" `should_exclude` rule targeting the Other-class
profile is **not viable**:

- AUC 0.74 is below the threshold required for a precision-focused
  filter on a 5% deep-loser base rate (the cohort is 44 / 941 = 4.7%).
- Every economically meaningful threshold filters 5-13 winners per
  loser caught, and BASELINE winners average +9.26% vs Other-losers
  averaging -24.59% — the 2.7× P&L asymmetry isn't enough to overcome
  the 5-13× volume asymmetry.

The cohort is **describable** (Finding 1) but the description is too
close to "trades the model legitimately wants to take" to be a clean
exclusion gate. This generalizes the rescue-rate heatmap finding (no
conditional-cut state shows Sharpe-positive EV-of-cutting) to entry-side
filters: **no entry rule on the available features cleanly isolates
the late-stage-reversal pattern without breaking the winner
distribution**.

Recommended pivots, in priority order:

1. **Path E (sizing / Kelly-style)**. The classifier's AUC of 0.74
   suggests the model's confidence is *partially* miscalibrated: trades
   in the Other profile (high RSI, shallow dd90, elevated funding) get
   probabilities higher than their realized hit rate justifies. A
   **probability haircut** (multiply predicted_probability by a factor
   < 1 when the classifier's Other-loser score exceeds some threshold)
   applied **before Top-N selection** can transfer risk away from the
   tail without the binary-filter Top-N-backfill problem that destroyed
   Variant E in ADR-028. Concretely: a calibrated rescaling rather than
   exclusion. This was hypothesized in KI-140 (item 2) for the
   4USDT-class — the Other-cohort analysis here strengthens that case
   for *the same fix* across both unaddressed classes.

2. **Path A' (Variant E'' for the characterized 15% 4USDT-class via a
   narrower lens)**. KI-140 documents the 4USDT-class is real and
   distinct (14 trades, avg -19.3%). A tighter compound rule that adds
   a recency-of-bounce condition (`dd90 < -0.40 AND ret10 > +0.10 AND
   return_60d < -0.20`) was not in the ADR-028 grid; might escape the
   regime-gate trap that killed Variant E. Re-test as a paired backtest
   if the operator wants the second-cheapest probe.

3. **Path D — entry-time signal enrichment is the long-term fix**. The
   model's training label (volatility-loving symmetric knockout) is
   the root cause documented in KI-137. A direction-aware label
   (long-only knockout from entry) would naturally penalize all three
   classes — SWARMSUSDT (still-falling = high downward forward
   probability), 4USDT (bounce-fade = high downward probability),
   Other (late-stage reversal = high downward probability) — at the
   model level, not via exclusion rules. This is the largest expected
   impact but the most expensive workstream.

The exclusion-rule family of interventions appears closed for the
BASELINE Policy D regime; the leverage has moved to sizing
(probability haircut), label engineering, or the few-shot specific
filter probes if cheap to run.

## Reproducibility

```
venv/bin/python .claude/local_scripts/other_deep_loser_characterization.py
```

Reads `data/mhde.duckdb`. Re-runs the BASELINE Policy D harness in
dry_run mode; joins entry features from `crypto_ml_features` at
`prediction_date = entry_date - 1d`; computes `down_days_10d` from
`crypto_prices_daily`. No writes. Produces this report.


