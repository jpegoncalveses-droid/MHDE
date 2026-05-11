"""Investigate the Phase 1B expected_hit_rate vs. walkfold OOS actual_hit
discrepancy (87% vs ~47%). READ-ONLY — no DB writes.

Three hypotheses tested:
  H1 — Definition mismatch (different "hit" semantics)
  H2 — Time-window cherry-picking
  H3 — Selection-mode difference (top_n vs. raw walkfold)
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

DB = "/home/jpcg/MHDE/data/mhde.duckdb"
RUN_ID = "backtest_10d_D_top_n_a02e15a0"

pd.set_option("display.max_rows", 60)
pd.set_option("display.width", 160)

conn = duckdb.connect(DB, read_only=True)


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


hr("1. Phase 1B winner row — runs + summary")

run_row = conn.execute("""
    SELECT run_id, horizon, exit_policy, selection_rule, parameters,
           date_start, date_end,
           n_predictions_seen, n_trades,
           n_skipped_duplicates, n_skipped_missing_atr,
           n_data_gap_exits, n_excluded_by_funding_floor
    FROM crypto_backtest_runs WHERE run_id = ?
""", [RUN_ID]).fetchone()
print("runs row:", run_row)

if run_row is None:
    raise SystemExit("Phase 1B winner row not found.")

(_, horizon, exit_policy, selection_rule, params_json,
 date_start, date_end, n_pred_seen, n_trades_row,
 n_dup, n_missing_atr, n_gap, n_below_floor) = run_row
print("\nparameters JSON:", json.dumps(json.loads(params_json), indent=2))

summary = conn.execute("""
    SELECT net_pnl_total_pct, net_pnl_annualized_pct, sharpe_ratio,
           max_drawdown_pct, hit_rate, avg_winner_pct, avg_loser_pct,
           profit_factor, avg_holding_days,
           pct_exits_tp, pct_exits_sl, pct_exits_trailing,
           pct_exits_time, pct_exits_data_gap
    FROM crypto_backtest_summary WHERE run_id = ?
""", [RUN_ID]).fetchone()
print("\nsummary row:")
for k, v in zip([
    "net_pnl_total_pct", "net_pnl_annualized_pct", "sharpe_ratio",
    "max_drawdown_pct", "hit_rate", "avg_winner_pct", "avg_loser_pct",
    "profit_factor", "avg_holding_days",
    "pct_exits_tp", "pct_exits_sl", "pct_exits_trailing",
    "pct_exits_time", "pct_exits_data_gap",
], summary):
    print(f"  {k:30s} = {v}")


hr("2. Trade-level hit-rate breakdown — compute hit_rate from trades")

trade_stats = conn.execute("""
    WITH t AS (
        SELECT * FROM crypto_backtest_trades WHERE run_id = ?
    )
    SELECT
        COUNT(*)                                            AS n_trades,
        SUM(CASE WHEN net_pnl_pct  >  0 THEN 1 ELSE 0 END)  AS n_net_winners,
        SUM(CASE WHEN gross_pnl_pct > 0 THEN 1 ELSE 0 END)  AS n_gross_winners,
        AVG(net_pnl_pct)   * 100.0                          AS avg_net_pct,
        AVG(gross_pnl_pct) * 100.0                          AS avg_gross_pct,
        MIN(entry_date) AS first_entry, MAX(entry_date) AS last_entry,
        MIN(exit_date)  AS first_exit,  MAX(exit_date)  AS last_exit
    FROM t
""", [RUN_ID]).fetchone()
print("trade stats:")
print(f"  n_trades        = {trade_stats[0]}")
print(f"  net winners     = {trade_stats[1]}  ({trade_stats[1]/trade_stats[0]:.4f})")
print(f"  gross winners   = {trade_stats[2]}  ({trade_stats[2]/trade_stats[0]:.4f})")
print(f"  avg net pct     = {trade_stats[3]:+.3f}%")
print(f"  avg gross pct   = {trade_stats[4]:+.3f}%")
print(f"  entry range     = {trade_stats[5]} → {trade_stats[6]}")
print(f"  exit range      = {trade_stats[7]} → {trade_stats[8]}")

print("\nexit-reason distribution:")
exit_dist = conn.execute("""
    SELECT exit_reason, COUNT(*) AS n,
           AVG(net_pnl_pct)*100   AS avg_net_pct,
           AVG(gross_pnl_pct)*100 AS avg_gross_pct,
           SUM(CASE WHEN net_pnl_pct > 0 THEN 1 ELSE 0 END) AS net_win
    FROM crypto_backtest_trades
    WHERE run_id = ?
    GROUP BY exit_reason
    ORDER BY n DESC
