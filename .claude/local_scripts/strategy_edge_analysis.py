"""Strategy-edge + regime + realistic-expectations analysis.

Output: prints all numbers to stdout; the markdown report is composed
separately from this output. READ-ONLY against /home/jpcg/MHDE/data/mhde.duckdb.

Sections (mirrors the operator's brief):
  Part 1 — Selection edge (top-6 vs random-6 vs bottom-6 vs all-universe)
  Part 2 — Regime-based profitability (bull/chop/bear by BTC monthly return)
  Part 3 — Realistic expectations (percentiles)
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

# Make the MHDE project importable when run via `venv/bin/python`.
sys.path.insert(0, "/home/jpcg/MHDE")

import duckdb
import numpy as np
import pandas as pd

DB = "/home/jpcg/MHDE/data/mhde.duckdb"
RUN_ID = "backtest_10d_D_top_n_a02e15a0"
SEED = 20260510  # deterministic random sampling

pd.set_option("display.max_rows", 60)
pd.set_option("display.width", 200)
pd.set_option("display.float_format", lambda x: f"{x:,.4f}")

conn = duckdb.connect(DB, read_only=True)


def hr(title: str) -> None:
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def pct_str(v: float, sign: bool = False) -> str:
    if v != v:  # NaN check
        return "—"
    return f"{v*100:+.2f}%" if sign else f"{v*100:.2f}%"


def percentiles(s: pd.Series, ps=(5, 25, 50, 75, 95)) -> dict:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {f"p{p}": float("nan") for p in ps}
    out = {f"p{p}": float(np.percentile(s, p)) for p in ps}
    return out


# ──────────────────────────────────────────────────────────────────────
# Load walkfold 10d predictions (universe + labels)
# ──────────────────────────────────────────────────────────────────────

WALKFOLD_SQL = """
SELECT symbol,
       prediction_date,
       predicted_probability,
       prediction_threshold,
       actual_max_return,
       actual_max_drawdown,
       actual_hit
FROM crypto_ml_predictions
WHERE model_id LIKE 'crypto_10d_walkfold_%'
  AND horizon = '10d'
  AND actual_hit IS NOT NULL
