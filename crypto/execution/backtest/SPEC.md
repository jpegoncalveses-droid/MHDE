# Phase 1: Execution Backtest Specification
## Purpose
Determine the optimal combination of horizon, exit policy, and selection rule for converting the crypto model's predictions into profitable trades. Produces the locked execution spec that Phase 2 will implement.
## Isolation Guarantees
This phase will not affect any existing pipeline. Specifically:
**Equities pipeline (live, validated):**
- No reads or writes to equity tables
- No imports from equity modules
- No changes to equity systemd timers or CLI commands
- Equity dashboard remains untouched
**FX pipeline (backlogged):**
- No reads or writes to FX tables if any exist
- No imports from FX modules
- No interference with future FX work
**Crypto pipeline (in Phase 0 calibration validation):**
- Read-only access to `crypto_ml_predictions`, `crypto_ml_features`, `crypto_ohlcv`, `crypto_funding_rates`
- No writes to any existing crypto table
- No changes to crypto prediction code, daily pipeline, or systemd timer
- Crypto dashboard remains untouched
**Shared infrastructure:**
- No modifications to shared `ml/` modules
- No changes to `main.py` core (only adds a new sub-command)
- No changes to existing DuckDB schema for any asset class
New code lives in `crypto/execution/backtest/` to make the crypto scope unambiguous. New tables prefixed `crypto_backtest_*`. Runs via a dedicated CLI command outside the daily prediction window.
DuckDB concurrency: backtest opens a read-only connection to existing tables and writes only to new `crypto_backtest_*` tables. If concurrent writes are a concern, schedule the backtest outside the 00:30 UTC daily prediction window.
---
## Inputs
**Read-only from existing tables:**
| Table | Fields needed | Purpose |
|---|---|---|
| `crypto_ml_predictions` | coin, date, horizon, probability, predicted_class | Signal source |
| `crypto_ohlcv` | coin, date, open, high, low, close, volume | Entry, exit, price path |
| `crypto_funding_rates` | coin, timestamp, rate | Funding cost calc |
| `crypto_ml_features` | coin, date, atr_pct_14d | ATR-based stops |
**Date range:** All Phase 1A backfill predictions (filtered by `model_id LIKE 'crypto_%_walkfold_%'`) where:

- Prediction horizon has fully elapsed (so actual outcomes are known)
- Entry date >= 2025-04-05 (funding-rate data coverage floor; earlier predictions would bias policy ranking due to missing funding cost, especially against longer-hold policies)

Expected volume: ~31k predictions across 5d and 10d horizons after both filters.
---
## Architecture
```
crypto/
└── execution/
    └── backtest/
        ├── __init__.py
        ├── harness.py         # Replay engine
        ├── policies.py        # Exit policy implementations
        ├── selection.py       # Top N vs threshold logic
        ├── costs.py           # Fee, funding, slippage models
        ├── metrics.py         # P&L, Sharpe, drawdown calculations
        ├── runner.py          # Orchestrates full grid runs
        └── report.py          # Summary tables + chart generation
```
**CLI entry point:** `python main.py crypto backtest --grid full` or `--grid sensitivity`
**New tables:**
- `crypto_backtest_runs`: one row per (horizon, policy, selection rule, parameters) combination
- `crypto_backtest_trades`: one row per simulated trade
- `crypto_backtest_summary`: aggregated metrics per run for fast comparison
---
## Replay Harness Logic
For each daily prediction batch in the historical dataset:
1. **Selection step:** apply selection rule (Top N or probability threshold) to filter daily predictions into a tradeable set
2. **Entry step:** simulate entry at next day's open price (or the open after the prediction was made)
3. **Tracking step:** for each open position, walk forward day by day
4. **Exit step:** apply exit policy on each forward day, exit at the first triggered condition:
   - Take profit hit
   - Stop loss hit
   - Trailing stop hit
   - Time stop reached (horizon expired)
5. **Settle step:** compute gross P&L, subtract fees and funding, record net P&L
6. **Repeat** until all predictions in dataset are processed
**Position sizing in backtest:** normalize to % returns per trade. Don't simulate dollar capital allocation in backtest itself, that's a Phase 2 concern. Just compute trade-level P&L as a percentage and aggregate.

**TP-rate / label-hit-rate divergence is expected.** The training-time label uses the daily *close* and (typically) a 10% threshold. The harness's take-profit fires on the intraday *high* and (typically) a 5% threshold. Both effects push the harness TP rate above the label hit rate even when the model is unchanged. See `harness.py` module docstring for the detailed explanation; do not interpret the gap as a bug.
**Concurrent positions:** for simplicity, treat each prediction as an independent trade. Don't enforce position limit during backtest. The "6 concurrent positions" rule is a live trading constraint, not a backtest constraint. If results show too many concurrent trades on certain days, flag for live policy adjustment.

**Missing data handling:**

