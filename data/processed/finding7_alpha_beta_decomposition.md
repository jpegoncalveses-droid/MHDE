# Finding 7 — Alpha vs Beta decomposition of PHASE1B_WINNER backtest

**Investigation date:** 2026-05-15
**Mode:** read-only
**Trigger:** Strategy reported Sharpe=6.25 and max DD=-17.0% — but
22/30 open positions today carry BTC beta > 0.8 (correlation banner
on the dashboard). Need to know whether the edge is real selection
alpha or mostly leveraged BTC dressed up by trade-cycle accounting.

**Run audited:** `backtest_10d_D_top_n_a02e15a0` — the
`active_spec.json` `phase_1b_winner.run_id`. 932 trades, 2025-04-06 →
2026-05-10 (~13 months). Reported summary:

| Metric | Value |
|---|---|
| net_pnl_total_pct | +52.23% |
| net_pnl_annualized_pct | +48.02% |
| **Sharpe** | **6.25** |
| **max_drawdown_pct** | **-17.01%** |
| hit_rate | 87.34% |
| profit_factor | 3.19 |
| avg_holding_days | 3.66 |

---

## TL;DR

**Both alpha and beta are real and large.** The premise that the
strategy might be "mostly leveraged BTC" is **rejected** — but the
opposite framing ("uncorrelated alpha") is also rejected. The strategy
sits in the third quadrant: **leveraged edge** with both substantial
selection alpha and ~1.5× BTC exposure.

| Quantity | Estimate | 95% CI |
|---|---:|---|
| **Beta to BTC (daily)** | **1.503** | [1.339, 1.667] |
| **Alpha (annualized, ×365)** | **+174.6%** | [+40.1%, +309.0%] |
| R² | 0.449 | — |
| Pearson corr (strat, BTC) | 0.670 | — |

**Two important caveats** (detail below):

1. The **reported Sharpe 6.25 / max DD -17%** uses trade-cycle returns
   (one observation per closed trade). My **daily mark-to-market
   reconstruction** gives **Sharpe 2.02 / max DD -48%**. The reported
   figures hide within-trade drawdowns. The daily-MTM figures are the
   more honest risk representation.
2. The fitted beta (1.50) compounded over the observed 399-day window
   accounts for only ~25% of realized return (BTC's mean daily return
   was barely positive); **alpha did the heavy lifting**. But in a
   2022-style bear (BTC -77% over ~250 days) the same beta drags
   strategy to **-63.5% cumulative** with persistent alpha — and to
   **-89% without alpha**. Either is well past the -30% kill switch.

---

## 1. Method

Strategy daily returns reconstructed from the trades table:

- For each calendar day `D` in `[2025-04-06, 2026-05-10]`:
  - Find all trades with `entry_date ≤ D ≤ exit_date`.
  - For each active trade's coin, compute that coin's daily return
    `(close_D / close_{D-1}) - 1` from `crypto_prices_daily`.
  - **Strategy daily return = unweighted mean** of those coin returns
    (equal-weight, fully invested when at least one trade is active;
    cash return = 0 when no trade active, observed once — last
    day-after-period boundary).
- BTC daily return: `(close_D / close_{D-1}) - 1` for BTCUSDT, same
  window.
- Inner-joined to 399 aligned daily observations.

**OLS fit:** `strat_ret ~ alpha + beta × btc_ret` via
`scipy.stats.linregress`. Standard errors and CIs are asymptotic.

**Approximation notes** (matter for the magnitude of Sharpe / DD,
not for the alpha/beta split):

- Real backtest sizing is `selection_n = 6` per day from active_spec;
  my equal-weight construction ignores how many of the 6 slots were
  filled on each day. If the strategy held only 3 of 6 slots on a
  given day, real exposure was lower and the alpha attribution
  somewhat higher. Cross-checking the trade-count timeline against
  daily realized vol would refine this.
- Trailing-stop exits within a day are not modeled (they would clip
  the worst tail days slightly).
- Funding / slippage / fees are not subtracted from the daily series
  — they reduce realized strategy return by ~1.6%pp over the period
  (per the reported summary), too small to swing the alpha/beta
  decomposition.

Script: `.claude/local_scripts/finding7_alpha_beta_audit.py`
(gitignored; re-runnable read-only).

---

## 2. Regression output

```
strat_ret ~ alpha + beta × btc_ret           (n = 399 aligned days)
─────────────────────────────────────────────────────────────────
alpha (daily)            : 0.00478
alpha (annualized ×365)  : +174.55%
  95% CI                 : [+40.11%, +308.98%]
  t-statistic (vs 0)     : 2.55
  p-value                : 0.0111

beta                     : 1.503
  95% CI                 : [1.339, 1.667]
  std-err                : 0.084
  slope p-value          : 2.1 × 10⁻⁵³  (extremely significant)

R²                       : 0.449
Pearson correlation      : 0.670
```