""", [RUN_ID]).fetchdf()
print(exit_dist.to_string(index=False))


hr("3. Distribution of net_pnl_pct around zero")

dist = conn.execute("""
    SELECT
      SUM(CASE WHEN net_pnl_pct >  0.10 THEN 1 ELSE 0 END) AS gt_10pct,
      SUM(CASE WHEN net_pnl_pct >  0.05 AND net_pnl_pct <= 0.10 THEN 1 ELSE 0 END) AS p5_10,
      SUM(CASE WHEN net_pnl_pct >  0.00 AND net_pnl_pct <= 0.05 THEN 1 ELSE 0 END) AS p0_5,
      SUM(CASE WHEN net_pnl_pct >= -0.05 AND net_pnl_pct <= 0.00 THEN 1 ELSE 0 END) AS n0_5,
      SUM(CASE WHEN net_pnl_pct >= -0.10 AND net_pnl_pct < -0.05 THEN 1 ELSE 0 END) AS n5_10,
      SUM(CASE WHEN net_pnl_pct < -0.10 THEN 1 ELSE 0 END) AS lt_n10pct
    FROM crypto_backtest_trades WHERE run_id = ?
""", [RUN_ID]).fetchone()
print(f"  > +10 pct net PnL : {dist[0]}")
print(f"  +5..+10 pct       : {dist[1]}")
print(f"   0..+5 pct        : {dist[2]}")
print(f"  -5..0 pct         : {dist[3]}")
print(f"  -10..-5 pct       : {dist[4]}")
print(f"  < -10 pct         : {dist[5]}")


hr("4. H1 — Definition mismatch — label hit rate vs net P&L hit rate")

# Get the universe of walkfold predictions used by the run, then apply
# top_n=6 selection per day, then check label `actual_hit` rate.
preds = conn.execute("""
    SELECT symbol AS coin,
           prediction_date AS date,
           predicted_probability AS probability,
           actual_max_return,
           actual_hit
    FROM crypto_ml_predictions
    WHERE model_id LIKE 'crypto_10d_walkfold_%'
      AND horizon = '10d'
      AND prediction_date >= ?
      AND prediction_date <= ?
    ORDER BY prediction_date, symbol
""", [date_start, date_end]).fetchdf()
print(f"walkfold 10d predictions in [{date_start}, {date_end}]: n={len(preds)}")
print(f"  with non-null actual_hit: {preds['actual_hit'].notna().sum()}")

# Apply the same top-6 ranking the harness uses.
sorted_p = preds.sort_values(
    ["date", "probability", "coin"], ascending=[True, False, True],
    kind="mergesort",
).reset_index(drop=True)
sorted_p["rank"] = sorted_p.groupby("date").cumcount() + 1
top6 = sorted_p[sorted_p["rank"] <= 6]

label_hit_rate = top6["actual_hit"].mean()
print(f"\nTop-6 daily label hit_rate over winner window: {label_hit_rate:.4f}  "
      f"(n={top6['actual_hit'].notna().sum()})")
print("→ This is the 'walkfold hit rate' definition the operator queried.")

# Compare to summary.hit_rate (winners by net P&L).
print(f"summary.hit_rate (net P&L > 0)               : {summary[4]:.4f}")
print(f"DIFFERENCE                                   : "
      f"{summary[4] - label_hit_rate:+.4f}")


hr("5. H2 — Time-window cherry-pick check — compare 12-month operator window vs harness window")

op_label = conn.execute("""
    WITH ranked AS (
        SELECT prediction_date, symbol, predicted_probability, actual_hit,
               ROW_NUMBER() OVER (
                   PARTITION BY prediction_date
                   ORDER BY predicted_probability DESC, symbol ASC
               ) AS rk
        FROM crypto_ml_predictions
        WHERE model_id LIKE 'crypto_10d_walkfold_%'
          AND horizon = '10d'
          AND prediction_date BETWEEN '2025-05-01' AND '2026-04-30'
          AND actual_hit IS NOT NULL
    )
    SELECT COUNT(*)                                     AS n,
           AVG(CASE WHEN actual_hit THEN 1 ELSE 0 END)  AS hit_rate
    FROM ranked WHERE rk <= 6