- Missing entry-day price: skip the trade entirely. Do not wait for next available day. Log skipped trade with coin and date.
- 1-2 missing days within hold window: forward-fill OHLCV from last known close. Position tracking continues using carried-forward values. Log forward-fills per trade.
- 3 or more missing days: exit position at last known close before gap. Mark exit_reason as `data_gap`. Log the trade.

These rules apply to all five exit policies. Funding payments during forward-filled days are zero (no funding rows = no funding cost incurred during the gap).

---
## Cost Model
**Fees:**
- Entry: maker fee 0.02% (assume limit orders fill within 24h, otherwise abandon trade)
- Exit: taker fee 0.05% (conservative assumption that exits use market orders for speed)
- Round-trip baseline: 0.07% per trade
**Funding:**
- Binance funding cadence varies per coin: 8h is the historical standard (00:00 / 08:00 / 16:00 UTC), but many newer pairs run on a 4h schedule (00 / 04 / 08 / 12 / 16 / 20 UTC) and a few on a 1h schedule. The cost model sums the **actual rows** present in `crypto_funding_rates` within the hold window regardless of cadence, so 4h coins naturally accrue 3× the funding pressure of standard coins per unit time, and 1h coins 24×.
- Long position pays positive funding rate, receives negative
- Cost = position_notional × sum_of_funding_rates_during_hold
- If a hold spans at least one day but the table contains no rows in the window, the cost model treats it as zero and logs a warning; the harness surfaces a count of such warnings in the run summary so silent data gaps don't get baked into P&L numbers unnoticed.
**Slippage:**
- Tier 1 (BTC, ETH, SOL, top 10 by volume): 0.02% per side
- Tier 2 (other top 30): 0.05% per side
- Tier 3 (rest of universe): 0.10% per side
Slippage classification done at backtest run time based on coin's average daily volume during the trade period.
**Total cost per trade:** entry fee + exit fee + entry slippage + exit slippage + funding payments. Recorded as separate fields for diagnostic visibility.
---
## Policies to Test (Phase 1A: Base Grid)
**Horizons:** 5d, 10d, 20d (separate models, separate runs)
**Exit policies:**
- **A:** Fixed TP at +5%, no stop, time stop at horizon
- **B:** Fixed TP at +5%, fixed -3% stop, time stop at horizon
- **C:** Fixed TP at +5%, ATR-based stop (2x daily 14d ATR using `atr_pct_14d`; since the field is already a fraction, `stop_price = entry_price * (1 - 2 * atr_pct_14d)`), time stop at horizon
- **D:** Trailing stop at 50% of peak profit with activation threshold (default 1%; trail arms only when `peak >= entry × 1.01`), no fixed TP, time stop at horizon
- **E:** Tiered exit: 50% off at +5%, 50% with trailing stop at 50% of peak, time stop at horizon
**Selection rules:**
- Top N (default N = 6)
- Threshold (default p > 0.55)
**Total base combinations:** 3 × 5 × 2 = 30 runs
---
## Sensitivity Tests (Phase 1B: only on top 3 base winners)
After base grid identifies the top 3 (horizon, policy, selection) combinations, run sensitivity tests on each:
- **Top N variants:** N = 5, 6, 7, 8
- **Threshold variants:** p > 0.50, 0.55, 0.60, 0.65
- **ATR multiplier (policy C):** 1.5x, 2x, 2.5x, 3x
- **Trailing % (policies D and E):** 30%, 50%, 70%
- **Trail activation % (policy D):** 0%, 1%, 2%, 3%
- **Take profit level (policies A, B, C, E):** +3%, +5%, +8% (sensitivity to target choice)
Sensitivity tests answer "is the winner robust to parameter perturbation?" If a 50% trailing stop wins but 40% and 60% both perform much worse, the winner is fragile.
---
## Output Schema
**`crypto_backtest_runs`:**
| Column | Type | Description |
|---|---|---|
| run_id | UUID | Unique run identifier |
| run_timestamp | TIMESTAMP | When the run completed |
| horizon | INT | 5, 10, or 20 |
| exit_policy | TEXT | A, B, C, D, or E |
| selection_rule | TEXT | top_n or threshold |
| parameters | JSON | Policy-specific params (TP%, stop%, ATR mult, etc.) |
| date_start | DATE | First prediction date in run |
| date_end | DATE | Last prediction date in run |
| n_predictions | INT | Total predictions evaluated |
| n_trades | INT | Total trades executed |
**`crypto_backtest_trades`:**
| Column | Type | Description |
|---|---|---|
| run_id | UUID | Foreign key to runs |
| trade_id | UUID | Unique per trade |
| coin | TEXT | Symbol |
| entry_date | DATE | Entry date |
| entry_price | DECIMAL | |
| exit_date | DATE | Exit date |
| exit_price | DECIMAL | |
| exit_reason | TEXT | tp, sl, trailing, time, data_gap |
| holding_days | INT | |
| gross_pnl_pct | DECIMAL | Before costs |
| fee_pct | DECIMAL | Total fees |
| slippage_pct | DECIMAL | Total slippage |
| funding_pct | DECIMAL | Net funding cost (negative if received) |
| net_pnl_pct | DECIMAL | After all costs |
| probability_at_entry | DECIMAL | Model's prediction confidence |
**`crypto_backtest_summary`:**
One row per run, pre-computed for fast comparison:
| Column | Type | Description |
|---|---|---|
| run_id | UUID | |
| net_pnl_total_pct | DECIMAL | Cumulative net return |
| net_pnl_annualized_pct | DECIMAL | Annualized |
| sharpe_ratio | DECIMAL | |
| max_drawdown_pct | DECIMAL | |
| hit_rate | DECIMAL | % trades with net_pnl > 0 |
| avg_winner_pct | DECIMAL | |
| avg_loser_pct | DECIMAL | |
| profit_factor | DECIMAL | sum(winners) / abs(sum(losers)) |
| avg_holding_days | DECIMAL | |
| pct_exits_tp | DECIMAL | % trades exited via take profit |
| pct_exits_sl | DECIMAL | % trades exited via stop loss |
| pct_exits_time | DECIMAL | % trades exited via time stop |
| total_fees_paid_pct | DECIMAL | Sum of fees as % of cumulative notional |
| total_funding_paid_pct | DECIMAL | Net funding cost |

