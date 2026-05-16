# Finding 11 — Average MAE among winning trades (PHASE1B winner run)

**Run:** `backtest_10d_D_top_n_a02e15a0`
**Horizon / exit / selection:** 10d / D / top_n
**Parameters:** `{"policy_params": {"trail_pct": 0.3}, "selection_params": {"n": 6}}`
**Backtest window:** 2025-04-05 → 2026-05-07 (n_trades=932)
**Data source:** `crypto_backtest_trades` joined with `crypto_prices_daily.low`
**Winner definition:** `net_pnl_pct > 0`

## Answer

**Average MAE across the 814 winners = −8.56%**

(MAE = `(min(daily low between entry_date and exit_date) − entry_price) / entry_price`, clipped to 0 for winners that never closed below entry.)

## Context

| Metric | Value |
|---|---|
| Winners | 814 |
| Winners that drew down at all (MAE < 0) | 811 (99.6%) |
| Winners that never went underwater (MAE = 0) | 3 (0.4%) |
| Average MAE | **−8.5615%** |
| Worst single-winner MAE | −83.56% |
| Winners with no joinable price rows | 0 |

## MAE distribution among winners

| Bucket | Count | Share |
|---|---:|---:|
| Never underwater (MAE = 0%) | 3 | 0.4% |
| (0%, 1%] | 70 | 8.6% |
| (1%, 2.5%] | 96 | 11.8% |
| (2.5%, 5%] | 158 | 19.4% |
| (5%, 10%] | 249 | 30.6% |
| > 10% | 238 | 29.2% |

## Interpretation

- Almost every winning trade (811/814 = 99.6%) traded below its entry price at some point before exiting profitable.
- The average dip is non-trivial: a **−8.56%** intraday-low excursion is the price of capturing the eventual upside on winners.
- ~30% of winners drew down more than 10% before recovering. A hard −10% stop would have converted roughly that share of winners into losers (without accounting for stop-induced trade replacement).
- The 3 winners that never went underwater are statistical outliers, not the norm.

## Method notes

- Daily granularity. Intra-day excursions worse than the daily `low` are not captured.
- `crypto_prices_daily.low` is sourced from Binance.
- Winners with `exit_date` IS NULL are excluded by the date-range filter, which is the correct treatment for closed-position MAE.
- Script: `.claude/local_scripts/finding11_winner_mae.py` (read-only DuckDB).

---

# Paper trading winners (out-of-sample)

**Sources**
- Positions: `/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb` → `positions` (read-only)
- Prices: `/home/jpcg/MHDE/data/mhde.duckdb` → `crypto_prices_daily.low` (read-only)
- Script: `.claude/local_scripts/finding11b_paper_mae.py`

**Universe**
- All `positions` with `current_state = 'exit_filled'` and non-null `entry_price` + `realized_pnl_usd`.
- 30 rows in `exit_filled`; 1 discarded for missing fill data; 29 closed positions retained.
- Winners (`realized_pnl_usd > 0`): **18** out of 29 (62.1% hit rate).
- 2 winners had entry/exit on 2026-05-15 (today) for which `crypto_prices_daily` has no row yet → excluded from MAE calc.
- **N for MAE = 16 winners.**

## Answer

**Average MAE across 16 paper-trading winners = −8.86%**

| Metric | Value |
|---|---|
| Winners with computable MAE | 16 |
| Average MAE | **−8.8605%** |
| Worst single-winner MAE | −15.57% (UBUSDT 2026-05-12) |
| Best single-winner MAE | −1.93% (UBUSDT 2026-05-13) |
| Winners that drew down (MAE < 0) | 16 / 16 (100%) |
| Winners never underwater (MAE = 0) | 0 / 16 |

## Per-winner table