""").fetchone()
print(f"Operator window 2025-05-01..2026-04-30 top-6 label hit_rate: "
      f"{op_label[1]:.4f}  (n={op_label[0]})")

print("\nMonthly label hit rate (top-6) over harness window for cross-check:")
monthly = conn.execute("""
    WITH ranked AS (
        SELECT prediction_date, symbol, predicted_probability, actual_hit,
               actual_max_return,
               ROW_NUMBER() OVER (
                   PARTITION BY prediction_date
                   ORDER BY predicted_probability DESC, symbol ASC
               ) AS rk
        FROM crypto_ml_predictions
        WHERE model_id LIKE 'crypto_10d_walkfold_%'
          AND horizon = '10d'
          AND prediction_date >= ? AND prediction_date <= ?
          AND actual_hit IS NOT NULL
    )
    SELECT strftime(prediction_date, '%Y-%m')               AS month,
           COUNT(*)                                         AS n,
           AVG(CASE WHEN actual_hit THEN 1 ELSE 0 END)*100  AS hit_pct,
           AVG(actual_max_return)*100                       AS avg_max_ret_pct
    FROM ranked WHERE rk <= 6
    GROUP BY month ORDER BY month
""", [date_start, date_end]).fetchdf()
print(monthly.to_string(index=False))


hr("6. H1 cross-check — net P&L hit rate per month from trades table")

monthly_pnl = conn.execute("""
    SELECT strftime(entry_date, '%Y-%m')                    AS month,
           COUNT(*)                                         AS n,
           AVG(CASE WHEN net_pnl_pct > 0 THEN 1.0 ELSE 0.0 END)*100 AS net_win_pct,
           AVG(net_pnl_pct)*100                             AS avg_net_pct,
           SUM(net_pnl_pct)*100                             AS sum_net_pct
    FROM crypto_backtest_trades
    WHERE run_id = ?
    GROUP BY month ORDER BY month
""", [RUN_ID]).fetchdf()
print(monthly_pnl.to_string(index=False))


hr("7. H3 — Selection mode — does harness top_n match the operator's top-6?")

# How many trades did the harness actually open per day? (Should be ≤ 6 per
# day; lower if duplicate-position-guard fired or entry price missing.)
per_day = conn.execute("""
    SELECT entry_date, COUNT(*) AS n_trades
    FROM crypto_backtest_trades WHERE run_id = ?
    GROUP BY entry_date ORDER BY entry_date
""", [RUN_ID]).fetchdf()
print(f"trades per entry_date: mean={per_day['n_trades'].mean():.2f}  "
      f"median={per_day['n_trades'].median():.0f}  "
      f"min={per_day['n_trades'].min()}  max={per_day['n_trades'].max()}  "
      f"days={len(per_day)}")
print(f"days with full 6-slot selection: "
      f"{(per_day['n_trades']==6).sum()} / {len(per_day)}")
print(f"days with <6 slots filled       : "
      f"{(per_day['n_trades']<6).sum()}")
print(f"sum of trades opened            : {per_day['n_trades'].sum()}  "
      f"(runs.n_trades = {n_trades_row})")


hr("8. Bottom-line numbers")
print(f"  expected_hit_rate in active_spec.json    : 0.871244635193133")
print(f"  summary.hit_rate (net P&L > 0)           : {summary[4]:.6f}")
print(f"  walkfold top-6 LABEL hit rate, harness window  : {label_hit_rate:.6f}")
print(f"  walkfold top-6 LABEL hit rate, operator window : {op_label[1]:.6f}")
print(f"  expected_annualized_return_pct           : "
      f"{summary[1]*100:.2f}% (×100 from frac per write_active_spec.py)")
print(f"  trades n={trade_stats[0]}, net winners={trade_stats[1]}")

conn.close()