**Metrics methodology — sum-of-fractions caveat.** `metrics.py` builds the daily P&L series by **summing** each trade's `net_pnl_pct` on its `exit_date` (no portfolio-weighting, no compounding). Equity is `1.0 + cumsum(daily_pnl)`. As a result, on days where many trades exit simultaneously the daily series can swing by more than 100 %, and `sharpe_ratio` / `max_drawdown_pct` are inflated relative to a true portfolio simulation that splits a fixed bankroll across N concurrent positions. Because every config in the grid uses the same methodology, **ranking is preserved** — these numbers are valid for picking the best (horizon, policy, selection) combination. For realistic absolute dollar P&L, drawdown, and Sharpe, refer to the simulated portfolio projection in `report.py` (Phase 1B step 7), which sizes equally-weighted concurrent positions out of a fixed starting capital and compounds.

---
## Reporting
**Primary deliverable:** ranking table sorted by Sharpe ratio.
| Rank | Run | Net P&L (annualized) | Sharpe | Max DD | Hit Rate | Profit Factor |
|---|---|---|---|---|---|---|
**Per-run detail report (top 3):**
- Equity curve chart (% returns)
- Drawdown curve chart (% from peak)
- Trade P&L distribution histogram
- Exit reason breakdown
- Cost breakdown (fees vs funding vs slippage)
- Per-month returns table
**Simulated portfolio projection (top 3):**
Reframes trade-level % returns into dollar terms for easier interpretation. Assumes:
- Starting capital: $1000
- Sizing: equal weight, 80% capital deployed across 6 concurrent positions
- Per-position margin at start: ~$133
- Leverage: 1x baseline (raw notional matches margin); 2x reported as a parallel view
- Compounding: position size scales with portfolio equity. Each new trade is sized off current equity, not the original $1000
Outputs per run:
- Equity curve in dollars
- Max drawdown in dollars and as % of peak equity
- Final portfolio value
- Best month and worst month in dollars
- Number of months in drawdown
This view is purely cosmetic. It does not affect ranking or decision criteria. The percentage metrics drive policy choice; the dollar view makes results concrete and easier to reason about emotionally.
**Sensitivity report (Phase 1B):**
For each of top 3 base runs, table showing how key metrics change as parameters are perturbed. Identifies which combinations are robust vs fragile.
---
## Decision Criteria
**Pass to Phase 2 if at least one combination meets all of:**
- Net P&L annualized > 5%
- Sharpe ratio > 1.0
- Max drawdown < 25%
- Profit factor > 1.3
- Sensitivity test shows the winner is not fragile (key metrics don't collapse with ±20% parameter perturbation)
**If multiple combinations pass:** pick the one with highest Sharpe, ties broken by lowest drawdown.
**If no combination passes:** halt. Do not proceed to Phase 2. Reassess the model, the universe, or the underlying premise.
---
## Implementation Notes
- Pure Python, matches existing stack
- DuckDB for storage (read-only access to existing tables, write access to new tables only)
- Pandas + NumPy for analysis
- Matplotlib or Plotly for charts
- No live Binance API calls required
- Runtime estimate: 30-60 minutes for full grid on a year of predictions
## Out of Scope for Phase 1
- Training new models with different targets (defer to post-Phase 3 if needed)
- Slippage modeling beyond simple tier-based assumption
- Concurrent position limit enforcement (Phase 2 concern)
- Tax cost modeling (Phase 4 concern)
- Live API integration (Phase 2 onwards)
