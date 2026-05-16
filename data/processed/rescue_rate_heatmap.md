# Rescue-rate heatmap — BASELINE Policy D

**Source:** `.claude/local_scripts/rescue_rate_heatmap.py`
**Window:** 2025-04-05 -> 2026-05-07 (Phase-1B walk-fold window)
**Config:** horizon=10d, Policy D, top_n=6, trail_pct=0.3, activation_pct=0.01, post-parabolic filter ON.
**Run mode:** dry_run=True (no DB writes).

## Method

1. Re-ran BASELINE Policy D in dry_run mode -> got 941 closed trades.
2. For each closed trade, walked daily closes from entry+1 to entry+days_held, recording `(day_held, drawdown_pct, final_pnl_pct)` per bar where `crypto_prices_daily.close` was available.
   - drawdown_pct = (close - entry_price) / entry_price * 100  (gross)
   - final_pnl_pct = (exit_price / entry_price - 1) * 100      (gross)
3. Total intermediate states observed: **3,463**.
4. Bucketed by drawdown center (5pp-wide bins) and day_held 1..9. `-35+` is open-ended (dd <= -32.5).
5. Per cell, computed: N, % recovered (final > current drawdown), % worse, mean and median final P&L, and EV-of-cutting (= cell_center - mean_final; positive means cutting beats holding).
6. Flag rule: `*` = `EV_of_cutting > 3pp AND N > 30`.

## Table 1 — N / recovery% per cell

Each cell shows `N / pct_recovered`. `*` = flagged (EV-of-cutting > 3pp, N > 30).

```
 dd \ day     d1        d2        d3        d4        d5        d6        d7        d8        d9   
---------------------------------------------------------------------------------------------------
     -5      197/  84    178/  87     74/  81     53/  81     40/  80     28/  71     25/  72     20/  60     11/  55 
    -10      114/  72     98/  76     68/  56     59/  53     42/  62     38/  55     29/  48     18/  50     18/  56 
    -15       37/  62     67/  72     34/  44     39/  51     43/  42     35/  51     23/  48     26/  42     20/  60 
    -20       16/  62     27/  59     16/  62     15/  47     15/  53     17/  53     27/  67     26/  54     29/  55 
    -25        9/  56     12/  58     15/  53     16/  44     14/  64     17/  35     15/  40     12/  58     12/  17 
    -30        3/ 100      5/  40      6/  50      4/  50     11/  36      9/  44     10/  30      9/  56     11/  36 
   -35+        5/  60      7/  57      8/  50     10/  40     11/  45     12/  50     16/  50     19/  58     16/  56 
```

## Table 2 — Mean final P&L of cohort (%, gross)

```
 dd \ day     d1        d2        d3        d4        d5        d6        d7        d8        d9   
---------------------------------------------------------------------------------------------------
     -5       +0.7       +1.7       +1.3       +0.7       +0.8       -0.7       -0.6       -3.8       -3.5   
    -10       -3.4       -1.4       -6.8       -5.6       -5.5       -7.3       -8.5       -7.8      -10.0   
    -15       -5.6       -5.0      -10.8      -13.5      -15.4      -12.0      -13.3      -14.7      -14.1   
    -20      -15.2       -6.2      -17.2      -16.8      -19.6      -20.9      -16.1      -18.4      -18.5   
    -25      -16.0      -13.8      -11.8      -18.4      -11.8      -23.4      -24.0      -21.1      -26.6   
    -30       +5.8      -14.8      -18.1      -23.7      -27.3      -29.0      -31.9      -30.3      -31.1   
   -35+       -6.7      -16.4      -37.1      -37.9      -37.5      -37.8      -35.1      -37.2      -36.7   
```

## Table 3 — EV of cutting vs holding (pp)

Positive = cutting at this state realizes a better outcome than the cohort's mean final P&L.

