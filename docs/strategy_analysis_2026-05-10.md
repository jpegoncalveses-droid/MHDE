# Strategy edge & realistic-profitability analysis — 2026-05-10

**Subject.** Phase 1B winner `backtest_10d_D_top_n_a02e15a0` (10d horizon, top-6 daily selection, Policy D trailing stop, `trail_pct=0.30`, `activation_pct=0.01`).

**Question we're answering.** Where exactly does the edge come from, what's a realistic monthly P&L range, and how should the operator interpret "hit" vs "miss" during paper trading?

**Source data.**
- 19,701 walk-fold 10d predictions with non-null outcomes (`crypto_ml_predictions WHERE model_id LIKE 'crypto_10d_walkfold_%'`), 513 prediction days, 2024-12-04 → 2026-04-30, ~38 coins/day average from the binance USDT-M perp top-50 universe.
- 932 executed harness trades for the winner run, 14 calendar months 2025-04 → 2026-05.
- `crypto_prices_daily.BTCUSDT` close series for regime classification.
- Read-only investigation; full reproduction script at `.claude/local_scripts/strategy_edge_analysis.py`. Companion: `.claude/local_scripts/investigate_phase1b_hitrate.py` (the prior session's H1/H2/H3 hypothesis test).

**Companion note.** Read the prior session's investigation first if you haven't — it establishes that `expected_hit_rate=0.871` in `active_spec.json` is **trade-level P&L positivity** (after fees/slippage/funding), not **model label accuracy** (`actual_hit` = max forward close ≥ +10% within 10d). True label hit rate on top-6 is ~48%. That distinction is the through-line of the rest of this document.

---

## Part 1 — Selection edge: where the alpha lives

For each of 513 walk-fold prediction days we compared four daily-selection buckets:

- **(a) Top-6** — model picks (current strategy): top 6 by `predicted_probability`, tie-broken alphabetically.
- **(b) Random-6** — null baseline: 6 distinct coins drawn uniformly without replacement from that day's universe (seed `20260510` for reproducibility).
- **(c) Bottom-6** — anti-strategy: 6 lowest-probability coins.
- **(d) Full daily universe** — market-beta proxy: every coin with a prediction that day (mean 38, range 25–48).

All metrics are on the **label outcome** (`actual_max_return`, `actual_hit`), so this section measures the model's pure prediction edge, isolated from execution-policy effects.

### Aggregate over 513 days

| Bucket | n | mean max ret | median | std | label hit rate | mean max DD |
|---|---:|---:|---:|---:|---:|---:|
| Top-6 (model picks) | 3,078 | **+25.07%** | +8.43% | 208.85% | **45.78%** | −11.68% |
| Random-6 (chance) | 3,078 | +17.07% | +5.81% | 176.96% | 34.08% | −9.53% |
| Bottom-6 (anti-strategy) | 3,078 | +7.20% | +3.96% | 16.25% | 20.63% | −6.94% |
| Full universe (beta) | 19,701 | +13.70% | +5.70% | 117.05% | 34.11% | −9.38% |

**Read these numbers as max-forward-close return over the 10d horizon, label-comparable.**

### Edge over the chance baseline

| | Δ mean max ret | Δ label hit rate |
|---|---:|---:|
| **Top-6 vs random-6** | **+8.00 pp** | **+11.70 pp** |
| Bottom-6 vs random-6 | −9.87 pp | −13.45 pp |
| Universe vs random-6 | −3.37 pp | +0.03 pp |

### Stability of the edge: month-by-month

Top-6 mean max-forward-return vs random-6, full sample:

| month | top-6 | random-6 | bottom-6 | edge (top vs rand) |
|---|---:|---:|---:|---:|
| 2024-12 | +7.32% | +6.43% | +4.82% | **+0.88** |
| 2025-01 | +5.96% | +5.34% | +4.08% | **+0.62** |
| 2025-02 | +7.95% | +6.11% | +4.70% | **+1.84** |
| 2025-03 | +9.17% | +9.13% | +5.23% | +0.04 |
| 2025-04 | +20.99% | +19.11% | +14.36% | **+1.88** |
| 2025-05 | +12.57% | +10.98% | +9.15% | **+1.59** |
| 2025-06 | +9.44% | +6.67% | +3.92% | **+2.76** |
| 2025-07 | +17.64% | +13.40% | +8.07% | **+4.25** |
| 2025-08 | +17.68% | +10.02% | +5.52% | **+7.65** |
| 2025-09 | +9.92% | +9.32% | +12.93% | **+0.60** |
| 2025-10 | +17.29% | +13.25% | +10.11% | **+4.04** |
| 2025-11 | +15.21% | +8.48% | +7.06% | **+6.72** |
| 2025-12 | +28.80% | +11.73% | +7.16% | **+17.07** |
| 2026-01 | +15.56% | +10.91% | +4.10% | **+4.65** |
| 2026-02 | +8.54% | +9.18% | +10.61% | −0.64 |
| 2026-03 | +23.71% | +8.61% | +4.46% | **+15.10** |
| 2026-04 | +197.65% | +131.60% | +6.33% | **+66.05** |

**Top-6 beat random-6 in 16 of 17 months.** Only February 2026 had a negative-edge month (−0.64 pp), and the magnitude was small. The April 2026 outlier (+66 pp edge) is real but inflated by an altcoin-runup tape — random-6 also returned +131% that month, so most of April 2026 is beta, not alpha.

### What this means

- **Yes, the model adds measurable value.** Top-6 outperforms random-6 by **+8 percentage points** on mean forward return and **+11.7 pp** on label hit rate, sustained across 16 of 17 months.
- **The model also actively avoids losers.** Bottom-6 underperforms random-6 by 9.87 pp on mean return and by 13.45 pp on hit rate. The probability rank is informative at both ends of the distribution.
- **The edge is mostly relative, not absolute.** Bull months float every bucket up. In December 2025 the bottom-6 still averaged +7.16% — high-tide effect. The model's value is in selecting the top of that tide, not in creating returns from nothing.
- **Sample warning.** 17 months of post-2024-12 data is short and mostly bullish. The model has not been observed through a true crypto bear (e.g. mid-2022). Treat the +25% bucket return as an upper anchor, not a steady-state.

---

## Part 2 — Regime-based profitability

### Regime definition

BTCUSDT monthly return (close on last bar of month ÷ close on first bar − 1), classified by symmetric ±5% thresholds:

| month | BTC ret | regime |
|---|---:|---|
| 2024-12 | −5.19% | bear |
| 2025-01 | +8.25% | bull |
| 2025-02 | −16.21% | bear |
| 2025-03 | −4.06% | chop |
| 2025-04 | +10.58% | bull |
| 2025-05 | +8.42% | bull |
| 2025-06 | +1.42% | chop |
| 2025-07 | +9.52% | bull |
| 2025-08 | −4.45% | chop |
| 2025-09 | +4.40% | chop |
| 2025-10 | −7.59% | bear |
| 2025-11 | −17.93% | bear |
| 2025-12 | +1.58% | chop |
| 2026-01 | −11.37% | bear |
| 2026-02 | −12.99% | bear |
| 2026-03 | +3.79% | chop |
| 2026-04 | +12.07% | bull |

Regime counts: bull = 5, chop = 6, bear = 6.

### Harness performance, exit-month → regime

Using `simulate_portfolio($1000 start, 6 concurrent, 80% deploy, 1× leverage)` against the 932 trades — same parameters `active_spec.json` declares.

**Monthly portfolio return:**

| month | equity end | monthly return | regime |
|---|---:|---:|---|
| 2025-04 | $1,432 | **+43.20%** | bull |
| 2025-05 | $1,989 | **+38.90%** | bull |
| 2025-06 | $1,842 | −7.38% | chop |
| 2025-07 | $2,551 | **+38.49%** | bull |
| 2025-08 | $4,127 | **+61.75%** | chop |
| 2025-09 | $3,798 | −7.97% | chop |
| 2025-10 | $4,470 | +17.69% | bear |
| 2025-11 | $5,461 | +22.17% | bear |
| 2025-12 | $6,686 | +22.43% | chop |
| 2026-01 | $8,830 | +32.07% | bear |
| 2026-02 | $7,659 | **−13.26%** | bear |
| 2026-03 | $8,130 | +6.15% | chop |
| 2026-04 | $21,785 | **+167.96%** | bull |
| 2026-05 | $32,122 | +47.45% | n/a (BTC data ends 2026-04-30) |

**Portfolio return per regime:**

| regime | months | mean | median | std | worst | best |
|---|---:|---:|---:|---:|---:|---:|
| bull | 4 | +72.14% | +41.05% | 63.92% | +38.49% | +167.96% |
| chop | 5 | +14.99% | +6.15% | 28.93% | −7.97% | +61.75% |
| bear | 4 | +14.67% | +19.93% | 19.56% | −13.26% | +32.07% |

Surprisingly, **bear regime is profitable on this dataset**. Three of four bear months returned +17 to +32%; only Feb 2026 was negative. The trailing-stop policy captures any individual-coin breakout, and several altcoins pump while BTC drifts down. **Caveat:** this is a 14-month window; a sustained, broad bear (every coin down, not just BTC) is not in the sample.

**Trade-level outcomes per regime** (entry month → regime, 30 May-2026 trades dropped — no BTC classification):

| regime | n_trades | win rate | mean net | median net | std | worst | best |
|---|---:|---:|---:|---:|---:|---:|---:|
| bull | 276 | **96.4%** | +8.92% | +4.37% | 19.78% | −41.42% | +221.26% |
| chop | 346 | 83.8% | +3.17% | +3.35% | 17.07% | −52.29% | +173.60% |
| bear | 280 | 82.5% | +3.55% | +3.75% | 17.68% | −53.83% | +119.32% |

The trade win rate (P&L positivity) compresses far less than monthly portfolio return — even in bear months, ~83% of trades close green. The reason is mechanical: Policy D's trailing stop guarantees that any trade that crosses +1% peak exits at a profit (stop = peak − 30% of peak-profit, which is always above entry once activated). Bull regimes simply have more trades activate and run further.

**Label hit rate per regime** (top-6, model accuracy):

| regime | n | label hit rate | mean max ret | median max ret |
|---|---:|---:|---:|---:|
| bull | 918 | **54.14%** | +50.20% | +11.29% |
| chop | 1,104 | 47.01% | +16.53% | +9.08% |
| bear | 1,056 | 37.22% | +12.17% | +6.01% |

The model's accuracy degrades cleanly with regime — 54% in bull, 47% in chop, 37% in bear — a 17 pp swing. Trade win rate barely moves over the same regimes (82.5% → 96.4%, a 14 pp swing but starting from a higher floor). Most of the regime sensitivity is absorbed by **mean trade magnitude**, not by win/loss count.

---

## Part 3 — Realistic expectations for paper trading

### Monthly portfolio return distribution

Over 14 observed months (`simulate_portfolio`, same params as `active_spec.json`):

| percentile | monthly return |
|---|---:|
| **p5** | **−9.82%** |
| p25 | +9.03% |
| **p50 (median)** | **+27.25%** |
| p75 | +42.13% |
| **p95** | **+98.93%** |

### Drawdown / negative-tail behaviour

| measure | value |
|---|---:|
| Worst single month | −13.26% (2026-02, bear) |
| Worst 2-month rolling cumulative | −7.11% (Feb−Mar 2026 area) |
| Worst 3-month rolling cumulative | **+24.96%** (i.e. no negative 3-month window in sample) |
| `simulate_portfolio` peak-to-trough DD | −23.73% |

Interpretation: in this 14-month window the strategy never had two consecutive losing months with combined damage worse than −7.1%. The full-curve maximum drawdown (−23.7%) was an intra-period peak-to-trough excursion. **This is a thin sample for tail-risk inference.**

### Hit-rate ranges (across regimes)

| metric | p5 | p25 | p50 | p75 | p95 |
|---|---:|---:|---:|---:|---:|
| Monthly **label hit rate** (model accuracy, top-6) | 32.0% | 37.5% | **42.5%** | 55.9% | 62.4% |
| Monthly **trade win rate** (P&L > 0 after costs) | 74.0% | 83.5% | **86.9%** | 90.6% | 99.0% |

**These two metrics are NOT comparable.** Label hit rate answers "did the asset rise ≥10% within 10 days?" (the model's training target). Trade win rate answers "did the trade close with positive net P&L after fees, slippage, funding?" — driven mostly by the trailing-stop mechanic, which guarantees a profitable exit once the trail arms at +1%. The 40+ pp gap between them is structural, not anomalous.

### Headline single-number expectation

> **Realistic median monthly portfolio return: +27%.**
>
> **Realistic spread (p5–p95): −10% to +99%.**
>
> **Realistic 1-month worst case: −13%. 2-month worst rolling: −7%. Full-curve max DD anchor: −24%.**
>
> Expect ~85% of trades to close green, ~42% of top-6 picks to satisfy the +10% label.

These numbers assume a broadly bullish-to-neutral crypto tape with periodic altcoin runups (the 17-month sample). They will **not** hold in a sustained broad bear; the sample contains no such window.

---

## Part 4 — Plain-English summary

### What the edge is

The model ranks coins each day by probability of clearing +10% within 10 days. The top 6 picks beat random-6 picks by **+8 percentage points** on mean forward return and **+11.7 pp** on +10%-label hit rate, sustained across 16 of 17 months. The bottom 6 picks underperform random by ~10 pp — so the probability rank is informative at both ends. **That's the alpha:** picking better than chance among the binance perp top-50.

The model is not magic. It does not turn losing tapes into winning tapes. In a flat or down month, the model's top-6 still average lower returns than in an up month — just *less* low than random-6 in the same tape. Think "skim the top of whatever tide", not "make the tide".

### What the edge isn't

The 87% "expected hit rate" in `active_spec.json` is **not** the model's accuracy. It's the fraction of harness trades that closed with positive net P&L — a metric dominated by the trailing-stop exit policy, which guarantees a profitable exit any time a trade crosses +1% above entry. The model's actual prediction accuracy (top-6 picks reaching +10% within 10d) is **~42–48% in the median month, 32–62% across the regime spread.** Don't conflate the two.

### What "hit" and "miss" mean — depending on context

| You care about | Use this metric | Typical value | What it tells you |
|---|---|---|---|
| Did the model pick well? | Label hit rate (`actual_hit` on top-6) | ~42% median, 32–62% range | Whether the model is still ranking coins accurately |
| Did the trade make money? | Net P&L > 0 (trade win rate) | ~87% median, 74–99% range | Whether the trailing-stop is doing its job once the model picks |
| Did the strategy earn? | Monthly portfolio return | +27% median, −10% to +99% range | Overall outcome, what hits your equity curve |

When monitoring paper trading, watch **all three** independently. They move on different timescales for different reasons:
- Label hit rate degrades when the **model** is stale (regime shift the model wasn't trained for, universe drift, fundamentals change).
- Trade win rate degrades when the **execution** is broken (slippage worse than modelled, funding rates spike, exchange outages, missed exits).
- Portfolio return degrades when **either** of the above breaks, or when sizing/concurrency assumptions don't hold live.

### What to expect monthly during paper trading

In a typical month:
- The model's label hit rate will be somewhere in **32–62%** — wide range. Don't panic if it's 35% in a bear month; it was 37% in the bear months of the backtest sample.
- The trade win rate will be somewhere in **74–99%**, usually 83–90%. Bull months push toward the upper end; bear months don't drop as much because the trailing mechanic carries it.
- The portfolio return will be somewhere in **−10% to +99%**, with median ~+27%. Big swings are normal. A −13% month is the worst observed; a −24% peak-to-trough drawdown is the full-curve worst.

### Red flags (vs. normal variance)

| Red flag | Why it's bad | Investigation |
|---|---|---|
| Monthly label hit rate < 30% for 2+ consecutive months | Model accuracy below worst-observed in sample | Check input feature distribution drift, model staleness, universe changes |
| Monthly trade win rate < 70% for any month | Below 5th percentile in sample (74%) | Execution issue: check slippage, funding cost outliers, missed exits, partial fills |
| 2+ consecutive months portfolio return < −7% combined | Worse than worst-observed 2-month rolling | Both model AND execution; consider pausing |
| Single month < −20% | Worse than worst-observed (−13%) | Stop and investigate before resuming |
| `pct_exits_time > 25%` for a month | Normal range was 0–25% (Feb-26 hit 31% during the worst month) | Trailing stop not arming → model picks aren't getting +1% breakouts → likely regime change |
| `pct_exits_trailing` collapsing toward zero | The strategy's profit engine is failing | Critical: trail is the only winning exit path |

### Normal variance (don't react)

- A single month at −10% portfolio return — within p5 band.
- Label hit rate of 35–40% in a bear or chop month — observed historically.
- Trade win rate of 78–84% in any regime — within normal spread.
- A 2-week stretch of mostly time-exits (no trailing) — expect periodic small losses when the tape goes sideways.

### How to act on this

1. **Treat `expected_hit_rate=0.871` in the spec as a trade-execution number, not a model-accuracy number.** Either rename it in `INTERFACE.md` + `active_spec.json` (coordinated change with the engine repo) or add a separate `expected_label_hit_rate≈0.48` field. Both repos need to know which metric is being monitored.
2. **For paper-trading expectations, use the median +27% monthly with a published p5–p95 of −10% to +99%.** Anchor stop conditions to the bottom of the band, not the median.
3. **Don't re-run Phase 1B for this reason.** The backtest math is internally consistent — only the field naming and operator interpretation needed to change.
4. **Track regime explicitly in production.** When BTC's monthly return crosses into "bear", expect 12–20 pp lower label hit rate and roughly 14% mean monthly portfolio return (with a meaningful chance of a single-digit negative month). When it's "bull", be cautious about over-extrapolating recent gains — bull months in this sample averaged +72% monthly, which is unrealistic to sustain.
5. **Watch the trail-activation rate as the canary.** If `pct_exits_trailing` drops materially while `pct_exits_time` rises, the strategy is degrading even if the headline hit rate looks OK.

---

## Methodology notes

- **Random-6 baseline.** For each prediction date, sampled `min(6, day_universe_size)` symbols uniformly without replacement. Deterministic seed `20260510`. Same `actual_max_return` and `actual_hit` columns as top-6, so comparison is apples-to-apples on label outcome.
- **Regime classification.** BTCUSDT monthly return computed as `close.resample("ME").last() / close.resample("ME").first() − 1`. Thresholds ±5% are conventional crypto regime buckets, not optimised against the trade outcomes. Sensitivity to the threshold choice was not tested.
- **Portfolio simulation.** Used `crypto.execution.backtest.report.simulate_portfolio` with the same parameters `write_active_spec.py:117-121` uses: $1000 start, max 6 concurrent, 80% deploy, 1× leverage. `n_trades_taken=484, n_trades_skipped_capacity=448` — about half the harness's 932 trades couldn't fit into the concurrent-position cap, which is also an alpha-leakage source.
- **Span / annualization.** Span = 398 days from first entry (2025-04-06) to last exit (2026-05-09). Annualized return = total_return × 365/span, then ×100 → 2854%. Mathematically correct, practically untrustworthy: compounding 14 months that include a +168% April 2026 outlier projects an unrealistic forward number.
- **Sample limitations.** 14 months of trading, 17 months of predictions, no period of broad sustained bear, no leverage events, no exchange-outage simulation, no live-market slippage variance. The numbers above describe what this specific sample produced; they do not bound what live trading will produce.
- **No code was changed** by this analysis. All artefacts are read-only.

---

## Appendix — Files

| Path | Purpose |
|---|---|
| `.claude/local_scripts/strategy_edge_analysis.py` | Reproducible analysis script (this report). |
| `.claude/local_scripts/investigate_phase1b_hitrate.py` | Prior investigation: confirms expected_hit_rate is P&L positivity, not label accuracy. |
| `.claude/local_scripts/_strategy_dump/` | CSV + JSON intermediates (monthly_compare, monthly_portfolio, regime_*). |
| `data/exports/active_spec.json` | Current spec referenced by the crypto-trading-engine. Field `expected_hit_rate=0.871` is the metric flagged for renaming or augmentation. |
| `crypto/execution/backtest/metrics.py:252-254` | Where `summary.hit_rate` (net-P&L hit rate) is computed. |
| `crypto/ml/backfill_walkforward.py:308` | Where `actual_hit` (label hit rate) is computed. |
| `crypto/exports/write_active_spec.py:122-139` | Where `summary.hit_rate` is mapped into `expected_hit_rate`. |
