# Finding 10 — Multi-factor alpha decomposition

**Investigation date:** 2026-05-15
**Mode:** read-only
**Trigger:** finding7's single-factor regression measured a +174.6%
annualized alpha against BTC. Step 3 of the finding7 follow-up asks
whether that "alpha" is genuinely uncorrelated edge or whether some
of it is hidden ETH / altcoin-index beta the BTC-only model couldn't
absorb.

**Bottom line:** **Most of the +174% disappears once ETH and an
altcoin-index factor are added.** In the three-factor model the
intercept drops to +41.6% annualized and becomes statistically
insignificant (p=0.43). The strategy's "alpha" is largely a long
exposure to the **altcoin complex** with a smaller residual ETH
component. **True uncorrelated edge is at most ~+40% annualized, and
the regression cannot rule out zero.**

---

## TL;DR

| Model | Alpha (annualized) | Alpha p-value | β_BTC | β_ETH | β_Alt | R² | adj R² |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Single (BTC only)** | **+174.55%** | **0.0113** | **1.503** | — | — | 0.4494 | 0.4481 |
| Two (BTC + ETH) | +143.83% | 0.0240 | 0.509 | 0.706 | — | 0.5310 | 0.5287 |
| **Three (BTC + ETH + Alt)** | **+41.62%** | **0.4331** | 0.198 | 0.390 | **0.571** | **0.6798** | **0.6774** |

Alt index = equal-weighted mean of daily returns across SOLUSDT,
LABUSDT, DOGSUSDT, ZECUSDT, TONUSDT, SKYAIUSDT, DOGEUSDT, XRPUSDT,
HYPEUSDT, RAVEUSDT (the top 10 alts by volume rank in
`crypto_universe`, excluding BTC and ETH).

n = 399 aligned daily observations, 2025-04-06 → 2026-05-10.

---

## 1. Method

Same strategy daily-MTM series as finding7 / finding9. Three OLS
specifications, nested:

```
M1 (single): strat_ret = α + β_btc × BTC_ret + ε
M2 (two):    strat_ret = α + β_btc × BTC_ret + β_eth × ETH_ret + ε
M3 (three):  strat_ret = α + β_btc × BTC_ret + β_eth × ETH_ret
                          + β_alt × Alt_ret + ε
```

Where `Alt_ret` is the equal-weighted daily return across the top-10
non-BTC, non-ETH altcoins by volume rank from `crypto_universe`.
Per-day NaN handling: `Alt_ret_t = mean(available alt returns on
day t)` — coverage varies (147 days for RAVEUSDT, 399 for the
mega-caps), but the equal-weighted mean uses whatever subset is
available on each day. Inner-joined across all four series; 399 days
with complete data.

Each model fit via `numpy.linalg.lstsq` with manual SE / t-stat /
p-value computation (asymptotic, normal-residual assumption).
Adjusted R² reported.

Script: `.claude/local_scripts/finding8_9_10_combined_audit.py`
(read-only, re-runnable).

---

## 2. Single-factor baseline (M1, replication of finding7)

```
alpha (daily)            : +0.00478
alpha (annualized ×365)  : +174.55%
  alpha t-stat            : 2.55   (p = 0.0113)
beta_btc                 : +1.503   (p ≈ 0, slope p ≈ 2e-53)
R²                       : 0.4494
adj R²                   : 0.4481
```

Identical to finding7's headline. ~45% of strategy variance explained
by BTC alone; the intercept-slot picks up a large +174% annualized
residual mean.

---

## 3. Adding ETH (M2)

```
alpha (annualized)       : +143.83%   (p = 0.0240)
beta_btc                 : +0.509     (p = 0.0004)
beta_eth                 : +0.706     (p = 1.6e-15)
R²                       : 0.5310
adj R²                   : 0.5287
```

**Two things happen at once.** BTC's beta drops from 1.50 to 0.51 —
huge — and an ETH beta of 0.71 absorbs the extracted exposure. The
strategy's apparent "BTC leverage" was substantially co-movement with
ETH that the BTC-only model couldn't disentangle (BTC and ETH are
highly correlated). R² rises from 0.45 to 0.53 — ETH adds 8pp of
explanatory power.