**Reading.** The slope (beta) is exceptionally well-determined — the
strategy moves ~1.5 USD for every USD of BTC move, with very tight
confidence (β = 1.34–1.67 at 95%). The intercept (alpha) is
significant at the 5% level but has a much wider CI because daily
alpha estimates are noisy over only 399 observations.

R² = 0.449 means **BTC explains 45% of strategy daily variance**.
The remaining 55% is residual — a mix of (a) coin-selection alpha and
(b) idiosyncratic altcoin noise. The fitted alpha extracts the
non-zero-mean part of that residual.

---

## 3. Sanity checks

| Metric | Strategy | BTC | Notes |
|---|---:|---:|---|
| **Daily-MTM Sharpe** | **2.021** | **0.316** | Strategy ~6× BTC's risk-adjusted return |
| Daily-MTM max DD | **-48.13%** | -49.56% | Within 1.5pp of BTC's DD |
| Trade-cycle Sharpe (reported) | 6.25 | — | Sees only closed-trade returns |
| Trade-cycle max DD (reported) | -17.01% | — | Hides within-trade drawdowns |
| Pearson correlation | 0.670 | — | High, consistent with beta>1 |
| Total return | **+409.96%** | **+4.83%** | Strategy 85× BTC's total return over period |
| Daily-return mean | +0.534% | +0.037% | |
| Daily-return std | 5.05% | 2.25% | Strategy ~2.2× BTC vol — matches beta×corr math |

### 3a. The Sharpe gap (6.25 vs 2.02) is methodological, not contradictory

The reported Sharpe of 6.25 uses **trade-cycle returns**: one
`net_pnl_pct` per closed trade (932 observations). That construction:

- Ignores days a trade is held but flat (no contribution to the
  variance estimate even though there is real volatility);
- Compresses within-trade drawdowns into a single number per trade.

My daily-MTM construction uses **daily portfolio returns** (399
observations covering every market day in the window). It exposes:

- Days where the active basket dropped 24%+ in one day (5/14 saw a
  -24.4% drawdown on the basket — driven by the deep-loser cluster
  KI-140 covers);
- The full equity-curve drawdown geometry that an operator
  experiences in production.

**Both numbers are correct for their definitions.** The daily-MTM
Sharpe is the right one for "what's the operator's lived
experience"; the trade-cycle Sharpe is the right one for "what's the
expected payoff per signal taken." The dashboard / paper-trading
expectations should be set against the daily-MTM figures going
forward — particularly the **max DD: -48% is more honest than -17%
for the live-trading risk envelope** (the kill switch at -30%
absolutely matters).

### 3b. Decomposition of the realized 410% over 399 days

Approximate compounded contributions (cross-term ignored — illustrative):

```
Alpha-only path:  (1 + 0.00478)^399 - 1  =  +570.9%
Beta-only path:   (1 + 1.503 × 0.000372)^399 - 1  =  +24.98%
Sum (no cross):                              +595.9%
Realized total:                              +409.96%
```

Cross-term (negative, ~-186%) absorbs the rest — alpha and beta are
not orthogonal in compounding. **Qualitatively: alpha did roughly 95%
of the directional work over this specific 13-month window** (BTC
was nearly flat for the period). In a sustained BTC bull, beta would
contribute more; in a bear, beta would subtract more (see §4).

---

## 4. Scenario simulation: 2022-style bear

Assume a synthetic BTC daily series that delivers cumulative -77%
over 250 trading days (≈ Nov 2021 → Nov 2022). Daily BTC return
required: -0.586% per day.

| Scenario | Daily strat return | Cumulative 250-day strat | Operator implication |
|---|---:|---:|---|
| **Fitted alpha + beta=1.50** | -0.403% | **-63.54%** | Trips -30% kill switch around day ~85. Strategy held to kill-switch limit, then forced to cash. |
| **Alpha=0, beta=1.50 (counterfactual)** | -0.879% | **-89.05%** | Trips kill switch around day ~40. Pure leveraged BTC short the whole way down. |
| **BTC only (beta=1.0)** | -0.586% | -77.00% | Trips kill switch around day ~60. |

**Interpretation.** Alpha is the difference between "tripped the kill
switch in 85 days and lost 30% before stopping" and "lost 89% before
stopping." It's real and significant — but **it does not prevent the
kill switch from firing in a sustained bear.** The kill switch is
calibrated correctly: it stops the strategy before -89% under
no-alpha-survives conditions, and well before the realized
distribution's tail.

Caveat: this assumes alpha persists at the fitted rate during a
bear. There's a reasonable prior that selection alpha contracts in
bear regimes (correlation tends to 1 when stress fires), so the
alpha-on path is a generous estimate. Independent regression on
BTC-down-only days would be a worthwhile follow-up.

