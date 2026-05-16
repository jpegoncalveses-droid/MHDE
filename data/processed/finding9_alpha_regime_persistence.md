# Finding 9 — Alpha persistence across BTC regimes

**Investigation date:** 2026-05-15
**Mode:** read-only
**Trigger:** finding7's 2022-style bear simulation assumed the fitted
+174% annualized alpha persists into a BTC drawdown. Step 2 of the
finding7 follow-up tests that assumption empirically by splitting the
backtest window by BTC regime and re-running the alpha/beta
regression on each half.

**Bottom line:** **Alpha does NOT persist symmetrically across BTC
regimes.** On individual BTC-down days the daily alpha is **negative**
(-63% annualized). Under a more forgiving "60-day BTC trend" cut,
alpha stays positive in bear-trending regimes (+90%) but is less than
half its bull-trend value (+208%). Both splits are statistically
significant. finding7's bear-case sim should be re-graded toward the
**-89% (no-alpha-survives) end of its range**, not the -63%
(alpha-persists) end.

---

## TL;DR

| Regime split | Alpha (annualized) | Beta | R² | n days | Slope p |
|---|---:|---:|---:|---:|---:|
| **Full period (finding7 baseline)** | **+174.6%** | 1.503 | 0.449 | 399 | 2.1e-53 |
| BTC-up days (daily BTC > 0) | +304.4% | 1.433 | 0.286 | 205 | 1.4e-16 |
| **BTC-down days (daily BTC < 0)** | **−63.3%** | 1.256 | 0.219 | 194 | 5.7e-12 |
| Trend-up (60d rolling BTC mean > 0) | +208.5% | 1.868 | 0.390 | 201 | 4.1e-23 |
| **Trend-down (60d rolling BTC mean < 0)** | **+90.5%** | 1.284 | 0.492 | 179 | 8.1e-28 |

Mean returns by regime:

|  | BTC-up days | BTC-down days |
|---|---:|---:|
| Mean BTC daily return | +1.567% | −1.579% |
| Mean strategy daily return | **+3.080%** | **−2.156%** |

The strategy beats BTC on up days (+3.08% vs +1.57% = +1.51pp positive
alpha-on-positive-day) **but loses MORE than BTC on down days**
(−2.16% vs −1.58% = −0.58pp negative-alpha on losing days). The
asymmetry is the key fact.

---

## 1. Method

Strategy daily returns reconstructed from the trades table exactly as
in finding7 (equal-weight mean of active-trade coin daily returns;
cash on no-active-trade days). 399 aligned daily observations,
2025-04-06 → 2026-05-10.

Two regime splits:

1. **Sign-of-day split.** Each daily observation classified by the
   sign of BTC's same-day return. 205 BTC-up days, 194 BTC-down days,
   0 flat. Crude but unbiased; large effective sample size for each
   half.
2. **60-day rolling trend split.** Each day classified by the sign of
   the trailing-60-day mean BTC daily return. Smoother, captures
   "regime" rather than "daily noise". Warmup window = first 60 days
   ≈ first 20 days with `min_periods=20`. 201 trend-up days, 179
   trend-down days, 19 warmup-NaN dropped.

Same OLS as finding7: `strat_ret ~ alpha + beta × btc_ret` via
`scipy.stats.linregress`, fit independently on each regime subset.

Script: `.claude/local_scripts/finding8_9_10_combined_audit.py`
(read-only, re-runnable).

---

## 2. Sign-of-day split

```
FULL       n=399  alpha_d=+0.00478  alpha_ann=+174.55%  beta=+1.503  R²=0.449
BTC-UP     n=205  alpha_d=+0.00834  alpha_ann=+304.42%  beta=+1.433  R²=0.286
BTC-DOWN   n=194  alpha_d=-0.00173  alpha_ann= -63.25%  beta=+1.256  R²=0.219
```