ORDER BY prediction_date, predicted_probability DESC, symbol
"""
preds = conn.execute(WALKFOLD_SQL).fetchdf()
preds["prediction_date"] = pd.to_datetime(preds["prediction_date"]).dt.date
preds["actual_max_return"] = pd.to_numeric(preds["actual_max_return"], errors="coerce")
preds["actual_max_drawdown"] = pd.to_numeric(preds["actual_max_drawdown"], errors="coerce")
preds["actual_hit"] = preds["actual_hit"].astype(bool)

date_min, date_max = preds["prediction_date"].min(), preds["prediction_date"].max()
n_days = preds["prediction_date"].nunique()
preds_per_day = preds.groupby("prediction_date").size()
print(f"Walkfold 10d predictions loaded: n={len(preds):,}  days={n_days}  "
      f"range=[{date_min} → {date_max}]")
print(f"Predictions per day: mean={preds_per_day.mean():.1f}  "
      f"median={int(preds_per_day.median())}  "
      f"min={preds_per_day.min()}  max={preds_per_day.max()}")


# ──────────────────────────────────────────────────────────────────────
# PART 1 — Selection edge
# ──────────────────────────────────────────────────────────────────────

hr("PART 1 — Quantify the model's selection edge")

# Rank within each day
preds_sorted = preds.sort_values(
    ["prediction_date", "predicted_probability", "symbol"],
    ascending=[True, False, True], kind="mergesort",
).reset_index(drop=True)
preds_sorted["rank"] = preds_sorted.groupby("prediction_date").cumcount() + 1
# Reverse rank for bottom-6
preds_sorted["rev_rank"] = (
    preds_sorted.groupby("prediction_date")["rank"]
    .transform(lambda s: s.max() - s + 1)
)

rng = np.random.default_rng(SEED)

# Build the four buckets per day
def bucket_stats(df: pd.DataFrame, label: str) -> dict:
    daily = df.groupby("prediction_date").agg(
        n=("actual_max_return", "count"),
        mean_max_ret=("actual_max_return", "mean"),
        med_max_ret=("actual_max_return", "median"),
        hit_rate=("actual_hit", "mean"),
        mean_max_dd=("actual_max_drawdown", "mean"),
    ).reset_index()
    if daily.empty:
        return {}
    # Across-day aggregates
    return {
        "label": label,
        "n_days": int(len(daily)),
        "n_obs": int(df["actual_max_return"].notna().sum()),
        "mean_max_ret": float(df["actual_max_return"].mean()),
        "median_max_ret": float(df["actual_max_return"].median()),
        "std_max_ret": float(df["actual_max_return"].std()),
        "hit_rate": float(df["actual_hit"].astype(float).mean()),
        "mean_max_dd": float(df["actual_max_drawdown"].mean()),
        # Per-day mean → distribution across days (for percentile context)
        "daily_mean_p5":  float(daily["mean_max_ret"].quantile(0.05)),
        "daily_mean_p50": float(daily["mean_max_ret"].quantile(0.50)),
        "daily_mean_p95": float(daily["mean_max_ret"].quantile(0.95)),
        "daily_obj": daily,
    }


# (a) Top-6
top6 = preds_sorted[preds_sorted["rank"] <= 6].copy()
# (b) Random 6 — sample per day deterministically
random_rows = []
for d, grp in preds_sorted.groupby("prediction_date"):
    n_pick = min(6, len(grp))
    idx = rng.choice(len(grp), size=n_pick, replace=False)
    random_rows.append(grp.iloc[idx])
random6 = pd.concat(random_rows, ignore_index=True)
# (c) Bottom-6
bot6 = preds_sorted[preds_sorted["rev_rank"] <= 6].copy()
# (d) Top-50 / all-universe per day (full set)
all_univ = preds_sorted.copy()

stats = {
    "top6":    bucket_stats(top6,    "Top-6 (model picks)"),
    "rand6":   bucket_stats(random6, "Random-6 (chance baseline)"),
    "bot6":    bucket_stats(bot6,    "Bottom-6 (anti-strategy)"),
    "universe":bucket_stats(all_univ,"Full daily universe (market beta)"),
}

print("\nSelection-bucket statistics:")
hdr = (f"{'bucket':<35} {'n_obs':>7} {'mean_ret':>10} {'median':>10} "
       f"{'std':>10} {'hit_rate':>9} {'mean_dd':>10}")
print(hdr)
print("-" * len(hdr))
for key in ("top6", "rand6", "bot6", "universe"):
    s = stats[key]
    print(f"{s['label']:<35} {s['n_obs']:>7,} "
          f"{pct_str(s['mean_max_ret']):>10} "
          f"{pct_str(s['median_max_ret']):>10} "
          f"{pct_str(s['std_max_ret']):>10} "
          f"{pct_str(s['hit_rate']):>9} "
          f"{pct_str(s['mean_max_dd']):>10}")

print("\nDelta vs random-6 (model edge over chance):")
base = stats["rand6"]["mean_max_ret"]
base_hit = stats["rand6"]["hit_rate"]
for key in ("top6", "bot6", "universe"):
    s = stats[key]
    print(f"  {s['label']:<35}: Δmean = "
          f"{pct_str(s['mean_max_ret'] - base, sign=True)}  "
          f"Δhit_rate = {pct_str(s['hit_rate'] - base_hit, sign=True)}")

# Best / worst MONTH of top-6
top6["month"] = pd.to_datetime(top6["prediction_date"]).dt.to_period("M").astype(str)
month_top6 = top6.groupby("month").agg(
    n=("actual_max_return", "count"),
    mean_ret=("actual_max_return", "mean"),
    hit_rate=("actual_hit", "mean"),
)
print(f"\nTop-6 monthly distribution: best = {pct_str(month_top6['mean_ret'].max(), sign=True)} "
      f"({month_top6['mean_ret'].idxmax()}), "
      f"worst = {pct_str(month_top6['mean_ret'].min(), sign=True)} "
      f"({month_top6['mean_ret'].idxmin()})")

# Monthly comparison table
month_rand6 = random6.assign(
    month=pd.to_datetime(random6["prediction_date"]).dt.to_period("M").astype(str)
).groupby("month").agg(mean_ret=("actual_max_return", "mean"))
month_bot6 = bot6.assign(
    month=pd.to_datetime(bot6["prediction_date"]).dt.to_period("M").astype(str)
).groupby("month").agg(mean_ret=("actual_max_return", "mean"))

monthly_compare = pd.DataFrame({
    "top6":    month_top6["mean_ret"],
    "random6": month_rand6["mean_ret"],
    "bottom6": month_bot6["mean_ret"],
})
monthly_compare["edge_top_vs_rand"] = monthly_compare["top6"] - monthly_compare["random6"]
monthly_compare["edge_top_vs_bot"] = monthly_compare["top6"] - monthly_compare["bottom6"]
print("\nMonthly mean max-return per bucket (top6 vs random vs bottom6):")
print((monthly_compare * 100).round(2).to_string())

n_months_top_beats_rand = int((monthly_compare["edge_top_vs_rand"] > 0).sum())
n_months_total = int(monthly_compare["edge_top_vs_rand"].notna().sum())
print(f"\nMonths where top-6 mean > random-6 mean: "
      f"{n_months_top_beats_rand}/{n_months_total}")


# ──────────────────────────────────────────────────────────────────────
# PART 2 — Regime classification + harness P&L per regime
# ──────────────────────────────────────────────────────────────────────

hr("PART 2 — Regime-based profitability")

# BTC monthly returns: close on last trade-day of month / close on first day - 1
btc = conn.execute("""
    SELECT trade_date, close
    FROM crypto_prices_daily
    WHERE symbol = 'BTCUSDT'
      AND trade_date BETWEEN ? AND ?
    ORDER BY trade_date