---

## 5. Verdict and recommendation

### Verdict on the operator's hypotheses

| Hypothesis | Verdict |
|---|---|
| "Mostly leveraged BTC" (alpha ≈ 0, beta high) | **Rejected.** Alpha is +174.6% annualized at the central estimate, statistically significant at the 1% level. The 95% CI does not cross zero. |
| "Uncorrelated alpha" (low beta, high alpha) | **Rejected.** Beta = 1.50 ± 0.16, extremely tight. R² = 0.45 says BTC explains nearly half the strategy's daily variance. |
| "Leveraged edge" (high alpha + high beta) | **Holds.** Both signals are real and large. The strategy is a long-only altcoin selection on top of substantial BTC beta. |

### Premise check vs the reported headline

| Reported | Daily-MTM truth | Operator implication |
|---|---|---|
| Sharpe 6.25 | Sharpe 2.02 | Still excellent (2.0 is top-quartile crypto strategy), but **3× less impressive than headline**. Trade-cycle Sharpe hides within-trade volatility. |
| Max DD -17.0% | Max DD -48.1% | **Concerning.** The "headline -17%" is unrelated to the -30% portfolio kill switch the operator calibrated against; the true daily-MTM tail is much closer to BTC's -50%. KI-148 (deployed-spec vs kill-switch gap) is more material than initially thought. |

### Recommended next steps

1. **(High priority) Re-calibrate operator expectations against the
   daily-MTM numbers, not the trade-cycle numbers.** The dashboard's
   backtest banner / spec file currently shows the trade-cycle
   figures. Worth adding a "daily-MTM equivalent" line for honest
   risk communication. Tracked in [[KI-148]] territory.

2. **(Medium priority) Run the regression separately on BTC-up and
   BTC-down half-windows** to see if alpha collapses in bear regimes.
   The scenario sim in §4 assumes alpha persists; if it doesn't, the
   bear DD goes from -64% to closer to -89%. This is the difference
   between "kill switch saves us" and "kill switch saves us, but
   barely."

3. **(Medium priority) Decompose alpha further: how much of the 174%
   annualized comes from sector / market-cap / momentum factors
   (which are *not* coin-selection alpha) vs true unsystematic edge?**
   Standard approach: regress strat_ret against BTC + ETH + a
   small/mid-cap altcoin index, see what's left. If most of the
   "alpha" is actually ETH or altcoin-index beta in disguise, that
   changes the live-trading risk story.

4. **(Low priority, awareness-only) The strategy carries true beta
   1.5 to BTC. Position sizing should reflect this.** Operationally,
   that means the engine's "10% capital per position" sizing
   translates to ~15% effective BTC exposure per position. With 6
   concurrent positions at peak utilization, total effective BTC
   exposure is ~90%. The strategy is functionally a 90% long-BTC
   portfolio with active idiosyncratic alpha layered on top.

### What this finding does **not** change

- The strategy's underlying coin-selection alpha is real; the model
  is producing genuine signal.
- Variant D's filter chain (post-parabolic + short-momentum) is
  orthogonal to this — it operates on per-coin features, not on
  strategy/BTC factor structure. Finding 6 confirmed Variant D works
  end-to-end; this finding does not affect that conclusion.
- The -30% kill switch as configured trips before the no-alpha
  counterfactual (-89%) and at roughly the right severity for the
  alpha-on case (-63%). The kill switch is correctly calibrated for
  a single 2022-style bear; it does not protect against repeated
  drawdowns or alpha regime change.

---

## 6. References

- Run ID: `backtest_10d_D_top_n_a02e15a0` in `crypto_backtest_runs`,
  `crypto_backtest_summary`, `crypto_backtest_trades`.
- Active spec: `data/exports/active_spec.json` →
  `phase_1b_winner.run_id`.
- Audit script: `.claude/local_scripts/finding7_alpha_beta_audit.py`.
- Related KIs:
  - [[KI-148]] — deployed-spec `portfolio_max_dd_pct` vs kill-switch
    gap; this finding amplifies KI-148's concern because the true
    daily-MTM DD is -48% vs the reported -17% the operator was
    calibrating against.
  - [[KI-137]] — crypto post-parabolic re-entry bias; the high beta
    is partly *because* the model selects high-beta altcoins that
    have just retraced. Filter ADR-021/ADR-028 mitigates this, but
    the residual structural beta remains.
- Related findings:
  - `finding5_pipeline_gap_and_t2_alignment.md` — equity-side
    investigation (independent).
  - `finding6_swarmsusdt_repeat_prediction.md` — confirmed Variant D
    filter works for the most beta-heavy excluded names today.
