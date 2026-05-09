Path to $`1000 Live Trading on Binance Futures

Complete plan covering all 5 phases from current state to live deployment, including Binance setup decisions and strategic context.

Current State Snapshot


Phase
Status
Description

Phase 0
In progress
Live calibration validation, ~6 weeks

Phase 1A
Complete
Walk-forward OOS prediction backfill (40,074 predictions)

Phase 1B (base)
Complete
20-config execution backtest, top 3 Policy D variants

Phase 1B (sensitivity)
Authorized, not yet run
Parameter sweeps on top 3 winners

Phase 2
Not started
Execution layer build (paper trading mode)

Phase 3
Not started
Paper trading validation, 4-8 weeks

Phase 4
Not started
Live deployment with `$1000



Guiding Principles

1. Don't disrupt the running prediction pipeline. New work lives in isolated modules.
2. Prove each layer before adding the next.
3. Let backtests decide policy, not intuition.
4. Live trading is the last step, not the first.
5. The first $`1000 is for validating the system end-to-end, not for profit. Treat it as tuition.

End Goal

Deploy the prediction model into live trading on Binance Futures with `$1000, after validating both the prediction signal in production AND the execution stack independently.

───

Phase 0: Live Calibration Validation

Status: Active, ~6 weeks total.

Goal: Confirm the model's walk-forward calibration holds in production.

Pass criteria:
• Top-N hit rate within ±25% relative to walk-forward expectation
• Lift over base rate stays > 1.3 over rolling 30-day window
• No systematic over- or under-confidence in calibration buckets
• Minimum sample: 200 predictions with elapsed horizons

Decision gate: if Phase 0 fails, halt project regardless of other progress. Reassess model.

Tooling (added 2026-05-09):