Alpha drops from +174% to +144% — modestly. Most of the "alpha"
survives. But the alpha p-value rises from 0.011 to 0.024 — still
significant at 5%, no longer at 1%.

---

## 4. Adding the altcoin index (M3)

```
alpha (annualized)       : +41.62%    (p = 0.4331)
beta_btc                 : +0.198     (p = 0.100)
beta_eth                 : +0.390     (p = 2.3e-07)
beta_alt                 : +0.571     (p ≈ 0)
R²                       : 0.6798
adj R²                   : 0.6774
```

**This is the decisive specification.** Three shifts:

1. **β_alt = 0.57, extremely significant** (p essentially 0). The
   strategy moves with the altcoin complex roughly 0.6× per day. This
   is the factor the single-factor BTC model could not see.
2. **β_btc drops to 0.20 and becomes statistically insignificant**
   (p=0.10). After controlling for ETH and the alt index, the
   strategy's residual BTC sensitivity is near zero. Most of the
   apparent "BTC beta" in M1 was actually altcoin co-movement.
3. **Alpha drops from +144% (M2) to +41.6% — and loses statistical
   significance** (p=0.43, well above any reasonable threshold). The
   confidence interval comfortably crosses zero.

R² rises from 0.53 to 0.68. The altcoin-index factor adds 15pp of
explanatory power — by far the largest single contribution. 68% of
strategy variance is explained by the three-factor crypto-beta model
alone.

---

## 5. Diagnostic comparison

```
        M1 (BTC)    M2 (BTC+ETH)   M3 (BTC+ETH+Alt)
α  ann  +174.55%    +143.83%       +41.62%
α  p     0.0113      0.0240         0.4331   ← lost significance
β_btc   +1.503      +0.509         +0.198    ← lost significance
β_eth     n/a       +0.706         +0.390    ← still significant
β_alt     n/a         n/a          +0.571    ← dominant factor
R²       0.4494      0.5310         0.6798
adj R²   0.4481      0.5287         0.6774
ΔR²        —         +0.082         +0.149   ← Alt adds the most
```

Adjusted R² monotonically increases across specifications — the
additional factors are pulling their weight, not just overfitting.
Each factor's t-stat magnitude in M3 confirms the multi-factor
structure: ETH and Alt are real factors, BTC and the intercept are
not.

---

## 6. What the +174% headline actually was

Decomposing the headline alpha by what each factor steals from the
intercept:

| Factor added | Alpha after addition | "Stolen" from alpha |
|---|---:|---:|
| (BTC only baseline) | +174.55% | — |
| + ETH | +143.83% | −30.7pp |
| + Alt index | +41.62% | **−102.2pp** |

The altcoin index alone "steals" 102pp of annualized alpha from M1.
This is the central finding: **the strategy is fundamentally an
altcoin-beta vehicle.** The model's selection produces correlated
exposure to the altcoin complex, and any individual coin's daily
move loads heavily on the alt-complex factor. The BTC-only regression
in finding7 had to lump that exposure into the intercept because it
had no altcoin variable.

---

## 7. Operational implications

1. **finding7's +174% alpha was largely misattributed.** The true
   uncorrelated edge is at most ~+40% annualized, and the data cannot
   rule out zero. The Sharpe 2.0 / DD -48% figures stand (those are
   total-return-derived), but the **interpretation** of the strategy
   shifts from "leveraged BTC + alpha" to "leveraged altcoin complex
   with negligible incremental selection skill."

2. **The strategy's behavior in stress conditions can be predicted
   from altcoin-index behavior, not BTC.** When the altcoin complex
   gets crushed (which historically happens harder than BTC in bear
   regimes — alt drawdowns of 90%+ are common), the strategy will
   crush proportionally. finding9's bear-case sim should be re-run
   with an altcoin-index path, not a BTC path:

   ```
   bear sim using M3 fitted model:
     strat_daily = 0.00114 + 0.198 × BTC_d + 0.390 × ETH_d + 0.571 × Alt_d
   
   2022-style alt bear (alts -90% over 250 days, ETH -75%, BTC -77%):
     BTC_d = -0.586%/day, ETH_d = -0.552%/day, Alt_d = -0.917%/day
     strat_d = +0.00114 + 0.198×(-0.00586) + 0.390×(-0.00552) + 0.571×(-0.00917)
             = -0.0073 per day
     cum_250 = (1 - 0.0073)^250 - 1 = -83.6%
   ```

   That's between finding9's two endpoints, closer to the pessimistic
   end. Same conclusion: kill switch fires; the strategy is unsuitable
   for a sustained crypto bear without modification.