""", [date_min, date_max]).fetchdf()
btc["trade_date"] = pd.to_datetime(btc["trade_date"])
btc = btc.set_index("trade_date").sort_index()

# Resample to month-end (use last close in each month).
month_close = btc["close"].resample("ME").last()
month_open = btc["close"].resample("ME").first()  # first close of month
month_btc_ret = (month_close / month_open) - 1.0
month_btc_ret.index = month_btc_ret.index.to_period("M").astype(str)

# Regime thresholds — symmetric around zero, well-known crypto buckets
BULL_T = 0.05
BEAR_T = -0.05

def regime(r):
    if r != r:
        return "unknown"
    if r >= BULL_T:
        return "bull"
    if r <= BEAR_T:
        return "bear"
    return "chop"

regimes_df = pd.DataFrame({
    "btc_ret": month_btc_ret,
})
regimes_df["regime"] = regimes_df["btc_ret"].apply(regime)
print("Monthly BTC returns + regime classification "
      f"(bull ≥ +{BULL_T*100:.0f}%, bear ≤ {BEAR_T*100:.0f}%, chop otherwise):")
print((regimes_df.assign(btc_ret_pct=lambda d: d["btc_ret"]*100)
       [["btc_ret_pct", "regime"]]).to_string(float_format=lambda x: f"{x:+.2f}"))

regime_counts = regimes_df["regime"].value_counts()
print(f"\nRegime month counts: "
      f"bull={int(regime_counts.get('bull', 0))}, "
      f"chop={int(regime_counts.get('chop', 0))}, "
      f"bear={int(regime_counts.get('bear', 0))}")


# Pull harness trades for the active spec's winner run
trades = conn.execute("""
    SELECT coin, entry_date, exit_date, exit_reason, holding_days,
           gross_pnl_pct, fee_pct, slippage_pct, funding_pct, net_pnl_pct,
           probability_at_entry
    FROM crypto_backtest_trades
    WHERE run_id = ?
    ORDER BY entry_date, coin