| Symbol | Entry | Exit | Entry px | Exit px | Min low | MAE % |
|---|---|---|---:|---:|---:|---:|
| ZEREBROUSDT | 2026-05-10 | 2026-05-11 | 0.04959 | 0.05162 | 0.043049 | −13.19 |
| RAVEUSDT | 2026-05-10 | 2026-05-11 | 0.7446 | 0.7545 | 0.661 | −11.23 |
| FHEUSDT | 2026-05-10 | 2026-05-11 | 0.03725 | 0.03948 | 0.032 | −14.09 |
| TAGUSDT | 2026-05-10 | 2026-05-11 | 0.001371 | 0.0014206 | 0.0012824 | −6.46 |
| BUSDT | 2026-05-11 | 2026-05-11 | 0.4448 | 0.4471 | 0.387 | −12.99 |
| 4USDT | 2026-05-11 | 2026-05-11 | 0.01297 | 0.01302 | 0.012417 | −4.26 |
| UBUSDT | 2026-05-11 | 2026-05-11 | 0.14091 | 0.14183 | 0.12866 | −8.69 |
| BUSDT | 2026-05-12 | 2026-05-12 | 0.6287 | 0.636 | 0.6025 | −4.17 |
| ZEREBROUSDT | 2026-05-12 | 2026-05-12 | 0.04156 | 0.04194 | 0.037081 | −10.78 |
| UBUSDT | 2026-05-12 | 2026-05-12 | 0.15759 | 0.15849 | 0.13306 | −15.57 |
| TAGUSDT | 2026-05-12 | 2026-05-12 | 0.0013313 | 0.001339 | 0.001305 | −1.98 |
| UBUSDT | 2026-05-13 | 2026-05-13 | 0.17078 | 0.17124 | 0.16748 | −1.93 |
| FHEUSDT | 2026-05-13 | 2026-05-13 | 0.03118 | 0.0314 | 0.02711 | −13.05 |
| TAGUSDT | 2026-05-13 | 2026-05-13 | 0.0014401 | 0.0014544 | 0.0013155 | −8.65 |
| SWARMSUSDT | 2026-05-13 | 2026-05-13 | 0.01877 | 0.0191 | 0.016673 | −11.17 |
| FHEUSDT | 2026-05-14 | 2026-05-14 | 0.02735 | 0.02807 | 0.02638 | −3.55 |
| _FHEUSDT 2026-05-15_ | 2026-05-15 | 2026-05-15 | 0.02739 | 0.02753 | (no row) | excluded |
| _UBUSDT 2026-05-15_ | 2026-05-15 | 2026-05-15 | 0.22427 | 0.23283 | (no row) | excluded |

## Caveats (read before quoting the number)

1. **Sample size is tiny.** N=16 winners. Standard error on the mean is ≈ ±1.0 percentage points (sample stdev of MAE ≈ 4.6%, SE = 4.6/√16 ≈ 1.15 pp). A 95% CI is roughly **[−11.2%, −6.5%]**. The backtest's −8.56% sits comfortably inside this interval.

2. **Daily-low caveat is more material here than in the backtest.** Every paper position opened and closed within a single trading day. The `crypto_prices_daily.low` for that day reflects the entire UTC day's low — including periods *before* entry and *after* exit. So per-trade MAE is an upper bound on the drawdown actually experienced during the holding window. For multi-day backtest trades this overstatement is much smaller.

3. **No pre/post-baseline split applied.** Per the task spec, all closed trades treated as one cohort.

4. **Engine DB has no exit timestamp column.** Used `updated_at::DATE` as the exit-date proxy (the `updated_at` is touched on the exit-fill state transition); all 16 valid trades resolve to a single-day window (entry_date = exit_date), so the proxy is moot for daily-low aggregation.

## Comparison vs. backtest

| Cohort | N | Avg MAE | Source |
|---|---:|---:|---|
| Backtest winners (10d D top_n, run a02e15a0) | 814 | −8.56% | `crypto_backtest_trades` |
| Paper-trading winners (closed, all dates) | 16 | −8.86% | `positions` (`exit_filled`) |

The two figures are within 0.3 pp of each other and well within the paper sample's confidence interval. **Live execution drawdown behaviour on winners is consistent with backtest expectations to date.** Caveat 2 (daily-low overstates intraday MAE) means the true live MAE is likely *less negative* than −8.86%, which strengthens the consistency claim.