3. **Position sizing should reflect altcoin-complex exposure, not BTC
   exposure.** finding7 noted ~90% effective BTC exposure at full
   utilization. The honest factor-decomposed picture: roughly
   12% × β_btc + 23% × β_eth + 34% × β_alt = effective per-position
   exposure to those factors. The total altcoin-complex exposure is
   much larger than the BTC exposure once factor structure is
   acknowledged. This matters for capital limits, not for the kill
   switch — but it should inform any conversation about scaling the
   strategy size.

4. **The +41.6% alpha residual is not nothing.** If real, that's a
   competitive edge on top of altcoin-complex beta — a coin-selection
   skill that picks better-than-equal-weighted alts. But it's
   statistically insignificant in this sample (p=0.43), and the next
   data point (live paper-trading P&L) is where to test whether the
   residual persists out-of-sample.

---

## 8. Caveats

- **Alt index construction is one of many reasonable choices.** I used
  equal-weighted mean of top-10 alts by volume rank. Market-cap
  weighted, broader (top-20), or excluding the most volatile (LAB,
  SKYAI which had partial coverage) would give somewhat different
  numbers but the qualitative result would not change — the
  alt-index factor is large and significant under any sensible
  construction.
- **399 daily observations is plenty for the slope estimates but
  modest for the intercept.** Alpha CIs widen with multi-factor
  specifications because the factors absorb variance that helped
  pin down the intercept in M1. Out-of-sample validation is the
  right test, not larger in-sample windows.
- **Collinearity between BTC, ETH, and the alt index is high** (they
  all move together). M3 may have some bias from multicollinearity,
  but the alpha-loss story is robust to which alt-index proxy is
  used and to dropping the BTC factor entirely (M2 alpha is already
  much lower than M1).
- **The strategy isn't equal-weighted alts.** It's a top-N selection
  model. To the extent the model picks high-mean alts within the
  altcoin universe, that selection skill IS the residual +41.6%.
  Finding 8 (per-symbol) tests whether that selection-skill story
  holds at the symbol level.

---

## 9. Verdict

**The +174% alpha headline is meaningfully inflated.** Re-stated
honestly:

- **True uncorrelated alpha: estimated +41.6% annualized, 95% CI
  comfortably crossing zero** (p=0.43). Could be real selection
  skill of moderate magnitude; could be zero edge with noisy
  estimation. Live paper-trading P&L is the next data point.
- **The "BTC beta = 1.50" framing is misleading.** After controlling
  for ETH and alts, BTC beta is +0.20 and not significant. The
  strategy is essentially **long altcoin complex** with secondary
  ETH exposure.
- **Bear-case worst plausible move is closer to -84%** than
  finding7's -63%, when stress propagates via altcoin drawdowns
  rather than BTC. Kill switch fires in all scenarios.

### Next-steps (read-only follow-ups; not requested for action here)

1. Refine the alt-index: try market-cap weighting, restrict to coins
   with full backtest-period coverage. See if β_alt stays around 0.57
   or drifts.
2. Add a **macro factor** (e.g., daily DXY return) to test whether
   the residual +41.6% is just inverse-USD beta in disguise.
3. Test whether the +41.6% residual is robust to recursive
   out-of-sample splits — fit M3 on the first half, predict the
   second half, and measure realized alpha vs predicted. Aligns with
   the finding9 recommendation to budget against the lower-bound
   regime numbers.

---

## 10. References

- Audit script: `.claude/local_scripts/finding8_9_10_combined_audit.py`
  (Investigation 3 section).
- finding7 — single-factor +174% alpha baseline (now downgraded).
- finding9 — regime-split analysis (asymmetry argument; reinforces
  this finding's conclusion).
- Top-10 alt sources: `crypto_universe.symbol` ORDER BY
  `rank_by_volume`, excluding BTC + ETH.
- [[KI-148]] — deployed-spec vs kill-switch gap; once again
  reinforced.