""", [RUN_ID]).fetchdf()
trades["entry_date"] = pd.to_datetime(trades["entry_date"])
trades["exit_date"] = pd.to_datetime(trades["exit_date"])
trades["month"] = trades["entry_date"].dt.to_period("M").astype(str)
print(f"\nHarness trades loaded: {len(trades)} trades over "
      f"[{trades['entry_date'].min().date()} → {trades['exit_date'].max().date()}]")


# Per-trade equity contribution: assume each trade sized at
# deploy_fraction/max_positions = 0.8/6 = 0.1333 of equity (the spec's
# sizing). Monthly portfolio return ≈ sum(net_pnl_pct × size) for trades
# CLOSED in that month. This is an approximation that ignores compounding
# and concurrent-position interactions; Part 2's exact portfolio numbers
# come from simulate_portfolio (next).
SIZE_FRAC = 0.8 / 6.0
trades["exit_month"] = trades["exit_date"].dt.to_period("M").astype(str)
trades["contrib"] = trades["net_pnl_pct"] * SIZE_FRAC

monthly_trade_agg = trades.groupby("exit_month").agg(
    n_trades=("net_pnl_pct", "count"),
    n_winners=("net_pnl_pct", lambda s: int((s > 0).sum())),
    mean_net=("net_pnl_pct", "mean"),
    sum_net_pct=("net_pnl_pct", "sum"),
    port_return_approx=("contrib", "sum"),
    n_time_exits=("exit_reason", lambda s: int((s == "time").sum())),
    n_trailing_exits=("exit_reason", lambda s: int((s == "trailing").sum())),
)
monthly_trade_agg["win_rate"] = monthly_trade_agg["n_winners"] / monthly_trade_agg["n_trades"]
print("\nMonthly trade aggregation (size_frac = 0.8/6 = 0.1333):")
out = monthly_trade_agg.copy()
out["mean_net_pct"] = out["mean_net"] * 100
out["sum_net_pct"] = out["sum_net_pct"] * 100
out["port_ret_approx_pct"] = out["port_return_approx"] * 100
out["win_rate_pct"] = out["win_rate"] * 100
print(out[["n_trades", "n_winners", "win_rate_pct", "mean_net_pct",
          "sum_net_pct", "port_ret_approx_pct",
          "n_time_exits", "n_trailing_exits"]].round(2).to_string())


# Exact portfolio result via simulate_portfolio (read-only). Use the same
# parameters write_active_spec.py uses (starting=$1000, 6 concurrent, 80%
# deploy, 1× leverage).
from crypto.execution.backtest.report import simulate_portfolio

sim = simulate_portfolio(
    conn, run_id=RUN_ID,
    starting_capital=1000.0, max_positions=6,
    deploy_fraction=0.8, leverage=1.0,
)
print(f"\nsimulate_portfolio: "
      f"final_equity=${sim.final_equity:,.2f}  "
      f"total_return={sim.total_return_pct*100:+.2f}%  "
      f"annualized={sim.annualized_return_pct*100:+.2f}%  "
      f"sharpe={sim.sharpe_ratio:.2f}  "
      f"max_dd={sim.max_drawdown_pct*100:+.2f}%  "
      f"n_taken={sim.n_trades_taken}  n_skipped_cap={sim.n_trades_skipped_capacity}")

eq = sim.equity_curve.copy()
eq["date"] = pd.to_datetime(eq["date"])
eq = eq.set_index("date")
month_end_eq = eq["equity"].resample("ME").last()
monthly_port_ret = month_end_eq.pct_change()
# First month — synthesize from starting capital.
first_month = month_end_eq.index[0]
prelude_return = (month_end_eq.iloc[0] / 1000.0) - 1.0
monthly_port_ret.iloc[0] = prelude_return
monthly_port_ret.index = monthly_port_ret.index.to_period("M").astype(str)

print("\nPortfolio month-end equity + monthly returns (simulate_portfolio):")
sim_df = pd.DataFrame({
    "month_end_equity": month_end_eq.values,
    "monthly_return_pct": (monthly_port_ret * 100).values,
}, index=monthly_port_ret.index)
print(sim_df.round(2).to_string())


# Join trades with regimes and report per-regime stats
trades["regime"] = trades["exit_month"].map(regimes_df["regime"])
unknown = trades["regime"].isna().sum()
if unknown:
    print(f"\n[warn] {unknown} trades have no regime classification "
          "(month outside BTC data range) — dropped from regime stats.")

trades_reg = trades.dropna(subset=["regime"])

regime_trade_stats = trades_reg.groupby("regime").agg(
    n_trades=("net_pnl_pct", "count"),
    win_rate=("net_pnl_pct", lambda s: float((s > 0).mean())),
    mean_net=("net_pnl_pct", "mean"),
    median_net=("net_pnl_pct", "median"),
    std_net=("net_pnl_pct", "std"),
    worst_net=("net_pnl_pct", "min"),
    best_net=("net_pnl_pct", "max"),
)
print("\nTrade-level outcome by regime (entry month → regime):")
disp = regime_trade_stats.copy()
for col in ("win_rate", "mean_net", "median_net", "std_net", "worst_net", "best_net"):
    disp[col] = disp[col] * 100
print(disp.round(2).to_string())


# Monthly portfolio return by regime
month_regime = regimes_df["regime"]
sim_df["regime"] = sim_df.index.map(month_regime)
regime_port_stats = sim_df.groupby("regime")["monthly_return_pct"].agg(
    n_months="count",
    mean="mean",
    median="median",
    std="std",
    worst="min",
    best="max",
)
print("\nMonthly portfolio return by regime:")
print(regime_port_stats.round(2).to_string())


# Top-6 LABEL hit rate by regime
top6_with_month = top6.copy()
top6_with_month["month"] = pd.to_datetime(top6_with_month["prediction_date"]).dt.to_period("M").astype(str)
top6_with_month["regime"] = top6_with_month["month"].map(regimes_df["regime"])
regime_label_stats = top6_with_month.dropna(subset=["regime"]).groupby("regime").agg(
    n=("actual_hit", "count"),
    label_hit_rate=("actual_hit", "mean"),
    mean_max_ret=("actual_max_return", "mean"),
    median_max_ret=("actual_max_return", "median"),
)
print("\nTop-6 label hit rate by regime (prediction-month → regime):")
disp_l = regime_label_stats.copy()
disp_l["label_hit_rate"] = disp_l["label_hit_rate"] * 100
disp_l["mean_max_ret"] = disp_l["mean_max_ret"] * 100
disp_l["median_max_ret"] = disp_l["median_max_ret"] * 100
print(disp_l.round(2).to_string())


# ──────────────────────────────────────────────────────────────────────
# PART 3 — Realistic expectations (percentiles)
# ──────────────────────────────────────────────────────────────────────

hr("PART 3 — Realistic expectations")

monthly_port_pct = (monthly_port_ret * 100).dropna()
p_port = percentiles(monthly_port_pct, ps=(5, 25, 50, 75, 95))
print(f"Monthly portfolio return percentiles (n={len(monthly_port_pct)}):")
for k, v in p_port.items():
    print(f"  {k:<5}: {v:+.2f}%")

# Worst rolling drawdown sequence (consecutive negative months)
neg_months = (monthly_port_pct < 0).astype(int)
# Rolling worst-N-month sum
roll_sums = {N: monthly_port_pct.rolling(N).sum().min() for N in (1, 2, 3, 6)}
print("\nRolling worst-N-month cumulative return (paper-trading DD proxy):")
for N, v in roll_sums.items():
    print(f"  worst {N}-month  : {v:+.2f}%")

# Label hit rate distribution across months (top-6)
label_month = top6_with_month.groupby("month")["actual_hit"].mean() * 100
print(f"\nMonthly label hit rate percentiles (n={len(label_month)}):")
for p in (5, 25, 50, 75, 95):
    print(f"  p{p}: {np.percentile(label_month, p):.2f}%")

# Trade win rate (P&L positivity) by month
mwr = monthly_trade_agg["win_rate"] * 100
print(f"\nMonthly trade win rate percentiles (n={len(mwr)}):")
for p in (5, 25, 50, 75, 95):
    print(f"  p{p}: {np.percentile(mwr, p):.2f}%")

print(f"\nHeadline numbers:")
print(f"  median monthly portfolio return        : {p_port['p50']:+.2f}%")
print(f"  90th-percentile spread (p5..p95)       : "
      f"[{p_port['p5']:+.2f}%, {p_port['p95']:+.2f}%]")
print(f"  median monthly LABEL hit rate (top-6)  : {np.median(label_month):.2f}%")
print(f"  median monthly trade win rate          : {np.median(mwr):.2f}%")
print(f"  simulate_portfolio max drawdown        : {sim.max_drawdown_pct*100:+.2f}%")
print(f"  worst 2-month cumulative return        : {roll_sums[2]:+.2f}%")

# ──────────────────────────────────────────────────────────────────────
# Dump intermediate DataFrames for the markdown writer to re-load.
# ──────────────────────────────────────────────────────────────────────
DUMP = Path("/home/jpcg/MHDE/.claude/local_scripts/_strategy_dump")
DUMP.mkdir(exist_ok=True)
monthly_compare.to_csv(DUMP / "monthly_compare.csv")
monthly_trade_agg.to_csv(DUMP / "monthly_trades.csv")
sim_df.to_csv(DUMP / "monthly_portfolio.csv")
regimes_df.to_csv(DUMP / "regimes.csv")
regime_trade_stats.to_csv(DUMP / "regime_trades.csv")
regime_port_stats.to_csv(DUMP / "regime_portfolio.csv")
regime_label_stats.to_csv(DUMP / "regime_labels.csv")
(DUMP / "summary.json").write_text(json.dumps({
    "date_min": str(date_min), "date_max": str(date_max),
    "n_days": int(n_days),
    "n_walkfold_preds": int(len(preds)),
    "preds_per_day_mean": float(preds_per_day.mean()),
    "preds_per_day_min": int(preds_per_day.min()),
    "preds_per_day_max": int(preds_per_day.max()),
    "buckets": {k: {kk: vv for kk, vv in v.items() if kk != "daily_obj"} for k, v in stats.items()},
    "regime_counts": {k: int(v) for k, v in regime_counts.items()},
    "n_months_top_beats_rand": n_months_top_beats_rand,
    "n_months_total": n_months_total,
    "portfolio": {
        "final_equity": float(sim.final_equity),
        "total_return_pct": float(sim.total_return_pct),
        "annualized_return_pct": float(sim.annualized_return_pct),
        "sharpe_ratio": float(sim.sharpe_ratio),
        "max_drawdown_pct": float(sim.max_drawdown_pct),
        "n_trades_taken": int(sim.n_trades_taken),
        "n_trades_skipped_capacity": int(sim.n_trades_skipped_capacity),
        "span_days": int(sim.span_days),
    },
    "monthly_port_percentiles": p_port,
    "rolling_dd": {f"{k}m": float(v) for k, v in roll_sums.items()},
    "label_hit_rate_percentiles": {
        f"p{p}": float(np.percentile(label_month, p))
        for p in (5, 25, 50, 75, 95)
    },
    "trade_win_rate_percentiles": {
        f"p{p}": float(np.percentile(mwr, p))
        for p in (5, 25, 50, 75, 95)
    },
}, indent=2, default=str))
print(f"\nDumped intermediate frames to {DUMP}")

conn.close()