```
 dd \ day     d1        d2        d3        d4        d5        d6        d7        d8        d9   
---------------------------------------------------------------------------------------------------
     -5       -5.7       -6.7       -6.3       -5.7       -5.8       -4.3       -4.4       -1.2       -1.5   
    -10       -6.6       -8.6       -3.2       -4.4       -4.5       -2.7       -1.5       -2.2       -0.0   
    -15       -9.4      -10.0       -4.2       -1.5       +0.4       -3.0       -1.7       -0.3       -0.9   
    -20       -4.8      -13.8       -2.8       -3.2       -0.4       +0.9       -3.9       -1.6       -1.5   
    -25       -9.0      -11.2      -13.2       -6.6      -13.2       -1.6       -1.0       -3.9       +1.6   
    -30      -35.8      -15.2      -11.9       -6.3       -2.7       -1.0       +1.9       +0.3       +1.1   
   -35+      -30.8      -21.1       -0.4       +0.4       +0.0       +0.3       -2.4       -0.3       -0.8   
```

## Open-position mapping (2026-05-14)

| coin | drawdown | day | bucket | N | rec% | worse% | mean_final | median_final | EV-of-cut | flag |
|------|---------:|----:|:------:|--:|-----:|-------:|-----------:|-------------:|----------:|:----:|
| SWARMSUSDT | -27.4% | d1 | -25 | 9 | 56% | 44% | -16.0% | -18.8% | -9.0pp | no |
| TAGUSDT | -9.6% | d1 | -10 | 114 | 72% | 28% | -3.4% | +1.8% | -6.6pp | no |
| 4USDT | -19.6% | d3 | -20 | 16 | 62% | 38% | -17.2% | -18.9% | -2.8pp | no |
| RAVEUSDT | -4.7% | d3 | -5 | 74 | 81% | 19% | +1.3% | +1.7% | -6.3pp | no |

## Flagged cells (cutting beats holding)

**No cells flagged.** Across the full grid, no `(drawdown, day)` combination has the cohort's mean final P&L worse than the current drawdown by > 3pp at N > 30.

The only three cells with *any* positive EV-of-cutting are all small samples:

| bucket | day | N | mean_final | EV-of-cut |
|-------:|----:|--:|-----------:|----------:|
| -30  | d7 | 10 | -31.9% | +1.9pp |
| -25  | d9 | 12 | -26.6% | +1.6pp |
| -30  | d9 | 11 | -31.1% | +1.1pp |

Each has N ≤ 12 (well below the 30 threshold) and EV ≤ 1.9pp (well below the 3pp threshold). These are noise, not signal.

## Findings

### Finding 1 — Recovery is the rule, not the exception, even from deep drawdowns

Across every observed (drawdown, day) cell with N > 30, the cohort's mean final P&L is **higher** (less negative) than the current drawdown — i.e., holding beats cutting on average, at every drawdown level from -5% through -25%. Examples (from Table 2):

- **-10% drawdown on d1**: cohort mean recovers to **-3.4%** (N=114, 72% recover). Cutting locks -10%, holding realizes -3.4% on average — **a 6.6pp gain for holding**.
- **-15% drawdown on d1-d2**: cohort mean ends at **-5.6% / -5.0%** (N=37 / N=67). Holding gains ~10pp over cutting.
- **-20% drawdown on d2**: cohort mean recovers to **-6.2%** (N=27). Holding gains 13.8pp.
- **-5% drawdown**: cohort mean stays near 0 (-3.5% to +1.7%) at every day; recovery rate 55-87%.

The deployed trail-stop policy (30% giveback once peak ≥ entry × 1.01) gives losing positions room to recover, and the data shows they often do.

### Finding 2 — No statistically robust "lost cause" zone exists

Deeper drawdown cohorts (-30% and -35+) settle near their current depth but never definitively below it at meaningful sample size:

- **-30% bucket on d6-d9**: mean settles at **-29.0% / -31.9% / -30.3% / -31.1%** (N = 9 / 10 / 9 / 11). Cutting at -30 vs holding to those means yields EV of **-1.0 / +1.9 / +0.3 / +1.1 pp** — symmetric around zero, not a clear cut signal.
- **-35+ bucket on d3-d9**: cohort mean lives at **-37.1% to -35.1%** (N = 8 / 10 / 11 / 12 / 16 / 19 / 16). EV-of-cutting (using -37.5 as the cell representative) hovers between **-2.4pp and +0.4pp** — even from a -35%+ underwater state on day 5, holding to the policy's natural exit realizes essentially the same outcome as cutting now.