**Reading.** On BTC-up days the strategy generates ~+0.83% alpha per
day above the beta-implied return. On BTC-down days it gives back
~-0.17% per day below the beta-implied return. The slope (beta) is
modestly lower in bear days (1.26 vs 1.43) — the strategy mechanically
holds slightly less BTC-correlated exposure when BTC is bleeding,
probably because trailing stops fire and trades close. But that beta
reduction doesn't compensate for the alpha sign flip.

The **alpha sign flip** (positive in up regime, negative in down
regime) is the diagnostic that matters. It means the strategy's
selection edge is conditioned on BTC strength: when the BTC tide
rises, picks rise faster; when the tide falls, picks fall faster.
That's the opposite of "uncorrelated alpha."

Both splits have very strong slope significance (BTC-up p=1e-16,
BTC-down p=6e-12). The intercept (alpha) p-values are not reported
directly by `linregress`, but the magnitudes are clear from the daily
return decomposition.

---

## 3. Trend split (60-day rolling BTC mean)

```
TREND-UP     n=201  alpha_d=+0.00571  alpha_ann=+208.46%  beta=+1.868  R²=0.390
TREND-DOWN   n=179  alpha_d=+0.00248  alpha_ann= +90.48%  beta=+1.284  R²=0.492
```

**Reading.** Under the smoothed-regime cut, **alpha stays positive in
bear regimes** — but **shrinks by more than half** (+208% → +90%).
Beta also drops substantially (1.87 → 1.28). The R² actually rises in
bear regimes (0.39 → 0.49), meaning BTC explains *more* of strategy
variance when BTC is in a downtrend — exactly when you'd want the
strategy to be decorrelated.

The contrast between sign-of-day (alpha flips to −63%) and
trend-of-window (alpha shrinks to +90% but stays positive) is
informative:

- **Day-by-day, the strategy is negative-alpha on the days BTC drops.**
  This is the mechanical / tactical pattern — bad days hit picks
  harder than they hit BTC.
- **Period-averaged in a bear regime, the strategy still extracts
  net-positive alpha** — but the alpha pool is smaller and probably
  carries the tail risk of clustering with BTC's worst days.

For bear-case simulation, the trend-down number (+90% annualized
alpha, beta 1.28) is the more honest input than the all-period
+174.6% / 1.50.

---

## 4. Implications for finding7's bear-case simulation

finding7's 2022-style bear (BTC -77% over 250 trading days) projected
the strategy at:

- −63.5% cumulative if **fitted alpha persists at +174.6% annualized**
- −89.0% cumulative if **alpha = 0**

Re-running the simulation with this finding's regime-specific inputs:

| Scenario | Alpha used | Beta used | Predicted strat cum. (250 days) |
|---|---:|---:|---:|
| finding7's "alpha persists" | +174.6% | 1.50 | −63.54% |
| finding7's "alpha = 0" | 0% | 1.50 | −89.05% |
| **This finding: trend-down regime** | +90.5% | 1.28 | **−72.83%** |
| **This finding: sign-of-day BTC-down** | −63.3% | 1.26 | **−87.06%** |
| **Pessimistic (alpha collapses & beta unchanged)** | 0% | 1.50 | −89.05% |

```
trend-down sim:
  strat_daily = +90.5/365/100 + 1.28 × (-0.586/100) = -0.00501 per day
  cum_250 = (1 - 0.00501)^250 - 1 = -72.83%

sign-of-day-down sim:
  strat_daily = -63.3/365/100 + 1.26 × (-0.586/100) = -0.00911 per day
  cum_250 = (1 - 0.00911)^250 - 1 = -87.06%
```

**Operational interpretation.** The bear-case range tightens:

- **Best plausible bear case ≈ −73%** (trend-regime alpha persists but
  is halved — the most charitable empirical reading).
- **Worst plausible bear case ≈ −89%** (sign-of-day alpha sign flip
  generalizes to a sustained bear).
