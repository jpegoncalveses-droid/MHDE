# Finding 8 — Per-symbol alpha/beta decomposition + bucket classification

**Investigation date:** 2026-05-15
**Mode:** read-only
**Trigger:** Portfolio-level β = 1.50 / α = +174% (finding7) is an
aggregate. The per-symbol picture might be wildly heterogeneous —
some symbols genuinely alpha-driven, others mostly BTC-leverage.
Step 1 of the finding7 follow-up runs the same regression for each
symbol in the active universe and bucket-classifies them, then
applies the classification to today's 18 predicted symbols.

**Bottom line:** The per-symbol picture is **heterogeneous but the
distribution is dominated by the "mixed" bucket** (high alpha + high
beta — 50% of universe, 67% of today's predictions). **Pure alpha
(low-beta, alpha-driven) candidates are essentially absent from
today's portfolio.** Most apparent symbol-level alpha is fragile:
low R², small samples, and consistent with the finding10 conclusion
that "alpha" is largely altcoin-complex co-movement.

---

## TL;DR

Bucket distribution over the 48 symbols with ≥100 days of paired
daily returns:

| Bucket | Definition | Universe (n=48) | Today's preds (n=18) |
|---|---|---:|---:|
| **1_alpha** | β < 0.5 AND α > +10% annualized | 3 (6%) | **1 (6%)** |
| **2_beta** | β > 0.8 AND α < +5% annualized | 20 (42%) | **4 (22%)** |
| **3_mixed** | β > 0.8 AND α > +5% annualized | 24 (50%) | **12 (67%)** |
| 4_other | everything else | 1 (2%) | 1 (6%) |

Today's 18 predicted symbols by bucket:

| Bucket | Symbols (today's predictions) |
|---|---|
| **1_alpha (1)** | UBUSDT (α=+463%, β=+0.49, R²=0.01, n=240 — high alpha but slope NOT significant, p=0.09; essentially noise) |
| **2_beta (4)** | PENDLEUSDT, 4USDT, NOTUSDT, WLFIUSDT |
| **3_mixed (12)** | RAVEUSDT, SKYAIUSDT, NAORISUSDT, ZEREBROUSDT, TAGUSDT, BUSDT, FHEUSDT, SWARMSUSDT, BIOUSDT, 1000LUNCUSDT, ORCAUSDT, TSTUSDT |
| 4_other (1) | LABUSDT (α=+968%, β=+0.69 — borderline, p not significant) |

---

## 1. Method

For each symbol with ≥100 paired daily observations in the backtest
window (2025-04-06 → 2026-05-10):

```
symbol_ret_t ~ alpha + beta × BTC_ret_t + ε_t
```

via `scipy.stats.linregress` on coin daily returns from
`crypto_prices_daily`, joined inner with BTC daily returns over the
same window. Alpha reported annualized (×365). 48 of the 50 active
universe symbols fit the threshold; 2 dropped for insufficient
history.

Bucket definitions:

- **Bucket 1 (alpha-driven):** `β < 0.5` AND `α_annual > +10%`. Low
  systematic crypto exposure with material positive intercept —
  candidate uncorrelated-edge symbols.
- **Bucket 2 (beta-driven):** `β > 0.8` AND `α_annual < +5%`. High
  systematic crypto exposure with no material intercept — leveraged
  BTC bets dressed up.
- **Bucket 3 (mixed):** `β > 0.8` AND `α_annual > +5%`. High exposure
  AND positive intercept — the portfolio's overall profile per
  finding7.
- **Bucket 4 (other):** everything else (typically `0.5 ≤ β ≤ 0.8`
  with various α). Hard to characterize.

Script: `.claude/local_scripts/finding8_9_10_combined_audit.py`
(Investigation 1 section). CSV outputs at
`/tmp/finding8_per_symbol.csv` and
`/tmp/finding8_today_preds_buckets.csv`.

---

## 2. Universe distribution

```
       alpha_annual_pct       beta         R²      n_days
count         48.000000  48.000000  48.000000   48.000000
mean         147.461404   1.344967   0.317198  366.166667
std          309.963950   0.446299   0.243939   65.764678
min         -102.527196   0.142984   0.004844  147.000000
25%          -10.474997   1.053823   0.094030  385.250000
50%           36.446917   1.412532   0.268635  399.000000
75%          181.947556   1.557489   0.489983  399.000000
max         1689.818826   2.379689   1.000000  399.000000
```

Median beta is **1.41** — close to the portfolio's 1.50. The
distribution is heavily skewed right on alpha (median +36%, mean
+147%) driven by a few outliers (RAVEUSDT +1690%, LABUSDT +968%,
UBUSDT +463%, etc.). Most of those high-alpha-low-R² outliers are
recent-listing meme/themed coins with very small windows. R² across
the universe is low to moderate (median 0.27) — symbol-level
single-factor R²s are noisier than the portfolio's 0.45.

**Bucket counts on the full universe:**

| Bucket | Count | % | Reading |
|---|---:|---:|---|
| 1_alpha | 3 | 6% | True alpha-driven symbols are rare. |
| 2_beta | 20 | 42% | A large fraction of the universe is mostly BTC-leveraged. |
| 3_mixed | 24 | 50% | The dominant pattern: high-beta + high-α. Likely altcoin-complex co-movement disguised as α (per finding10). |
| 4_other | 1 | 2% | LABUSDT, mid-beta. |

The 50% "mixed" bucket dominance is consistent with finding10's
conclusion that the +174% portfolio alpha is mostly altcoin-complex
beta in BTC-only regression disguise. At the symbol level, the same
phenomenon: most coins look "high alpha + high beta to BTC" because
the BTC-only regression cannot separate out the altcoin-complex
factor they all move with.

---

## 3. Today's 18 predicted symbols — detail

All 18 have sufficient history to fit.

### Bucket 1 (alpha-driven, β<0.5 + α>+10%): 1 symbol

| symbol | α_annual % | β | R² | n_days | slope_p |
|---|---:|---:|---:|---:|---:|
| UBUSDT | +463.31% | +0.49 | 0.012 | 240 | 0.095 |

**Interpretation.** UBUSDT lands in the alpha bucket *only* because
its beta narrowly clears the < 0.5 cutoff. But its **R² is 0.012 and
slope p-value is 0.095** — beta is not statistically distinguishable
from zero. The huge alpha (+463%) is mostly the symbol's recent
strong run, not a robust signal. This is **noise classified as
alpha**, not a candidate uncorrelated-edge symbol.

**Practical conclusion: today's predictions contain zero credible
bucket-1 symbols.**

### Bucket 2 (beta-driven, β>0.8 + α<+5%): 4 symbols

| symbol | α_annual % | β | R² | n_days | slope_p |
|---|---:|---:|---:|---:|---:|
| PENDLEUSDT | +2.94% | +1.64 | 0.479 | 399 | 3.5e-58 |
| 4USDT | −9.28% | +2.31 | 0.183 | 214 | 6.4e-11 |
| NOTUSDT | −54.58% | +1.47 | 0.323 | 399 | 1.7e-35 |
| WLFIUSDT | −102.53% | +0.98 | 0.161 | 260 | 1.8e-11 |

**Interpretation.** These are well-fit, high-beta names with
**negative or near-zero alpha**. Trading them is effectively
trading BTC β. The model is including them because their probability
score exceeded the threshold today, but historically they offer no
selection edge above their systematic exposure. 4USDT in particular
is a 2.31× BTC leveraged-equivalent position — exactly the
"leveraged BTC dressed up" pattern the operator suspected.

This bucket includes one currently-open pre-baseline position
(4USDT, finding5/6 also flagged it) and three actively-traded
candidates. **For operator situational awareness: 22% of today's
predictions are effectively leveraged-BTC plays with no historical
selection-alpha.**

### Bucket 3 (mixed, β>0.8 + α>+5%): 12 symbols

| symbol | α_annual % | β | R² | n_days | slope_p |
|---|---:|---:|---:|---:|---:|
| RAVEUSDT | +1689.82% | +1.37 | 0.013 | 147 | 0.164 |
| SKYAIUSDT | +476.96% | +1.45 | 0.056 | 362 | 4.9e-06 |
| NAORISUSDT | +469.30% | +1.49 | 0.052 | 283 | 1.1e-04 |
| ZEREBROUSDT | +376.83% | +1.44 | 0.035 | 399 | 1.5e-04 |
| TAGUSDT | +313.47% | +0.85 | 0.037 | 289 | 1.0e-03 |
| BUSDT | +273.38% | +1.30 | 0.064 | 353 | 1.6e-06 |
| FHEUSDT | +268.72% | +0.83 | 0.014 | 393 | 1.8e-02 |
| SWARMSUSDT | +135.41% | +1.87 | 0.199 | 399 | 6.9e-21 |
| BIOUSDT | +109.44% | +1.85 | 0.206 | 399 | 1.2e-21 |
| 1000LUNCUSDT | +106.63% | +0.99 | 0.120 | 399 | 1.0e-12 |
| ORCAUSDT | +41.17% | +1.37 | 0.209 | 399 | 5.8e-22 |
| TSTUSDT | +6.96% | +1.25 | 0.104 | 399 | 4.0e-11 |

**Interpretation.** 67% of today's portfolio. Two sub-patterns:

- **High-α, very-low-R² names** (RAVEUSDT, SKYAIUSDT, NAORISUSDT,
  ZEREBROUSDT, TAGUSDT, BUSDT, FHEUSDT) — R² < 0.07. These are
  recent-listing or meme/themed coins where the small-sample alpha
  estimates are inflated by a few large rallies. The α numbers
  (+270% to +1690%) are **not robust** — wide CIs, low explanatory
  power, large fraction of variance is idiosyncratic noise. Treat
  these as "high-beta speculative bets that happened to win
  recently."
- **Moderate-α, moderate-R² names** (SWARMSUSDT, BIOUSDT, 1000LUNC,
  ORCAUSDT, TSTUSDT) — R² 0.10–0.21. Real systematic signal AND
  positive intercept, all statistically significant. These are the
  closest thing the universe has to "leveraged edge" symbols. Per
  finding10's multi-factor analysis, much of even these "alphas"
  reflects altcoin-complex co-movement, but they're more credible
  than the very-low-R² names.

SWARMSUSDT specifically: β=1.87, R²=0.20. The strategy held it 5/13 →
exit, re-entered 5/14, and today the Variant D filter (correctly per
finding6) excluded it. The per-symbol α/β picture explains *why* the
model keeps picking SWARMSUSDT — it's a high-beta high-volatility
name where the model's signal compounds well in bull regimes — and
why the filter is doing the right work: structurally exposed to the
exact tail finding9 / finding10 warn about.

### Bucket 4 (other): 1 symbol

| symbol | α_annual % | β | R² | n_days | slope_p |
|---|---:|---:|---:|---:|---:|
| LABUSDT | +968.10% | +0.69 | 0.009 | 205 | 0.176 |

**LABUSDT** has medium beta (0.69 — outside both extremes), huge
nominal alpha, and **R²=0.009 with p=0.18**. The β estimate is not
significantly different from zero. Same caveat as UBUSDT — high
alpha looks dramatic but is small-sample / low-signal.

---

## 4. Does per-symbol mask portfolio-level β=1.50?

**Mostly no, but with caveats.** The universe median β is 1.41,
75th-percentile β is 1.56 — **the portfolio's β=1.50 is the typical
symbol-level β,** not an aggregation artifact. The story holds at
both levels.

What the per-symbol view *does* surface that the portfolio number
hid:

1. **Three universe-level bucket-1 candidates exist** (3 symbols out
   of 48) but none of them are picked by the model today, and the
   one that is (UBUSDT) is a noise classification, not a real
   alpha-pick.
2. **Bucket-2 leveraged-BTC names are 22% of today's portfolio.**
   The model is willing to issue picks for symbols that have no
   historical alpha — it's selecting purely on its short-window
   probability score, not on whether the symbol is structurally
   profitable to trade beyond beta. This is a model-selection
   concern, not a filter concern.
3. **The 12 bucket-3 symbols split into two cohorts** (high-α
   low-R² vs moderate-α moderate-R²) that look identical in the
   portfolio aggregate but tell different stories per-symbol. The
   low-R² cohort is mostly noise; the moderate-R² cohort is
   genuinely "leveraged edge with credible systematic component."

---

## 5. Operational implications

1. **The portfolio is structurally an altcoin-complex bet.** Per
   finding10 (multi-factor) the headline alpha is mostly altcoin
   beta. Per this finding (per-symbol) most of the universe is
   high-β-to-BTC with intercepts that look high only because the
   BTC-only model can't see the altcoin factor. Both findings agree:
   **selection skill at the symbol level, conditional on factor
   structure, is weaker than finding7 implied.**

2. **The "1 position per symbol" rule plus Variant D's
   post-parabolic filter are the two structural protections** that
   keep this portfolio composition tradeable. Without them the
   strategy would re-pile into recent winners (selection-from-noise
   bias on the bucket-3 low-R² names).

3. **A future Layer-1 risk-architecture decision should explicitly
   distinguish bucket-2 picks from bucket-3 picks.** The current
   engine consumes all 30 raw predictions identically; the per-symbol
   bucket layer would gate bucket-2 names harder (since they're just
   leveraged BTC) and apply tighter trailing on bucket-3 low-R² names
   (since their α is partly noise). Out of scope for this finding;
   filed as a future research direction.

4. **No bucket-1 candidates in today's portfolio** means the
   operator should not expect "BTC-uncorrelated returns" from any
   single position today. Every actionable pick is either pure beta
   or leveraged-edge.

---

## 6. Caveats

- **The β cutoffs are not principled** — they're operationally
  meaningful (β < 0.5 → "not really crypto-correlated"; β > 0.8 →
  "definitely a crypto bet") but boundary symbols can flip with
  small noise. UBUSDT and LABUSDT are exactly the kind of
  border-classification cases where the bucket assignment is more
  noise than signal.
- **Single-factor regression is the wrong model for most of these
  symbols** — per finding10, the altcoin-complex factor explains
  more of the variance for most symbols than BTC does. Per-symbol
  multi-factor regressions would shrink the bucket-3 "alphas"
  substantially. The bucket labels in this finding overstate
  apparent alpha and should be read as "single-factor α, mostly
  altcoin-complex beta in disguise" for the high-α-low-R² names.
- **Sample sizes vary** (147 to 399 days). High-α small-sample names
  (RAVEUSDT, WLFIUSDT, UBUSDT) have very wide CIs. Treat their α
  numbers as point estimates with much weaker confidence than the
  full-sample names.
- **Today's 18 predictions** is a subset (10d horizon only — the
  production-exported model). 5d-horizon predictions weren't
  bucketed; the picture should generalize but isn't tested here.

---

## 7. Verdict

The per-symbol picture **reinforces** rather than overturns
finding7 / finding10:

- The portfolio's β = 1.50 is the typical symbol-level β, not an
  aggregation artifact.
- True alpha-driven candidates (bucket 1) are scarce universe-wide
  (3 of 48) and absent in today's portfolio.
- 22% of today's portfolio is bucket-2 leveraged-BTC with no
  historical edge.
- 67% is bucket-3 mixed — split between low-R² noise-driven and
  moderate-R² credible-leveraged-edge sub-cohorts.

**Net:** the strategy is structurally a leveraged-altcoin-complex
play with thin and noisy symbol-level alpha. Live paper trading is
the right test for whether the residual signal is real out-of-sample.

### Next-steps (read-only follow-ups; not requested for action here)

1. **Per-symbol multi-factor regression.** Re-run the per-symbol
   analysis with M3's `BTC + ETH + Alt index` as the factor set.
   Expectation: most bucket-3 α numbers collapse, bucket boundaries
   shift, bucket-1 may grow or shrink.
2. **Live-trading α tracking.** For each new closed trade, measure
   realized return against the same-window factor-implied return
   (using the symbol's fitted β to BTC/ETH/Alt). Maintain a running
   live-α-by-symbol table. If after 3–6 months the realized α holds
   above the in-sample CI, selection skill is confirmed
   out-of-sample.
3. **Bucket-aware engine architecture (Layer 1).** Apply position
   sizing or filter gates based on bucket — e.g., bucket-2 symbols
   get half the standard position size since their expected return
   is mostly systematic.

---

## 8. References

- Audit script: `.claude/local_scripts/finding8_9_10_combined_audit.py`
  (Investigation 1 section).
- CSV outputs:
  - `/tmp/finding8_per_symbol.csv` — full 48-symbol table.
  - `/tmp/finding8_today_preds_buckets.csv` — today's 18 predictions
    with buckets.
- finding7 — portfolio-level baseline.
- finding9 — alpha regime persistence (the bear-case is per-coin worse
  than per-portfolio because individual coins drop harder than the
  altcoin index in stress).
- finding10 — multi-factor decomposition (cause of the bucket-3 noise:
  altcoin-complex co-movement disguised as α by the BTC-only model).
- finding6 — SWARMSUSDT specifically (now contextualized: high-β
  mixed-bucket symbol that Variant D correctly filters in stress).
- [[KI-148]] — kill-switch envelope; once again reinforced.
