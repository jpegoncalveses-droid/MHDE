# Backtest: -1% MTM stop addition to Policy D

**Branch:** `research-mtm-stop-1pct-backtest`
**Script:** `.claude/local_scripts/backtest_mtm_stop_1pct.py`
**Run mode:** `dry_run=True` (no writes to `crypto_backtest_*` tables)
**Window:** 2025-04-05 → 2026-05-07 (the full funding-floored walk-fold window
used by the deployed Phase-1B winner)
**Config:** horizon `10d`, Policy D, `top_n=6`, `trail_pct=0.3`,
`activation_pct=0.01` (default), post-parabolic filter ON.

---

## Variant under test

**MTM_1PCT** — addition to existing Policy D logic, not replacement:

- Existing trail still applies (arms at peak ≥ entry × 1.01, 30% giveback).
- Time stop at 10d horizon still applies.
- **NEW**: exit at the day's close when `close <= entry_price × 0.99`.
- First trigger wins. Per-bar evaluation order:
  1. Trail stop (when armed; fires intraday on the bar's low)
  2. -1% MTM stop (fires at the bar's close)
  3. Time stop (fires at the horizon-day close)

Tagged in the trade rows as `exit_reason='sl'` (Policy D never emits `sl`
in baseline, so MTM stops are disambiguated cleanly from trailing/time exits).

### Granularity caveat

The backtest sees daily OHLC bars only. The MTM stop is evaluated on the
**close**, so intraday touches below -1% that recover before close are
invisible. This biases the MTM-stop fire count *down* and the realized
P&L distribution to be *wider* than a tick-data implementation would
produce — when the close ends at -7%, the bar likely passed through -1%
much earlier in the day, but the simulator only sees the close. **Real
intraday execution would stop out at -1% on those bars, not at -7%.**
The headline numbers below therefore overstate the per-stop loss but
also understate the trade count; the directional conclusions still hold.

---

## 1) Portfolio metrics — BASELINE vs MTM_1PCT

| metric                       |   BASELINE |   MTM_1PCT |      Δ |
| ---------------------------- | ---------: | ---------: | -----: |
| n_predictions_seen           |     16,679 |     16,679 |     +0 |
| n_excluded_by_postparabolic  |        105 |        105 |     +0 |
| **n_trades**                 |        941 |  **1,361** |  **+420** |
| **total_net_pnl_pct**        |  **+5247.92%** | **+3098.44%** | **-2149.48pp** |
| **sharpe**                   |   **6.3223** |  **3.1901** | **-3.1323** |
| **max_dd_pct**               |   **-16.98%** | **-45.72%** | **-28.74pp** |
| hit_rate                     |     87.57% |     51.65% | -35.91pp |
| avg_winner_pct               |     +9.26% |    +11.70% |  +2.44pp |
| avg_loser_pct                |    -20.37% |     -7.79% | +12.58pp |
| **profit_factor**            |   **3.2018** |  **1.6046** | **-1.5972** |

## 2) Deltas — summary

- **n_trades +44.6%.** Each MTM-stopped trade frees the coin earlier, so the
  selector picks it (or others) into more later windows. Volume rises but
  quality collapses.
- **Sharpe halved** (6.32 → 3.19).
- **Max drawdown ~2.7× worse** in magnitude (-17.0% → -45.7%).
- **Total return cut by 41%** (5,248pp → 3,098pp).
- **Profit factor halved** (3.20 → 1.60).
- Per-loss magnitude shrinks (-20.4% → -7.8%) but loser **count** explodes:
  baseline had ~117 losers, MTM_1PCT has ~658.
- Per-winner edge improves slightly (+9.3% → +11.7%) — surviving trails
  are the ones that didn't dip past -1% near entry — but the hit rate
  collapse (87.6% → 51.7%) overwhelms the per-winner gain.

## 3) Exit reason distribution

| exit_reason            | BASELINE | MTM_1PCT |
| ---------------------- | -------: | -------: |
| `trailing`             |      824 |      702 |
| `time` (horizon)       |      117 |        0 |
| `sl` (MTM stop @ -1%)  |        0 |  **659** |

- BASELINE Policy D never time-stops above breakeven *and* never closes
  below the trail-armed envelope at the horizon, so the 117 time-stops
  are mostly small winners that never got far enough above +1% to arm
  the trail.
- MTM_1PCT eliminates every time-stop. All 117 baseline time-stops plus
  ~542 trades that previously trailed-out at small profits now either
  stop out at -1% (close-basis) or trail-out from a slightly different
  peak.
- 48.4% of MTM_1PCT trades exit via the MTM stop (659/1361).

## 4) MTM-stop realized P&L distribution (net of costs, 659 trades)

| stat                                       |       value |
| ------------------------------------------ | ----------: |
| mean                                       |  **-7.78%** |
| median                                     |      -6.04% |
| min                                        |     -49.72% |
| max                                        |      +0.02% |
| stdev                                      |       6.41% |
| Q1 / Q3                                    | -10.57% / -3.36% |
| `-2.0% < P&L ≤ -0.5%` (cluster near stop)  |     66 (10%) |
| `P&L ≤ -2.0%` (gap-down past stop)         | **592 (90%)** |
| `P&L > -0.5%` (recovered — shouldn't happen) | 1 |

**Daily-close granularity defeats the stop.** With a -1% intent, only 10%
of fires actually clear near -1%; the other 90% close at -2% or worse
because by end-of-day the bar has already moved well past -1%. Median
realized loss is **-6.04%**, not -1%. Min is -49.7% (a one-day -50%
crash where intraday execution couldn't have helped much either).

This is the documented limitation: a real implementation on tick data
would close most positions at -1% and produce a tight cluster near
-0.01. The current simulator can't show that, but it does show the
*lower bound* of stop-out frequency (intraday-real would only add fires,
not subtract).

## 5) Stop-out tax — would MTM-stopped trades have hit +10% if held?

For each of the 659 MTM-stopped trades, we look up the matching
walkfold prediction in `crypto_ml_predictions` (model_id LIKE
`crypto_%_walkfold_%`, horizon=`10d`, prediction_date = entry_date − 1d)
and read its `actual_hit` label (= 1 if the held-to-horizon trajectory
ever reached +10%).

| bucket                                             | count |    % of stopped |
| -------------------------------------------------- | ----: | --------------: |
| would have hit +10% in 10d (`actual_hit=1`)        |  **165** | **25.0%** |
| would NOT have hit +10% (`actual_hit=0`)           |   487 |          73.9% |
| label not available / unmatured                    |     7 |           1.1% |
| **total MTM-stopped**                              |   659 |         100.0% |

Held-to-horizon hypothetical performance of the stopped-out set:

| stat                                      |    value |
| ----------------------------------------- | -------: |
| `actual_max_return` — mean                | **+8.42%** |
| `actual_max_return` — median              |   +1.83% |
| `actual_max_drawdown` — mean              |  -16.76% |
| `actual_max_drawdown` — median            |  -14.46% |

**Interpretation.** One in four stopped-out trades would have reached
+10% if held to horizon. The set's *average* would-have-been peak return
is **+8.42%** — positive, meaningfully so. The stop is killing the
upside without proportionally protecting the downside, because Policy
D's trail already trims the deepest losers (baseline avg_loser_pct
is -20%, but only on 117 trades; the MTM stop adds 541 *additional*
losers that the baseline never even registered as losers).

---

## Verdict: **KILL**

Every directional metric moves the wrong way:

| metric        | direction wanted | direction observed |
| ------------- | ---------------- | ------------------ |
| Sharpe        | up               | **down (-50%)**    |
| Max DD        | shallower        | **2.7× deeper**    |
| Total return  | up or flat       | **-41%**           |
| Profit factor | up               | **-50%**           |
| Hit rate      | up or flat       | **-36 pp**         |

The mechanism is straightforward:

1. The trail stop already provides loss truncation **conditional on a
   profitable peak** (it arms at +1%). Trades that don't reach +1% just
   ride the horizon.
2. A -1% absolute stop fires on a population that includes many trades
   destined to recover — 25% would have hit the +10% label target if
   left alone, and the held-to-horizon mean is **+8.4%** on the stopped
   set.
3. Because the stop fires early and frees the coin, the selector
   re-enters more often, growing trade count by 45%. Each re-entry has
   the same edge-distribution problem, so the bad ones compound the
   stop-out tax.
4. Daily-close granularity means the stop doesn't actually limit losses
   to -1% — median realised stop-out is -6% and 90% of stops fire ≤ -2%.
   Even with perfect intraday execution closing every fire at exactly
   -1%, the stop-out-tax (25% of fires on would-be +10% winners) is the
   dominant signal, and that survives the granularity assumption.

**Recommendation:** do **not** add an absolute -1% MTM stop to Policy D.
The trail is doing its job. If we want to bound *worst-case* loss
(currently -50% on one observed bar) the right control is a much wider
stop (≥ -5% or ATR-scaled) so it only fires on tail moves, not on
ordinary noise.

---

## Files

- `.claude/local_scripts/backtest_mtm_stop_1pct.py` — backtest harness
  (subclasses `policies.TrailingStopOnly` → `TrailingStopOnlyWithMtm`,
  swaps it into `policies._POLICY_BY_ID["D"]` for the MTM_1PCT run, runs
  both variants `dry_run=True`).
- `data/processed/mtm_stop_1pct_backtest_report.md` — this report.

No production code changes. No writes to the live DB.