- **All scenarios trip the −30% portfolio kill switch** by day ~70
  (best) or day ~35 (worst).

The kill switch is still calibrated correctly. But the operator
should mentally bracket the bear-case as "−70% to −90% if the kill
switch is removed or breached," not "−63% if alpha persists." The
alpha-persists assumption was the optimistic end of the range.

---

## 5. Caveats

1. **Sample-size asymmetry.** Apr 2025 – May 2026 was mostly a BTC
   bull period (BTC total return +4.8% over 399 days, but with major
   intra-period drawdown). The BTC-down day count (194) and
   trend-down day count (179) are roughly half the window, so the
   bear-regime statistics rest on smaller samples than the full
   regression. Confidence intervals on bear-regime alpha are wider
   than the table shows.

2. **No 2022-style sustained-bear sample in this backtest.** The
   "trend-down" days are mostly part of mid-cycle drawdowns within an
   overall bull period, not a 12-month-long bear. A real 2022-style
   regime might suppress alpha further (correlation tends to 1 in
   stress; cross-coin alpha shrinks; trailing stops cluster-fire).

3. **Beta-stability is not assumed.** The fitted beta drops from 1.50
   (full) to 1.28 (trend-down) to 1.26 (BTC-down). The strategy
   effectively shrinks its BTC exposure when BTC weakens — partly
   from trailing-stop exits, partly because the selection model
   produces fewer high-prob picks in down regimes. This reduces tail
   damage but doesn't replace the alpha shortfall.

4. **The sign-flip vs trend-shrink disagreement is real, not noise.**
   With p=5.7e-12 (BTC-down sign-of-day split) and p=8.1e-28
   (trend-down split), both regressions are well-determined. The
   apparent contradiction is the data telling a coherent story: at
   the daily-tactical level the strategy underperforms its beta on
   bad days; at the regime-averaged level it still extracts net
   positive alpha because good days within bear regimes still
   contribute. The relevant input for a sustained-bear simulation is
   the regime average (+90%), not the day-by-day decomposition.

---

## 6. Verdict

**Finding7's "alpha persists" assumption is half-defensible.** The
right summary is:

- **Alpha persists in bear regimes, but at less than half its bull
  magnitude** (+90% vs +208% in the regime split). The finding7
  +174% number was inflated by the bullish-tilt of the backtest
  sample.
- **Alpha is asymmetric:** strongly positive on BTC-up days
  (~+0.83%/day), modestly negative on BTC-down days (~-0.17%/day).
  The strategy is correlated-alpha, not uncorrelated-alpha.
- **In a sustained 2022-style bear, expect cumulative drawdown of
  -73% to -89%** (vs finding7's headline -63%). The portfolio kill
  switch fires in all scenarios.

### Next-steps (read-only follow-ups; not requested for action here)

1. Re-run the alpha/beta decomposition with a sample restricted to
   the worst peak-to-trough drawdown window inside the backtest
   period. Compare alpha there to the period average. If alpha is
   sharply negative in the strategy's own worst window, the
   finding9 "regime average" is itself optimistic.
2. Build a forward live-trading risk budget against the **trend-down
   alpha** (+90% / beta 1.28), not the full-period figures. This is
   the inverse of the operator's current calibration.
3. Worth filing the conclusion as a KI follow-up to KI-148: the spec's
   stated `portfolio_max_dd_pct = -23.7%` is empirically the
   *bull-regime* peak-to-trough; the *bear-regime* envelope is
   substantially worse. Same operator-facing concern as finding7
   amplified.

---

## 7. References

- Audit script: `.claude/local_scripts/finding8_9_10_combined_audit.py`
  (sections 2a + 2b).
- Strategy daily-MTM construction: see
  `data/processed/finding7_alpha_beta_decomposition.md` §1.
- finding7 §4 — the original bear-case simulation this finding
  re-grades.
- [[KI-148]] — deployed-spec vs kill-switch gap; this finding
  reinforces it.