• Formal evaluation: `venv/bin/python main.py crypto phase0-report` produces a markdown go/no-go document covering all four criteria per active model. Saves to `data/reports/phase0_report_YYYY-MM-DD.md`. The verdict is INTERIM until the 200-sample gate is met; all four metrics are still computed and shown so the operator can track the trajectory week over week.
• Weekly interim monitoring: `mhde-monitor-phase0-calibration.timer` (Sundays 06:00 UTC, system-level, `User=jpcg`) runs `monitoring/phase0_calibration.py`. Three alert paths: drift signal (lift < 1.5×, rolling-precision/baseline < 0.85, or 3+ consecutive calibration buckets > 10pp off), sample-rate slowdown (projected gate ETA slipped > 7 days vs prior week), and one-shot "200 reached" notification idempotently fired via `phase0_milestones`.
• Implementation: `crypto/ml/phase0_evaluate.py` (4 criterion evaluators + reliability diagram + sample projection), `crypto/ml/phase0_report.py` (markdown renderer), `monitoring/phase0_calibration.py` (weekly monitor). Crypto-only wired today; engine extension is a one-config-block change.
• Calibration drift definition is currently absolute (3+ consecutive buckets off > 10pp in the current week's data). Week-over-week relative drift detection is deferred to KI-126 until weekly snapshots accumulate.

───

Phase 1A: Walk-Forward Prediction Backfill

Status: Complete.

Goal: Generate legitimate walk-forward out-of-sample predictions across the full historical window so Phase 1B can produce valid policy comparisons.

What was done:
• Discovered the existing crypto_ml_predictions table contained ~50% in-sample predictions because the live model was trained once and predictions before train_end were technically in-sample.
• Built crypto/ml/backfill_walkforward.py to capture and persist per-fold OOS predictions that the existing walk-forward CV in train.py was already computing internally but discarding.
• Ran 18 folds × 2 horizons (5d, 10d), expanding training window starting 2024-01-01.
• Persisted 40,074 OOS predictions tagged model_id LIKE 'crypto_%_walkfold_%'.
• All 6 validation checks passed: no leakage, coverage within tolerance, outcomes filled, distinct model_ids, is_active integrity preserved, live pipeline unaffected.

Deferred: 20d horizon. Existing retrain.py config covers 5d and 10d only. If Phase 1B winner is at the 10d boundary, add 20d as a separate task.

───

Phase 1B: Execution Backtest

Status: Base grid complete. Sensitivity grid pending.

Goal: Pick the optimal combination of horizon, exit policy, and selection rule using realistic trade simulation on walk-forward predictions.

Modules built

Located in crypto/execution/backtest/:

• costs.py: fees (0.02% maker, 0.05% taker), slippage tiered by liquidity (0.02%/0.05%/0.10%), funding from real historical rates (handles 8h/4h/1h cadences)
• policies.py: 5 exit policies (A: TP only, B: TP + fixed -3% SL, C: TP + ATR SL, D: trailing 50% peak with 1% activation, E: tiered 50% TP + 50% trailing)
• selection.py: Top N or threshold-based daily filtering
• harness.py: trade lifecycle (entry, daily walk-forward, exits, missing-data handling, duplicate skipping)
• metrics.py: Sharpe, drawdown, profit factor, hit rate, cost diagnostics
• runner.py: grid orchestrator, deterministic run_id, idempotent re-runs
• report.py: ranking tables, per-run detail, simulated $`1000 portfolio projection

Test count: 202 tests across the full backtest + backfill suite, all passing.

Base grid results (20 runs)

Date range: 2025-04-05 to 2026-05-08 (~13 months, filtered by funding-rate data coverage floor).

Top 3 by Sharpe:


Rank
Config
Sharpe
Max DD
Realistic Final $ from $1k
Hit Rate

1
5d / D / threshold
3.78
-42% (sum-of-frac) / -26% (portfolio)
`$7,958
82.5%

2
5d / D / top_n
2.99
-11% / -29%
$`12,229
81.2%

3
10d / D / top_n
2.95
-24% / -28%
`$24,240
86.1%



Headline findings:
• All top 3 are Policy D (trailing stop with 1% activation).
• Policies A, B, C are net losers across all 12 of their configurations. Fixed-distance stops fire on crypto noise.
• 55% of selection signals are duplicate skips (same coin already open). Real Binance constraint, materially reduces realized capital deployment.
• Capacity binding: even rank 3 drops 49% of candidate trades at the 6-position cap.

Decision criteria evaluation

Spec requires all four: annualized > 5%, Sharpe > 1.0, max drawdown < 25%, profit factor > 1.3.

Base grid result (initial state, 2026-05-08): no config passed all four. All top 3 failed max drawdown < 25% by 1-4 percentage points using realistic-portfolio numbers. Annualized return, Sharpe, and profit factor all passed comfortably.

Sensitivity grid result (executed 2026-05-09): 8 of 27 single-axis-around-original-3 configs pass all four gates. The dominant axis change is `trail_pct: 0.50 → 0.30`. See "Phase 1B selected winner" below for the chosen policy.

Sensitivity grid (executed)

Per the spec, for each top-3 base winner sweep one axis at a time:
• trail_pct: 0.30, 0.50, 0.70
• activation_pct: 0.0, 0.01, 0.02, 0.03
• Selection: top_n in {5,6,7,8} or threshold in {0.50, 0.55, 0.60, 0.65}

11 configs per top run, 33 total. Actual wall-clock ~25s. Iterated CLI invocations were observed to produce multi-axis configs through greedy axis-by-axis hill climbing — guarded against in the runner CLI from 2026-05-09 onward; see KNOWN_ISSUES KI-125.

Phase 1B selected winner: `backtest_10d_D_top_n_a02e15a0`

Source: strict single-axis sensitivity slice around the base grid's top-3.

| Field | Value |
|---|---|
| run_id | `backtest_10d_D_top_n_a02e15a0` |
| Horizon | 10d |
| Exit policy | D (trailing stop with activation) |
| Selection rule | top_n |
| `n` | 6 |
| `trail_pct` | **0.30** (changed from base default 0.50 — the dominant sensitivity axis) |
| `activation_pct` | 0.01 (class default; not overridden in stored params) |
| Stored `policy_params` | `{"trail_pct": 0.3}` |
| Stored `selection_params` | `{"n": 6}` |

Portfolio metrics ($1,000 starting capital, 80% deployed across 6 concurrent positions, 1× leverage, 398-day span 2025-04-05 → 2026-05-07):

| Gate | Rule | Realized | Pass? |
|---|---|---|:---:|
| Annualized return | > 5% | +2854% | ✓ |
| Sharpe ratio | > 1.0 | 5.096 | ✓ |
| Max drawdown | < 25% | -23.73% | ✓ (1.27 pp under the gate) |
| Profit factor | > 1.3 | 3.811 | ✓ |

End equity: $32,121.89 from $1,000.
Trades taken: 484 (448 skipped at the 6-position cap).
Best month: +$13,655; worst month: -$1,170. Months in drawdown: 12 of ~13.

Sum-of-fractions metrics (the values stored in `crypto_backtest_summary`): Sharpe 6.32, max DD -17.0%, profit factor 3.13, hit rate 87.1%, 932 trades, avg holding 3.66 days. As documented in "Methodology caveats" below, sum-of-fractions inflates absolute Sharpe / DD vs portfolio reality; ranking is preserved.

Why this winner over alternatives:
• **Cleanest derivation** — single-axis change from a published top-3 base (`backtest_10d_D_top_n_e08cf9da`, originally trail=0.5, n=6).
• **Highest portfolio Sharpe** (5.10) among the strict-slice passers.
• **Drawdown comfortably under 25%** (1.27 pp margin) without relying on multiple simultaneous axis changes.
• **No iteration / no multi-axis stacking** — the result the agreed sensitivity-grid contract emits.

Iterated extras (out of agreed spec): 30 additional configs exist in `crypto_backtest_summary` from chained CLI invocations that re-ranked against sensitivity-found bases. The strongest of these (`backtest_10d_D_top_n_d884e9f2`, two-axis-from-base) hit portfolio Sharpe 6.04, maxDD -12.9%, $80,931 — but its provenance is iterated, not single-axis. A targeted single-invocation sweep around it (per KI-125 follow-up) is documented in `PHASE1B_HANDOFF.md`. The selected winner remains `a02e15a0` unless that follow-up demonstrates a robust local optimum.

───

Phase 2: Execution Layer Build

Status: Not started. Authorized to begin only after Phase 0 calibration passes.

Goal: Build the bot that takes daily predictions and executes trades on Binance Futures Demo.

Phase 1B Selected Winner — inputs to Phase 2

The execution layer must implement these exact parameters (locked 2026-05-09):

| Parameter | Value | Source |
|---|---|---|
| Horizon | **10d** | Phase 1B winner |
| Selection rule | **top_n** | Phase 1B winner |
| Top N | **6** | Phase 1B winner |
| Exit policy | **D** (trailing stop) | Phase 1B base + sensitivity |
| `trail_pct` | **0.30** | Sensitivity (50% → 30%) |
| `activation_pct` | **0.01** | Class default |
| Concurrent positions | 6 target (5-8 range) | Locked Decision (sizing) |
| Capital deployment | 80% of wallet, 20% reserve | Locked Decision (sizing) |
| Per-position size | Equal weight | Locked Decision (sizing) |
| Leverage | 1x or 2x | Locked Decision |
| Margin mode | Isolated | Locked Decision |
| Reference run_id | `backtest_10d_D_top_n_a02e15a0` | DB anchor |

**Phase 2 acceptance criterion**: paper-trading P&L over 4 weeks must track the realistic-portfolio expectation derived from `simulate_portfolio` on `a02e15a0` within the ±20% rolling-30-day band specified in Phase 3.

Tasks:
1. Order placement module (limit orders, fill tracking, retry logic)
2. Position state management
3. Exit logic from Phase 1 chosen spec
4. Risk circuit breakers (account drawdown stop, per-day loss limit)
5. Daily reconciliation script
6. Paper trading mode flag (always demo at this stage)
7. Logging: every signal, decision, order, fill, exit reason, realized P&L

Sizing rule (locked):
• Equal weight per position
• 80% of wallet deployed, 20% reserve
• 6 concurrent positions target, 5-8 range
•  1000 wallet
• Leverage: 1x or 2x

Architecture:
• New module: crypto/execution/ (live)
• New tables: crypto_paper_trades, crypto_paper_positions, crypto_paper_pnl
• New systemd timer: separate from prediction timer
• Reads predictions from existing DB, writes nothing back to ML tables

───

Phase 3: Paper Trading Validation

Status: Not started. Begins after Phase 2 build complete.

Goal: Run the bot in demo mode for 4-8 weeks. Validate execution stack matches backtest.

Tasks:
• Daily monitoring (5 min: wallet, positions, last 24h trades, log review)
• Weekly reconciliation (bot DB vs Binance demo statements)
• Weekly P&L comparison (paper realized vs Phase 1B backtest expectation)
• Bug fixes
• Stress test: API failure, network outage, order rejection

Decision gate (must pass to proceed to Phase 4):
• Paper P&L within ±20% of backtest expectation over rolling 30-day window
• Zero unexplained P&L gaps (every dollar accounted for)
• Reconciliation passes 4 weeks in a row
• Phase 0 calibration also passed

If any fail, debug and extend. Do not proceed under pressure.

───

Phase 4: Live Deployment

Status: Not started. Begins only after Phase 3 passes.

Goal: Switch from demo to live with $`1000.

Pre-deployment checklist:
1. Generate live API key (separate from demo)
2. Live key permissions: Reading + Futures trading. Withdrawals disabled. IP whitelist on VPS IP.
3. Deposit EUR via SEPA from Spanish bank
4. Convert EUR to USDT (limit order on EUR/USDT for best price)
5. Transfer USDT spot wallet to futures wallet
6. Set margin mode to Isolated on each tradeable pair
7. Set leverage to 1x or 2x per pair (pre-set, not at order time)
8. Switch bot config from demo API to live API
9. Switch bot mode from paper to live

Day 1 monitoring: Watch first 3 trades fill manually. Verify positions match bot DB. Confirm fees and funding match expected.

Week 1 review: Any divergence from paper behavior is a stop signal.

Week 4 review: If P&L tracks paper expectations, continue. If not, halt and reassess.

───

Binance Setup Decisions

Account type: USDT-M Perpetual Futures

Not Spot. Reasons:
• Two of the model's key features (funding rates, open interest) only exist on futures markets
• Universe was selected by Binance perpetual futures volume
• Lower fees: 0.02% maker / 0.05% taker on futures vs 0.1% on spot
• Allows shorting (currently long-only by label, but optionality matters)

Leverage: 1x or 2x

Higher leverage doesn't fix weak signal, it amplifies losses and makes liquidation realistic.


Leverage
Adverse move that liquidates

1x
-100% (effectively never)

2x
-50%

5x
-20%

10x
-10%



At 1-2x, normal market noise can't liquidate.

Margin mode: Isolated (per position)

Caps loss per bad trade at the margin assigned to that position. One bad trade can't cascade into liquidating other positions. Cross margin is for sophisticated portfolio strategies, not what we want.

API key: HMAC (System generated)

Simpler than Ed25519 self-generated. At `$1000 scale and demo phase first, the security difference is theoretical. The mitigations that actually protect (IP whitelist, withdrawals disabled, key in env vars not code) work identically with both key types.

Permissions:
• Reading: enabled
• Futures trading: enabled
• Spot/Margin: disabled (live; demo forces this on, ignore)
• Withdrawals: disabled
• Universal Transfer: disabled
• IP whitelist: VPS IP only

Deposit flow

EUR via SEPA from Spanish bank → Spot wallet → Convert to USDT → Internal transfer to Futures wallet.

Conversion via "Convert" feature or EUR/USDT spot pair limit order at mid-price.

Cannot go negative

Worst case loss is the futures wallet balance. Binance auto-liquidates before positions go negative. Insurance fund covers gaps. No legal liability beyond deposited capital.

───

Locked Decisions


Decision
Choice

Sizing
Equal weight, 80% deployed, 20% reserve

Concurrent positions
6 target (5-8 range)

Leverage
1x or 2x

Horizon
Single horizon, picked by Phase 1B (top 3 currently include both 5d and 10d)

Exit policy
Policy D (trailing stop with 1% activation, 50% peak trail) likely; final pick after sensitivity

Selection
top_n=6 or threshold=0.55, picked by Phase 1B sensitivity

Margin mode
Isolated

Universe
Existing 50-coin USDT-M perp universe

API security
HMAC, withdrawals disabled, IP whitelist



───

Strategic Context for Future Sessions

Methodology caveats

The Phase 1B backtest uses sum-of-fractions equity curves (not portfolio-weighted) for trade-level Sharpe and drawdown. Numbers are inflated relative to portfolio reality. Ranking is preserved. Always interpret realistic-portfolio numbers from report.py simulate_portfolio for absolute decisions, not the values in crypto_backtest_summary.

Sharpe is event-day annualized by sqrt(252), which slightly inflates vs calendar-day Sharpe. Annualization is linear, not compound.

Duplicate skipping is significant

55% of daily-selected signals are dropped because the same coin is already open. This is correct (Binance can't have two independent long positions on same pair) but means the effective trade count is roughly half the raw selection count.

The 25% drawdown threshold was a heuristic

Set early in conversation as "where retail traders psychologically cave." Not a hard physical limit. Top 3 portfolio drawdowns are 26-29%, just over. If sensitivity grid can't push under, accepting 26-29% DD may be reasonable for a strategy with otherwise excellent metrics ( 24k over 13 months).

Backtest is still optimistic vs live

Even the realistic portfolio simulation assumes:
• All limit orders fill at intended price
• No exchange outages or latency
• No market impact (probably true at $`1k scale)
• No tax friction

Spain capital gains tax is ~19-26% on realized profits. Phase 3 paper trading will reveal real-world execution friction beyond fees and slippage.

Tax record-keeping

Set up Binance tax export early. Every trade is a taxable event in Spain. Modelo 100/720 requirements should be verified with a Spanish accountant before year-end.

Phase 0 calibration is independent and parallel-safe

The 6-week live calibration validation runs continuously regardless of Phase 1+ progress. Phase 1A, 1B did not touch the live prediction pipeline. Phase 2 will be similarly isolated. Phase 0 must pass before Phase 4 (live trading) regardless of other phase progress.

Files of record in the repo

• docs/PHASE1B_HANDOFF.md: detailed Phase 1B handoff with pending sensitivity command
• crypto/ml/PHASE1A_SPEC.md: Phase 1A walk-forward backfill spec
• crypto/execution/backtest/SPEC.md: Phase 1B execution backtest spec (with all in-flight patches)
• crypto/execution/backtest/: all Phase 1B modules (costs, policies, selection, harness, metrics, runner, report)
• crypto/ml/backfill_walkforward.py: Phase 1A backfill orchestrator
• tests/crypto/: 202 tests across the backtest + backfill suite

DB tables of record

• crypto_ml_predictions: contains both live model predictions (in-sample for most dates) and walkfold backfill predictions (filtered by model_id LIKE 'crypto_%_walkfold_%')
• crypto_ml_model_runs: 38 backfill model_runs entries plus 2 live actives (untouched)
• crypto_backtest_runs, crypto_backtest_trades, crypto_backtest_summary: 20 base grid runs persisted

───

Out of Scope (Explicitly)

• Multiple exit models or ensemble strategies
• Probability-weighted, Kelly, or vol-targeted sizing
• Scaling beyond `$1000 before all gates pass
• Adding coins or features mid-deployment
• Spot trading
• Shorting (current label is +5% upside, long-only)
• 20d horizon (deferred unless 10d is at ranking boundary)
• Regime filters (potential future refinement if drawdown becomes binding)

───

Decision Gates Summary


Gate
When
Pass criteria

Phase 0
End of week 6
Hit rate within ±25%, lift > 1.3, no calibration drift, sample > 200

Phase 1B
After sensitivity grid
Best policy passes annualized > 5%, Sharpe > 1.0, drawdown < 25%, profit factor > 1.3 (using realistic portfolio numbers); OR explicit acceptance of slightly elevated drawdown

Phase 3
End of paper trading
Paper P&L within ±20% of backtest expectation, reconciliation clean 4 weeks

Phase 4 week 1
End of week 1 live
Live behavior matches paper

Phase 4 week 4
End of week 4 live
Live P&L tracks paper expectations



Failure of any gate = halt and reassess. No proceeding under pressure.