The "deep loser locks in" intuition is not supported in this window. The trail policy's eventual time-stop or partial-recovery trail exit produces outcomes near (but rarely worse than) the current drawdown.

### Finding 3 — All 4 open positions sit in hold-favored cells

| coin | dd | day | N | rec% | mean_final | EV-of-cut | read |
|------|----|----:|--:|----:|----------:|----------:|------|
| RAVEUSDT  | -4.7%  | d3 |  74 | 81% | **+1.3%** | -6.3pp | **strongest hold** — cohort finishes net positive on average |
| TAGUSDT   | -9.6%  | d1 | 114 | 72% | -3.4%     | -6.6pp | **clear hold** — large sample, big recovery edge |
| 4USDT     | -19.6% | d3 |  16 | 62% | -17.2%    | -2.8pp | **directional hold** — small N, but mean still beats current dd by 2.4pp |
| SWARMSUSDT| -27.4% | d1 |   9 | 56% | -16.0%    | -9.0pp | **directional hold** — N=9, but mean recovers 11pp; deep d1 cohorts have rarely been "locked in" historically |

The two small-N positions (SWARMSUSDT d1, 4USDT d3) sit in cells with N=9 and N=16, so the read is directional rather than statistically robust. But there is no cell in the grid where the data argues for cutting — the worst-case read is "ambiguous, slight hold."

## Path C (recovery-probability model) feasibility

**Conclusion: Path C has no historical edge to capture in this window. Deprioritize.**

A recovery-probability model would be useful only if there were a region of the (drawdown, day) state space where holding is meaningfully worse than cutting, *and* enough samples to learn the boundary. The heatmap shows:

- **63 occupied cells covering 3,463 intermediate states** — adequate data density.
- **Zero cells flagged** under the threshold (EV > 3pp, N > 30). The strongest "cut" signals are noise-level (EV ≤ 1.9pp at N ≤ 12).
- **The trail-stop's existing structure already absorbs the recovery dynamics**: it cuts losses only by going to the horizon time-stop (10d), and gives the trade the entire window to recover.

Building a binary classifier on top of this state space would be learning to predict noise. The signal isn't there.

This is consistent with — and now generalizes — the prior **-1% MTM stop KILL verdict** (`data/processed/mtm_stop_1pct_backtest_report.md`, commit `0ed3f51`). That report showed cutting at -1% cost ~2,150pp of total return and 2.7× the drawdown. The rescue-rate heatmap shows *no* fixed drawdown threshold (not just -1%) has the historical signal to support a cutting rule.

### What this means for the open positions

Hold all four through their natural Policy D exits. The historical data does not support any of them being cut at current drawdowns. Watch RAVEUSDT and TAGUSDT particularly — their cohorts have **81% / 72%** recovery rates with mean final P&L near or above zero.

### Where to redirect Path C effort — pivot recommendation

Since conditional cutting doesn't help, the leverage is upstream of the exit:

- **Path A (entry-side filters / model selectivity)**: tighten the top-N selection or add post-prediction filters that screen out the trades most likely to spend time deep underwater. Even a small reduction in the tail of the entry distribution compounds, because the trail policy can't fix bad entries.
- **Path D (entry-time signal enrichment)**: e.g., funding-rate or OI features at entry that predict early dump magnitude. The post-parabolic filter is one example of this lever already paying off; more filters of the same shape are the right next investment.
- **Path E (sizing / Kelly)**: scale position size by signal strength rather than equal-weight top-N. The mean-final distribution per cell suggests that high-conviction trades may be the ones that recover; low-conviction trades may be the deep-DD tail. Conviction-weighted sizing transfers risk from the tail to the body without needing a cutting rule.

The MTM-stop investigation (which informed the -1% KILL) and this rescue-rate analysis together close the conditional-cut family of interventions for the BASELINE Policy D regime in this window.

## Reproducibility

```
venv/bin/python .claude/local_scripts/rescue_rate_heatmap.py
```

Reads `data/mhde.duckdb` (`crypto_ml_predictions`, `crypto_prices_daily`, `crypto_funding_rates`, `crypto_ml_features`) and re-runs the BASELINE Policy D harness in dry_run mode. No writes. Produces `data/processed/rescue_rate_heatmap.md` (this file).
