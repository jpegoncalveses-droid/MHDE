"""Phase 1B decay & stress analysis.

Six analyses, each producing a section verdict, then a final synthesis.

Sections
--------
A. Per-month model accuracy on walk-forward 10d predictions
   (top-decile precision, Brier, AUC, base rate).
B. Per-month portfolio Sharpe of the locked Phase 1B winner.
C. Spearman correlation between A's top-decile precision and B's Sharpe.
   Strong negative or zero correlation while Sharpe stays high =
   "Sharpe is mechanical, not edge-driven".
D. Execution-friction stress test: re-simulate the locked winner trades
   with elevated round-trip cost + slippage.
E. Universe survivorship check: for each (date, predicted-coin) pair,
   verify the coin's 30-day USD-volume rank ≤ 50 at THAT time.
F. Cross-fold walk-forward stability — per-fold model accuracy and
   per-fold portfolio Sharpe. Indirect proxy for "frozen vs walk-forward"
   because the persisted prediction store does not contain old-model
   predictions on new dates; this is documented in-section.

This script does NOT modify spec_config or active_spec, and treats
crypto_ml_predictions / crypto_prices_daily / crypto_funding_rates /
crypto_ml_features as read-only. Section D works entirely off the
already-persisted trades for the locked Phase 1B run_id. No new rows
are written to crypto_backtest_*.

Outputs
-------
- data/exports/phase1b_decay_analysis.json
- data/exports/phase1b_decay_analysis.md
"""
from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

from crypto.config import (  # noqa: E402
    STABLECOIN_EXCLUDE,
    UNIVERSE_SIZE,
    WRAPPED_EXCLUDE,
)
from storage.db import get_connection  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Pre-registered constants
# ──────────────────────────────────────────────────────────────────────

FULL_START = date(2025, 4, 5)
FULL_END = date(2026, 5, 9)
LOCKED_RUN_ID = "backtest_10d_D_top_n_a02e15a0"
HORIZON = "10d"
WALKFOLD_PATTERN = "crypto_10d_walkfold_%"

TOP_DECILE_PROB = 0.9

# Section D friction scenarios — additional round-trip cost in fraction terms.
#
# baseline RTC ≈ 0.07 % (entry maker 0.02 % + exit taker 0.05 %) + tiered
# slippage. The deltas below are the EXTRA round-trip cost applied to each
# trade's stored net_pnl_pct.
FRICTION_SCENARIOS = [
    {
        "name": "baseline",
        "description": "0.07 % RTC + tiered slippage (as backtested)",
        "extra_cost_per_trade": 0.0,
    },
    {
        "name": "realistic_small_cap",
        "description": "0.30 % RTC + 0.10 % extra slippage per side "
                       "(= +0.43 % per trade vs baseline)",
        "extra_cost_per_trade": (0.30 - 0.07) / 100.0 + 0.10 * 2 / 100.0,
    },
    {
        "name": "conservative_small_cap",
        "description": "0.50 % RTC + 0.15 % extra slippage per side "
                       "(= +0.73 % per trade vs baseline)",
        "extra_cost_per_trade": (0.50 - 0.07) / 100.0 + 0.15 * 2 / 100.0,
    },
    {
        "name": "worst_case_volatile",
        "description": "1.00 % RTC, baseline slippage "
                       "(= +0.93 % per trade vs baseline)",
        "extra_cost_per_trade": (1.00 - 0.07) / 100.0,
    },
]

# Section E thresholds for "is universe contaminated"
SURVIVORSHIP_FLAG_RATIO = 0.10   # > 10 % of predictions out-of-universe = flag
VOLUME_LOOKBACK_DAYS = 30

# Portfolio sim — match active_spec.json
PORTFOLIO_SIM_KWARGS = dict(
    starting_capital=1000.0,
    max_positions=6,
    deploy_fraction=0.8,
    leverage=1.0,
)
SHARPE_PERIODS_PER_YEAR = 252
ANN_DAYS_PER_YEAR = 365.0

# Section A / B trend thresholds for verdicts
TREND_SLOPE_DECAY_THRESHOLD = 0.0  # negative slope on month vs metric = decay
TREND_MAGNITUDE_THRESHOLD = 0.10   # |slope * span| > 0.10 = material

OUT_JSON = PROJECT_ROOT / "data" / "exports" / "phase1b_decay_analysis.json"
OUT_MD = PROJECT_ROOT / "data" / "exports" / "phase1b_decay_analysis.md"

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("validate_phase1b_decay")
log.setLevel(logging.INFO)


# ──────────────────────────────────────────────────────────────────────
# Generic helpers
# ──────────────────────────────────────────────────────────────────────


def _fmt(v: Any, spec: str = ".3f") -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if math.isnan(v):
            return "n/a"
        if math.isinf(v):
            return "inf"
        return format(v, spec)
    return str(v)


def _coerce_date(x: Any) -> Optional[date]:
    if x is None:
        return None
    if hasattr(x, "to_pydatetime"):
        return x.to_pydatetime().date()
    if hasattr(x, "date") and not isinstance(x, date):
        return x.date()
    return x


def _print_section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _safe_brier(p: np.ndarray, y: np.ndarray) -> Optional[float]:
    if len(p) == 0:
        return None
    return float(np.mean((p - y) ** 2))


def _safe_auc(p: np.ndarray, y: np.ndarray) -> Optional[float]:
    if len(p) == 0 or len(np.unique(y)) < 2:
        return None
    try:
        return float(roc_auc_score(y, p))
    except Exception:
        return None


def _linear_trend(months: list[str], values: list[Optional[float]]) -> dict[str, Any]:
    """Fit a linear trend of values vs month index, ignoring NaN months."""
    pairs = [
        (i, v) for i, v in enumerate(values)
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ]
    if len(pairs) < 3:
        return {"slope": None, "intercept": None, "n_points": len(pairs)}
    xs = np.array([p[0] for p in pairs], dtype=float)
    ys = np.array([p[1] for p in pairs], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return {
        "slope": float(slope),
        "intercept": float(intercept),
        "n_points": len(pairs),
        "span_months": float(xs.max() - xs.min()),
        "total_change_over_span": float(slope * (xs.max() - xs.min())),
    }


# ──────────────────────────────────────────────────────────────────────
# Portfolio sim (in-memory, mirrors simulate_portfolio for filtered trades)
# ──────────────────────────────────────────────────────────────────────


def _simulate_on_trades(
    trades: pd.DataFrame,
    *,
    starting_capital: float = 1000.0,
    max_positions: int = 6,
    deploy_fraction: float = 0.8,
    leverage: float = 1.0,
) -> dict[str, Any]:
    empty = {
        "final_equity": starting_capital,
        "total_return_pct": 0.0,
        "annualized_return_pct": 0.0,
        "sharpe_ratio": None,
        "max_drawdown_pct": 0.0,
        "profit_factor": None,
        "n_trades_taken": 0,
        "n_trades_skipped_capacity": 0,
        "span_days": 0,
        "equity_series": pd.Series(dtype=float),
    }
    if trades.empty:
        return empty

    t = trades.copy()
    t["entry_date"] = t["entry_date"].apply(_coerce_date)
    t["exit_date"] = t["exit_date"].apply(_coerce_date)
    t = t.sort_values(["entry_date", "trade_id"]).reset_index(drop=True)

    equity = float(starting_capital)
    open_positions: list[dict[str, Any]] = []
    closes: list[dict[str, Any]] = []
    n_taken = 0
    n_skipped = 0

    for row in t.itertuples(index=False):
        still_open: list[dict[str, Any]] = []
        for pos in sorted(
            open_positions, key=lambda p: (p["exit_date"], p["trade_id"])
        ):
            if pos["exit_date"] < row.entry_date:
                pnl = pos["size"] * pos["net_pnl_pct"]
                equity += pnl
                closes.append({
                    "date": pos["exit_date"], "pnl_dollars": pnl,
                    "trade_id": pos["trade_id"],
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        if len(open_positions) >= max_positions:
            n_skipped += 1
            continue

        size = (equity * deploy_fraction * leverage) / max_positions
        open_positions.append({
            "trade_id": row.trade_id, "coin": row.coin,
            "entry_date": row.entry_date, "exit_date": row.exit_date,
            "size": size, "net_pnl_pct": float(row.net_pnl_pct),
        })
        n_taken += 1

    for pos in sorted(
        open_positions, key=lambda p: (p["exit_date"], p["trade_id"])
    ):
        pnl = pos["size"] * pos["net_pnl_pct"]
        equity += pnl
        closes.append({
            "date": pos["exit_date"], "pnl_dollars": pnl,
            "trade_id": pos["trade_id"],
        })

    if not closes:
        return empty

    closes_df = pd.DataFrame(closes)
    daily_pnl = closes_df.groupby("date", sort=True)["pnl_dollars"].sum()
    cumulative = starting_capital + daily_pnl.cumsum()
    first_date = daily_pnl.index.min()
    prelude = pd.Series(
        {first_date - pd.Timedelta(days=1): starting_capital}
    ).rename_axis("date")
    equity_series = pd.concat([prelude, cumulative]).sort_index()

    final = float(equity_series.iloc[-1])
    first_entry = t["entry_date"].min()
    last_exit = t["exit_date"].max()
    span_days = (
        max(0, (last_exit - first_entry).days) if first_entry and last_exit else 0
    )

    total_ret = (final - starting_capital) / starting_capital
    annualized = (
        total_ret * (ANN_DAYS_PER_YEAR / float(span_days))
        if span_days > 0 else 0.0
    )

    rets = pd.Series(equity_series.values).pct_change().dropna()
    if len(rets) >= 2 and rets.std(ddof=1) > 0:
        sharpe = float(
            rets.mean() / rets.std(ddof=1) * (SHARPE_PERIODS_PER_YEAR ** 0.5)
        )
    else:
        sharpe = None

    eq = equity_series.astype(float)
    peak = eq.cummax()
    dd = float(((eq - peak) / peak).min()) if len(eq) else 0.0

    winners = float(closes_df.loc[closes_df["pnl_dollars"] > 0, "pnl_dollars"].sum())
    losers = float(closes_df.loc[closes_df["pnl_dollars"] <= 0, "pnl_dollars"].sum())
    if winners == 0 and losers == 0:
        pf: Optional[float] = None
    elif losers == 0:
        pf = math.inf if winners > 0 else None
    else:
        pf = winners / abs(losers)

    return {
        "final_equity": final,
        "total_return_pct": float(total_ret),
        "annualized_return_pct": float(annualized),
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": dd,
        "profit_factor": pf,
        "n_trades_taken": int(n_taken),
        "n_trades_skipped_capacity": int(n_skipped),
        "span_days": int(span_days),
        "equity_series": equity_series,
    }


def _load_locked_trades(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT trade_id, coin, entry_date, exit_date,
               gross_pnl_pct, fee_pct, slippage_pct, funding_pct,
               net_pnl_pct, probability_at_entry, exit_reason
        FROM crypto_backtest_trades
        WHERE run_id = ?
          AND entry_date IS NOT NULL
          AND exit_date IS NOT NULL
          AND net_pnl_pct IS NOT NULL
        ORDER BY entry_date, trade_id
        """,
        [LOCKED_RUN_ID],
    ).fetchdf()


# ──────────────────────────────────────────────────────────────────────
# Section A — per-month model accuracy
# ──────────────────────────────────────────────────────────────────────


def section_a(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    _print_section("Section A — per-month model accuracy (walkfold 10d)")
    preds = conn.execute(
        f"""
        SELECT prediction_date, predicted_probability, actual_hit, model_id
        FROM crypto_ml_predictions
        WHERE model_id LIKE '{WALKFOLD_PATTERN}'
          AND horizon = '{HORIZON}'
          AND actual_hit IS NOT NULL
          AND prediction_date BETWEEN ? AND ?
        ORDER BY prediction_date
        """,
        [FULL_START, FULL_END],
    ).fetchdf()
    print(f"  {len(preds)} walk-fold 10d predictions with outcomes "
          f"({preds['prediction_date'].min()} → {preds['prediction_date'].max()})")

    preds["prediction_date"] = pd.to_datetime(preds["prediction_date"])
    preds["month"] = preds["prediction_date"].dt.to_period("M").astype(str)

    rows: list[dict[str, Any]] = []
    for month, grp in preds.groupby("month", sort=True):
        p = grp["predicted_probability"].to_numpy(dtype=float)
        y = grp["actual_hit"].astype(int).to_numpy()
        top_mask = p >= TOP_DECILE_PROB
        top_p = p[top_mask]
        top_y = y[top_mask]
        rows.append({
            "month": month,
            "n_predictions": int(len(grp)),
            "base_rate": float(y.mean()) if len(y) else None,
            "n_top_decile": int(top_mask.sum()),
            "top_decile_precision": (
                float(top_y.mean()) if len(top_y) else None
            ),
            "brier_score": _safe_brier(p, y),
            "auc": _safe_auc(p, y),
        })

    months = [r["month"] for r in rows]
    precision_trend = _linear_trend(
        months, [r["top_decile_precision"] for r in rows]
    )
    auc_trend = _linear_trend(months, [r["auc"] for r in rows])
    brier_trend = _linear_trend(months, [r["brier_score"] for r in rows])

    print()
    print(f"  {'month':<8}  {'n_pred':>6}  {'base_rt':>7}  "
          f"{'n_top':>5}  {'top_prec':>8}  {'brier':>7}  {'AUC':>5}")
    print("  " + "-" * 60)
    for r in rows:
        print(f"  {r['month']:<8}  {r['n_predictions']:>6}  "
              f"{_fmt(r['base_rate'], '.3f'):>7}  "
              f"{r['n_top_decile']:>5}  "
              f"{_fmt(r['top_decile_precision'], '.3f'):>8}  "
              f"{_fmt(r['brier_score'], '.3f'):>7}  "
              f"{_fmt(r['auc'], '.3f'):>5}")

    decaying = (
        precision_trend["slope"] is not None
        and precision_trend["slope"] < TREND_SLOPE_DECAY_THRESHOLD
        and abs(precision_trend.get("total_change_over_span") or 0.0)
            > TREND_MAGNITUDE_THRESHOLD
    )
    auc_decaying = (
        auc_trend["slope"] is not None
        and auc_trend["slope"] < TREND_SLOPE_DECAY_THRESHOLD
        and abs(auc_trend.get("total_change_over_span") or 0.0)
            > 0.05
    )
    if decaying or auc_decaying:
        verdict = (
            f"YES — model accuracy decaying. Top-decile precision slope "
            f"{_fmt(precision_trend['slope'])}/mo, AUC slope "
            f"{_fmt(auc_trend['slope'])}/mo."
        )
    elif precision_trend["slope"] is not None:
        verdict = (
            f"NO material decay observed. Top-decile precision slope "
            f"{_fmt(precision_trend['slope'])}/mo over "
            f"{precision_trend['n_points']} months "
            f"(magnitude {_fmt(precision_trend['total_change_over_span'])} "
            f"across the span)."
        )
    else:
        verdict = "UNCLEAR — insufficient months for trend fit."
    print()
    print(f"  Verdict: {verdict}")

    return {
        "monthly": rows,
        "precision_trend": precision_trend,
        "auc_trend": auc_trend,
        "brier_trend": brier_trend,
        "verdict": verdict,
    }


# ──────────────────────────────────────────────────────────────────────
# Section B — per-month portfolio Sharpe of the locked Phase 1B winner
# ──────────────────────────────────────────────────────────────────────


def _per_month_portfolio_metrics(
    trades: pd.DataFrame,
    equity_series: pd.Series,
) -> list[dict[str, Any]]:
    """Per exit-month metrics on the locked-winner trades.

    portfolio_sharpe_in_month: annualised Sharpe of the daily portfolio
    return series within the month (drawn from the full-window equity curve).
    """
    t = trades.copy()
    t["exit_date"] = t["exit_date"].apply(_coerce_date)
    t["month"] = pd.to_datetime(t["exit_date"]).dt.to_period("M").astype(str)

    es = equity_series.copy()
    es.index = pd.to_datetime(es.index)
    daily_ret = es.pct_change().dropna()
    daily_ret.index = pd.to_datetime(daily_ret.index)

    rows: list[dict[str, Any]] = []
    for month, grp in t.groupby("month", sort=True):
        period = pd.Period(month, freq="M")
        ms, me = period.start_time, period.end_time
        eq_slice = es.loc[(es.index >= ms) & (es.index <= me)]
        rets_slice = daily_ret.loc[(daily_ret.index >= ms) & (daily_ret.index <= me)]
        if len(rets_slice) >= 2 and rets_slice.std(ddof=1) > 0:
            sh = float(
                rets_slice.mean() / rets_slice.std(ddof=1)
                * (SHARPE_PERIODS_PER_YEAR ** 0.5)
            )
        else:
            sh = None
        if len(eq_slice) >= 2:
            peak = eq_slice.cummax()
            dd = float(((eq_slice - peak) / peak).min())
        else:
            dd = 0.0
        pnl_series = grp["net_pnl_pct"].astype(float)
        rows.append({
            "month": month,
            "n_trades": int(len(grp)),
            "mean_net_pnl_pct": float(pnl_series.mean()) if len(pnl_series) else None,
            "trade_hit_rate": (
                float((pnl_series > 0).mean()) if len(pnl_series) else None
            ),
            "portfolio_sharpe_in_month": sh,
            "max_dd_in_month": dd,
        })
    return rows


def section_b(
    conn: duckdb.DuckDBPyConnection, locked_trades: pd.DataFrame
) -> dict[str, Any]:
    _print_section("Section B — per-month portfolio Sharpe (locked Phase 1B winner)")
    sim = _simulate_on_trades(locked_trades, **PORTFOLIO_SIM_KWARGS)
    print(f"  full-window Sharpe={_fmt(sim['sharpe_ratio'])}  "
          f"final_equity=${_fmt(sim['final_equity'], '.0f')}  "
          f"max DD={_fmt(sim['max_drawdown_pct'], '.2%')}  "
          f"n_trades_taken={sim['n_trades_taken']}")
    monthly = _per_month_portfolio_metrics(locked_trades, sim["equity_series"])

    print()
    print(f"  {'month':<8}  {'n':>4}  {'mean P&L':>9}  {'hit':>6}  "
          f"{'Sharpe':>7}  {'maxDD':>7}")
    print("  " + "-" * 60)
    for r in monthly:
        print(f"  {r['month']:<8}  {r['n_trades']:>4}  "
              f"{_fmt(r['mean_net_pnl_pct'], '.2%'):>9}  "
              f"{_fmt(r['trade_hit_rate'], '.1%'):>6}  "
              f"{_fmt(r['portfolio_sharpe_in_month']):>7}  "
              f"{_fmt(r['max_dd_in_month'], '.2%'):>7}")

    months = [r["month"] for r in monthly]
    sh_trend = _linear_trend(
        months, [r["portfolio_sharpe_in_month"] for r in monthly]
    )
    if (
        sh_trend["slope"] is not None
        and sh_trend["slope"] < TREND_SLOPE_DECAY_THRESHOLD
        and abs(sh_trend.get("total_change_over_span") or 0.0) > 1.0
    ):
        verdict = (
            f"YES — per-month portfolio Sharpe decaying. Slope "
            f"{_fmt(sh_trend['slope'])} Sharpe/mo over "
            f"{sh_trend['n_points']} months."
        )
    elif sh_trend["slope"] is not None:
        verdict = (
            f"NO material per-month Sharpe decay. Slope "
            f"{_fmt(sh_trend['slope'])} Sharpe/mo across "
            f"{sh_trend['n_points']} months."
        )
    else:
        verdict = "UNCLEAR — insufficient months for trend fit."
    print()
    print(f"  Verdict: {verdict}")
    return {
        "full_window": {
            k: v for k, v in sim.items()
            if k not in ("equity_series",)
        },
        "monthly": monthly,
        "sharpe_trend": sh_trend,
        "verdict": verdict,
    }


# ──────────────────────────────────────────────────────────────────────
# Section C — correlation precision (A) vs portfolio Sharpe (B)
# ──────────────────────────────────────────────────────────────────────


def section_c(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    _print_section("Section C — model accuracy vs portfolio Sharpe per month")
    a_by_month = {r["month"]: r for r in a["monthly"]}
    b_by_month = {r["month"]: r for r in b["monthly"]}
    common = sorted(set(a_by_month) & set(b_by_month))

    paired: list[tuple[float, float, str]] = []
    for m in common:
        p = a_by_month[m]["top_decile_precision"]
        s = b_by_month[m]["portfolio_sharpe_in_month"]
        if p is None or s is None:
            continue
        if isinstance(p, float) and math.isnan(p):
            continue
        if isinstance(s, float) and math.isnan(s):
            continue
        paired.append((float(p), float(s), m))

    if len(paired) < 4:
        verdict = "UNCLEAR — insufficient paired months for correlation."
        result = {
            "n_paired": len(paired),
            "spearman_correlation": None,
            "spearman_pvalue": None,
            "interpretation": "insufficient_data",
            "verdict": verdict,
            "paired_points": [
                {"month": m, "top_decile_precision": p, "portfolio_sharpe": s}
                for (p, s, m) in paired
            ],
        }
        print(f"  {verdict}")
        return result

    xs = np.array([p[0] for p in paired])
    ys = np.array([p[1] for p in paired])
    res = spearmanr(xs, ys)
    rho = float(res.correlation)
    pval = float(res.pvalue)

    print(f"  paired months: {len(paired)}")
    print(f"  Spearman ρ = {rho:.3f}   p = {pval:.4f}")
    print()
    print(f"  {'month':<8}  {'top_prec':>8}  {'port Sharpe':>11}")
    print("  " + "-" * 32)
    for p, s, m in paired:
        print(f"  {m:<8}  {p:>8.3f}  {s:>11.3f}")

    # Verdict interpretation
    # rho ~ +0.5 or higher = real edge link (when model is right, portfolio Sharpe is up)
    # rho near 0 with high Sharpe = mechanical
    # rho negative = perverse (model wrong but Sharpe up -> suspicious)
    mean_sharpe = float(np.mean(ys))
    if rho >= 0.4:
        interp = "edge_linked"
        verdict = (
            f"REAL EDGE LINK — ρ={rho:.2f}: months with better model "
            f"precision also see higher portfolio Sharpe."
        )
    elif rho >= 0.0:
        if mean_sharpe > 1.5:
            interp = "mechanical_or_weak_link"
            verdict = (
                f"WEAK / MECHANICAL — ρ={rho:.2f} with mean monthly "
                f"Sharpe {mean_sharpe:.2f}. Portfolio performance is not "
                f"clearly tracking model precision; the trailing-stop "
                f"mechanic may be doing most of the work."
            )
        else:
            interp = "weak_link"
            verdict = (
                f"WEAK LINK — ρ={rho:.2f}; low overall Sharpe ({mean_sharpe:.2f}). "
                f"Neither model nor mechanics are reliably profitable."
            )
    else:
        interp = "perverse"
        verdict = (
            f"PERVERSE — ρ={rho:.2f}: months with WORSE model accuracy "
            f"have HIGHER portfolio Sharpe. Suspicious; investigate before "
            f"trusting the headline numbers."
        )

    print()
    print(f"  Verdict: {verdict}")
    return {
        "n_paired": len(paired),
        "spearman_correlation": rho,
        "spearman_pvalue": pval,
        "mean_monthly_portfolio_sharpe": mean_sharpe,
        "interpretation": interp,
        "verdict": verdict,
        "paired_points": [
            {"month": m, "top_decile_precision": p, "portfolio_sharpe": s}
            for (p, s, m) in paired
        ],
    }


# ──────────────────────────────────────────────────────────────────────
# Section D — friction stress test
# ──────────────────────────────────────────────────────────────────────


def section_d(locked_trades: pd.DataFrame) -> dict[str, Any]:
    _print_section("Section D — execution-friction stress test")
    print(f"  {'scenario':<24}  {'+cost/trade':>11}  "
          f"{'Sharpe':>7}  {'AnnRet':>8}  {'maxDD':>7}  "
          f"{'final $':>10}  {'PF':>5}")
    print("  " + "-" * 76)
    scenario_results = []
    for sc in FRICTION_SCENARIOS:
        modified = locked_trades.copy()
        modified["net_pnl_pct"] = (
            modified["net_pnl_pct"].astype(float) - sc["extra_cost_per_trade"]
        )
        sim = _simulate_on_trades(modified, **PORTFOLIO_SIM_KWARGS)
        row = {
            "name": sc["name"],
            "description": sc["description"],
            "extra_cost_per_trade": sc["extra_cost_per_trade"],
            "portfolio_sharpe": sim["sharpe_ratio"],
            "portfolio_max_dd_pct": sim["max_drawdown_pct"],
            "portfolio_annualized_return_pct": sim["annualized_return_pct"],
            "portfolio_profit_factor": sim["profit_factor"],
            "final_equity": sim["final_equity"],
            "n_trades_taken": sim["n_trades_taken"],
        }
        scenario_results.append(row)
        print(f"  {sc['name']:<24}  "
              f"{sc['extra_cost_per_trade']:>10.2%}  "
              f"{_fmt(sim['sharpe_ratio']):>7}  "
              f"{_fmt(sim['annualized_return_pct'], '.0%'):>8}  "
              f"{_fmt(sim['max_drawdown_pct'], '.1%'):>7}  "
              f"{_fmt(sim['final_equity'], '.0f'):>10}  "
              f"{_fmt(sim['profit_factor'], '.2f'):>5}")

    # Find the first scenario where portfolio is unprofitable
    first_loss = next(
        (r for r in scenario_results if r["final_equity"] <= 1000.0),
        None,
    )
    if first_loss is None:
        # Still profitable in all scenarios — find when sharpe drops below 1.0
        first_sub_unity = next(
            (r for r in scenario_results
             if r["portfolio_sharpe"] is not None
             and r["portfolio_sharpe"] < 1.0),
            None,
        )
        if first_sub_unity is None:
            verdict = (
                "ROBUST to all tested friction levels — strategy stays "
                "Sharpe>1 and profitable through worst-case (+0.93 % per "
                "trade). Continues to clear the Phase 1B Sharpe gate."
            )
        else:
            verdict = (
                f"PROFITABLE but Sharpe<1 at scenario "
                f"'{first_sub_unity['name']}' (+{first_sub_unity['extra_cost_per_trade']:.2%} "
                f"per trade). Strategy survives small-cap costs but fails "
                f"the Phase 1B Sharpe gate under elevated friction."
            )
    else:
        verdict = (
            f"BREAKS at scenario '{first_loss['name']}' "
            f"(+{first_loss['extra_cost_per_trade']:.2%} per trade) — portfolio "
            f"ends below starting capital. Strategy is fragile to small-cap "
            f"trading costs."
        )
    print()
    print(f"  Verdict: {verdict}")
    return {"scenarios": scenario_results, "verdict": verdict}


# ──────────────────────────────────────────────────────────────────────
# Section E — universe survivorship check
# ──────────────────────────────────────────────────────────────────────


def section_e(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    _print_section("Section E — universe survivorship check")
    print(f"  Walk-fold 10d predictions vs 30-day USD-volume rank ≤ "
          f"{UNIVERSE_SIZE} at prediction date (excludes stables/wrapped)")
    excluded = list(STABLECOIN_EXCLUDE | WRAPPED_EXCLUDE)
    excl_sql = ",".join(["?"] * len(excluded))

    # Unique prediction dates within the executable window
    pred_dates = conn.execute(
        f"""
        SELECT DISTINCT prediction_date FROM crypto_ml_predictions
        WHERE model_id LIKE '{WALKFOLD_PATTERN}'
          AND horizon = '{HORIZON}'
          AND prediction_date BETWEEN ? AND ?
        ORDER BY prediction_date
        """,
        [FULL_START, FULL_END],
    ).fetchdf()
    pred_dates["prediction_date"] = pd.to_datetime(pred_dates["prediction_date"]).dt.date
    dates = pred_dates["prediction_date"].tolist()
    print(f"  unique prediction dates: {len(dates)}")

    n_total = 0
    n_out = 0
    out_examples: list[dict[str, Any]] = []
    monthly_counts: dict[str, dict[str, int]] = {}

    for d in dates:
        # Compute as-of universe: top UNIVERSE_SIZE by 30d USD volume.
        ranks_rows = conn.execute(
            f"""
            WITH vol30 AS (
                SELECT symbol, AVG(close * volume) AS adv
                FROM crypto_prices_daily
                WHERE trade_date >  ?::DATE - INTERVAL '{VOLUME_LOOKBACK_DAYS} days'
                  AND trade_date <= ?
                  AND symbol NOT IN ({excl_sql})
                GROUP BY symbol
            )
            SELECT symbol, RANK() OVER (ORDER BY adv DESC) AS rk
            FROM vol30
            WHERE adv IS NOT NULL
            ORDER BY rk
            """,
            [d, d, *excluded],
        ).fetchall()
        in_universe = {sym for sym, rk in ranks_rows if rk <= UNIVERSE_SIZE}

        preds_today = conn.execute(
            f"""
            SELECT symbol FROM crypto_ml_predictions
            WHERE model_id LIKE '{WALKFOLD_PATTERN}'
              AND horizon = '{HORIZON}'
              AND prediction_date = ?
            """,
            [d],
        ).fetchall()
        ym = d.strftime("%Y-%m")
        bucket = monthly_counts.setdefault(
            ym, {"n_total": 0, "n_out": 0}
        )
        for (sym,) in preds_today:
            n_total += 1
            bucket["n_total"] += 1
            if sym not in in_universe:
                n_out += 1
                bucket["n_out"] += 1
                if len(out_examples) < 20:
                    out_examples.append({
                        "date": d.isoformat(),
                        "symbol": sym,
                    })

    out_ratio = n_out / n_total if n_total else None
    print(f"  total walkfold predictions: {n_total}")
    print(f"  out-of-universe-at-time   : {n_out}  ({_fmt(out_ratio, '.2%')})")

    print()
    print(f"  monthly out-of-universe rate:")
    print(f"  {'month':<8}  {'n_total':>7}  {'n_out':>5}  {'rate':>6}")
    print("  " + "-" * 32)
    monthly_rows = []
    for m in sorted(monthly_counts):
        c = monthly_counts[m]
        rate = c["n_out"] / c["n_total"] if c["n_total"] else None
        monthly_rows.append({
            "month": m,
            "n_total": c["n_total"],
            "n_out_of_universe": c["n_out"],
            "out_of_universe_rate": rate,
        })
        print(f"  {m:<8}  {c['n_total']:>7}  {c['n_out']:>5}  "
              f"{_fmt(rate, '.2%'):>6}")

    if out_ratio is not None and out_ratio > SURVIVORSHIP_FLAG_RATIO:
        verdict = (
            f"FLAGGED — {_fmt(out_ratio, '.2%')} of walkfold predictions "
            f"are for coins that were NOT in the top-{UNIVERSE_SIZE} by 30-day "
            f"USD volume at the time. Threshold is "
            f"{SURVIVORSHIP_FLAG_RATIO:.0%}. The universe used at training "
            f"time appears to differ from a strict point-in-time top-{UNIVERSE_SIZE}; "
            f"survivorship-bias / look-ahead exposure is non-trivial."
        )
    elif out_ratio is not None:
        verdict = (
            f"PASSES — only {_fmt(out_ratio, '.2%')} of walkfold predictions "
            f"are for coins outside the contemporaneous top-{UNIVERSE_SIZE} "
            f"by 30d USD volume (threshold {SURVIVORSHIP_FLAG_RATIO:.0%}). "
            f"Universe looks point-in-time consistent within tolerance."
        )
    else:
        verdict = "UNCLEAR — no predictions found in window."
    print()
    print(f"  Verdict: {verdict}")
    return {
        "n_predictions_total": n_total,
        "n_out_of_universe": n_out,
        "out_of_universe_rate": out_ratio,
        "flag_threshold": SURVIVORSHIP_FLAG_RATIO,
        "monthly": monthly_rows,
        "examples_out_of_universe": out_examples,
        "verdict": verdict,
    }


# ──────────────────────────────────────────────────────────────────────
# Section F — cross-fold walk-forward stability
# ──────────────────────────────────────────────────────────────────────


def section_f(
    conn: duckdb.DuckDBPyConnection, locked_trades: pd.DataFrame
) -> dict[str, Any]:
    _print_section("Section F — cross-fold walk-forward stability")
    print(
        "  NOTE: a strict 'frozen-model vs walk-forward' comparison would\n"
        "  require predictions from a single fold's model applied to LATER\n"
        "  months. The persisted store only contains each fold's model\n"
        "  applied to its own one-month test window, so a direct comparison\n"
        "  is not possible without re-running training. Below: per-fold\n"
        "  model accuracy + per-fold portfolio Sharpe — if every fresh\n"
        "  refit yields stable performance, the walk-forward refit pipeline\n"
        "  is doing its job. Decline across folds = compounding decay\n"
        "  despite refit; flat = stable."
    )
    # Per-fold model accuracy
    folds = conn.execute(
        f"""
        SELECT model_id,
               MIN(prediction_date) AS test_start,
               MAX(prediction_date) AS test_end,
               COUNT(*) AS n_pred
        FROM crypto_ml_predictions
        WHERE model_id LIKE '{WALKFOLD_PATTERN}'
          AND horizon = '{HORIZON}'
          AND actual_hit IS NOT NULL
        GROUP BY model_id
        ORDER BY MIN(prediction_date)
        """,
    ).fetchdf()

    # Restrict to folds whose test month falls in the executable window
    folds["test_start"] = pd.to_datetime(folds["test_start"]).dt.date
    folds["test_end"] = pd.to_datetime(folds["test_end"]).dt.date
    folds = folds.loc[folds["test_start"] >= FULL_START].reset_index(drop=True)

    fold_rows: list[dict[str, Any]] = []
    for r in folds.itertuples(index=False):
        rows = conn.execute(
            """
            SELECT predicted_probability, actual_hit
            FROM crypto_ml_predictions
            WHERE model_id = ?
              AND actual_hit IS NOT NULL
            """,
            [r.model_id],
        ).fetchdf()
        if rows.empty:
            continue
        p = rows["predicted_probability"].to_numpy(dtype=float)
        y = rows["actual_hit"].astype(int).to_numpy()
        top_mask = p >= TOP_DECILE_PROB
        top_y = y[top_mask]

        # Apply locked strategy to the trades from this fold's predictions
        # via the existing locked_trades frame — slice by entry_date within
        # the fold's test_start..test_end window. The locked harness took
        # entries at T+1, so trades whose entry_date falls within the fold
        # window correspond to that fold's predictions.
        fold_trades = locked_trades.loc[
            (pd.to_datetime(locked_trades["entry_date"]).dt.date >= r.test_start)
            & (pd.to_datetime(locked_trades["entry_date"]).dt.date
               <= r.test_end + timedelta(days=2))
        ].reset_index(drop=True)
        sim = _simulate_on_trades(fold_trades, **PORTFOLIO_SIM_KWARGS)
        fold_rows.append({
            "model_id": r.model_id,
            "test_start": r.test_start.isoformat(),
            "test_end": r.test_end.isoformat(),
            "n_predictions": int(r.n_pred),
            "base_rate": float(y.mean()),
            "n_top_decile": int(top_mask.sum()),
            "top_decile_precision": (
                float(top_y.mean()) if len(top_y) else None
            ),
            "brier": _safe_brier(p, y),
            "auc": _safe_auc(p, y),
            "n_locked_trades_in_fold": int(len(fold_trades)),
            "portfolio_sharpe_in_fold": sim["sharpe_ratio"],
            "fold_final_equity": float(sim["final_equity"]),
            "fold_max_dd_pct": float(sim["max_drawdown_pct"]),
        })

    print()
    print(f"  {'fold':<28}  {'n_pred':>6}  {'top_prec':>8}  "
          f"{'AUC':>5}  {'n_tr':>4}  {'fold Sharpe':>11}")
    print("  " + "-" * 75)
    for f in fold_rows:
        print(f"  {f['model_id']:<28}  {f['n_predictions']:>6}  "
              f"{_fmt(f['top_decile_precision'], '.3f'):>8}  "
              f"{_fmt(f['auc'], '.3f'):>5}  "
              f"{f['n_locked_trades_in_fold']:>4}  "
              f"{_fmt(f['portfolio_sharpe_in_fold']):>11}")

    # Trend on per-fold top-decile precision and per-fold Sharpe
    precision_trend = _linear_trend(
        [f["model_id"] for f in fold_rows],
        [f["top_decile_precision"] for f in fold_rows],
    )
    sharpe_trend = _linear_trend(
        [f["model_id"] for f in fold_rows],
        [f["portfolio_sharpe_in_fold"] for f in fold_rows],
    )

    p_slope = precision_trend.get("slope")
    s_slope = sharpe_trend.get("slope")
    decay_signals = []
    if (
        p_slope is not None and p_slope < TREND_SLOPE_DECAY_THRESHOLD
        and abs(precision_trend.get("total_change_over_span") or 0.0)
            > TREND_MAGNITUDE_THRESHOLD
    ):
        decay_signals.append("top-decile precision")
    if (
        s_slope is not None and s_slope < TREND_SLOPE_DECAY_THRESHOLD
        and abs(sharpe_trend.get("total_change_over_span") or 0.0)
            > 1.0
    ):
        decay_signals.append("per-fold portfolio Sharpe")

    if decay_signals:
        verdict = (
            f"DEGRADATION across walk-forward folds: {', '.join(decay_signals)} "
            f"trending down. The refit pipeline is NOT fully compensating "
            f"for whatever is changing in the underlying data."
        )
    elif p_slope is not None or s_slope is not None:
        verdict = (
            f"STABLE across walk-forward folds. Per-fold precision slope "
            f"{_fmt(p_slope)}, per-fold Sharpe slope {_fmt(s_slope)}. The "
            f"refit pipeline appears to keep performance roughly constant."
        )
    else:
        verdict = "UNCLEAR — insufficient folds for trend fit."
    print()
    print(f"  Verdict: {verdict}")
    return {
        "fold_rows": fold_rows,
        "precision_trend": precision_trend,
        "sharpe_trend": sharpe_trend,
        "verdict": verdict,
        "note": (
            "True frozen-model-vs-walk-forward comparison was not feasible "
            "from stored predictions. The cross-fold trend reported here is "
            "the practical proxy."
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# Synthesis
# ──────────────────────────────────────────────────────────────────────


def synthesize(results: dict[str, Any]) -> dict[str, Any]:
    """Combine the section verdicts into a deployment-confidence call."""
    a_decay = results["section_a"]["verdict"].startswith("YES")
    b_decay = results["section_b"]["verdict"].startswith("YES")
    c_interp = results["section_c"].get("interpretation", "unclear")
    d_verdict = results["section_d"]["verdict"]
    d_breaks = d_verdict.startswith("BREAKS")
    d_fails_sharpe = d_verdict.startswith("PROFITABLE")
    e_flagged = results["section_e"]["verdict"].startswith("FLAGGED")
    f_decay = results["section_f"]["verdict"].startswith("DEGRADATION")

    accuracy_decay = a_decay or f_decay
    sharpe_decay = b_decay
    mechanical_link = c_interp in ("mechanical_or_weak_link", "weak_link", "perverse")
    friction_breaks = d_breaks
    friction_marginal = d_fails_sharpe
    survivorship = e_flagged

    if survivorship or friction_breaks:
        confidence = "DO_NOT_DEPLOY"
        why = []
        if survivorship:
            why.append("universe survivorship flagged")
        if friction_breaks:
            why.append("strategy unprofitable under realistic small-cap friction")
        recommendation = (
            f"Do NOT deploy live. Hard-blocking signals: {', '.join(why)}. "
            f"Fix the universe construction and/or rework cost assumptions "
            f"before any further validation."
        )
    elif accuracy_decay or sharpe_decay or mechanical_link or friction_marginal:
        confidence = "DEPLOY_WITH_GUARDRAILS"
        flags = []
        if accuracy_decay:
            flags.append("model accuracy decaying")
        if sharpe_decay:
            flags.append("per-month Sharpe decaying")
        if mechanical_link:
            flags.append(
                "Sharpe weakly linked to model precision (mechanical risk)"
            )
        if friction_marginal:
            flags.append(
                "strategy fails Phase 1B Sharpe gate under realistic friction"
            )
        recommendation = (
            f"Deploy with guardrails: strict decay monitoring (weekly model "
            f"AUC + monthly portfolio Sharpe checks against this report's "
            f"baseline), small initial sizing (≤ $1k notional), and a "
            f"hard kill-switch on the first month with Sharpe < 1.0 OR "
            f"top-decile precision < 35 %. Flags: {', '.join(flags)}."
        )
    else:
        confidence = "PROCEED_TO_PAPER_TRADING"
        recommendation = (
            "All six decay/stress tests passed. Validation strengthens. "
            "Proceed to Phase 2 paper trading per docs/PATH_TO_LIVE_PLAN.md."
        )

    return {
        "confidence_label": confidence,
        "signals": {
            "accuracy_decay_A_or_F": accuracy_decay,
            "sharpe_decay_B": sharpe_decay,
            "mechanical_or_weak_link_C": mechanical_link,
            "friction_breaks_D": friction_breaks,
            "friction_marginal_D": friction_marginal,
            "survivorship_flagged_E": survivorship,
        },
        "recommendation": recommendation,
    }


# ──────────────────────────────────────────────────────────────────────
# Output writers
# ──────────────────────────────────────────────────────────────────────


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    raise TypeError(f"not JSON-serializable: {type(obj)}")


def _strip_series(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_series(v) for k, v in obj.items()
            if not isinstance(v, (pd.Series, pd.DataFrame))
        }
    if isinstance(obj, list):
        return [_strip_series(x) for x in obj]
    return obj


def _write_markdown(out_path: Path, results: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# Phase 1B Decay & Stress Analysis")
    lines.append("")
    lines.append(
        f"Locked Phase 1B run_id: `{LOCKED_RUN_ID}` — full window "
        f"{FULL_START} → {FULL_END}."
    )
    lines.append("")
    syn = results["synthesis"]
    lines.append("## Synthesis")
    lines.append("")
    lines.append(f"**Confidence:** `{syn['confidence_label']}`")
    lines.append("")
    lines.append(f"{syn['recommendation']}")
    lines.append("")
    lines.append("| signal | tripped? |")
    lines.append("|---|:---:|")
    for sig, tripped in syn["signals"].items():
        lines.append(f"| {sig} | {'YES' if tripped else 'no'} |")
    lines.append("")

    # Section A
    a = results["section_a"]
    lines.append("## Section A — per-month model accuracy")
    lines.append("")
    lines.append(f"**Verdict:** {a['verdict']}")
    lines.append("")
    lines.append("| month | n_pred | base | n_top | top-decile precision | Brier | AUC |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in a["monthly"]:
        lines.append(
            f"| {r['month']} | {r['n_predictions']} | "
            f"{_fmt(r['base_rate'], '.3f')} | {r['n_top_decile']} | "
            f"{_fmt(r['top_decile_precision'], '.3f')} | "
            f"{_fmt(r['brier_score'], '.3f')} | "
            f"{_fmt(r['auc'], '.3f')} |"
        )
    lines.append("")
    lines.append(
        f"Linear trends — top-decile precision slope "
        f"{_fmt(a['precision_trend'].get('slope'))} / month; AUC slope "
        f"{_fmt(a['auc_trend'].get('slope'))} / month; Brier slope "
        f"{_fmt(a['brier_trend'].get('slope'))} / month."
    )
    lines.append("")

    # Section B
    b = results["section_b"]
    lines.append("## Section B — per-month portfolio Sharpe")
    lines.append("")
    lines.append(f"**Verdict:** {b['verdict']}")
    lines.append("")
    lines.append("| month | n_trades | mean P&L | hit rate | Sharpe in month | max DD in month |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for r in b["monthly"]:
        lines.append(
            f"| {r['month']} | {r['n_trades']} | "
            f"{_fmt(r['mean_net_pnl_pct'], '.2%')} | "
            f"{_fmt(r['trade_hit_rate'], '.1%')} | "
            f"{_fmt(r['portfolio_sharpe_in_month'])} | "
            f"{_fmt(r['max_dd_in_month'], '.2%')} |"
        )
    lines.append("")

    # Section C
    c = results["section_c"]
    lines.append("## Section C — model precision vs portfolio Sharpe correlation")
    lines.append("")
    lines.append(f"**Verdict:** {c['verdict']}")
    lines.append("")
    lines.append(
        f"Spearman ρ = {_fmt(c['spearman_correlation'])}  "
        f"(p = {_fmt(c['spearman_pvalue'])}), "
        f"paired months = {c['n_paired']}."
    )
    lines.append("")

    # Section D
    d = results["section_d"]
    lines.append("## Section D — execution-friction stress test")
    lines.append("")
    lines.append(f"**Verdict:** {d['verdict']}")
    lines.append("")
    lines.append(
        "| scenario | +cost / trade | Sharpe | AnnRet | maxDD | final $ | PF |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in d["scenarios"]:
        lines.append(
            f"| {r['name']} | {r['extra_cost_per_trade']:.2%} | "
            f"{_fmt(r['portfolio_sharpe'])} | "
            f"{_fmt(r['portfolio_annualized_return_pct'], '.0%')} | "
            f"{_fmt(r['portfolio_max_dd_pct'], '.1%')} | "
            f"{_fmt(r['final_equity'], '.0f')} | "
            f"{_fmt(r['portfolio_profit_factor'], '.2f')} |"
        )
    for r in d["scenarios"]:
        lines.append(f"- `{r['name']}`: {r['description']}")
    lines.append("")

    # Section E
    e = results["section_e"]
    lines.append("## Section E — universe survivorship check")
    lines.append("")
    lines.append(f"**Verdict:** {e['verdict']}")
    lines.append("")
    lines.append(
        f"Out-of-universe-at-time rate: "
        f"**{_fmt(e['out_of_universe_rate'], '.2%')}** "
        f"({e['n_out_of_universe']} / {e['n_predictions_total']}). "
        f"Threshold: {e['flag_threshold']:.0%}."
    )
    lines.append("")
    lines.append("| month | n_pred | n_out | rate |")
    lines.append("|---|---:|---:|---:|")
    for r in e["monthly"]:
        lines.append(
            f"| {r['month']} | {r['n_total']} | "
            f"{r['n_out_of_universe']} | "
            f"{_fmt(r['out_of_universe_rate'], '.2%')} |"
        )
    if e["examples_out_of_universe"]:
        lines.append("")
        lines.append("Examples of out-of-universe predictions (first 20):")
        lines.append("")
        for ex in e["examples_out_of_universe"]:
            lines.append(f"- {ex['date']}  {ex['symbol']}")
    lines.append("")

    # Section F
    f = results["section_f"]
    lines.append("## Section F — cross-fold walk-forward stability")
    lines.append("")
    lines.append(f"**Verdict:** {f['verdict']}")
    lines.append("")
    lines.append(f"> {f['note']}")
    lines.append("")
    lines.append("| fold model_id | n_pred | top-decile prec. | AUC | n_trades | fold Sharpe |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for r in f["fold_rows"]:
        lines.append(
            f"| `{r['model_id']}` | {r['n_predictions']} | "
            f"{_fmt(r['top_decile_precision'], '.3f')} | "
            f"{_fmt(r['auc'], '.3f')} | "
            f"{r['n_locked_trades_in_fold']} | "
            f"{_fmt(r['portfolio_sharpe_in_fold'])} |"
        )
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    conn = get_connection()
    locked_trades = _load_locked_trades(conn)
    print(f"  loaded {len(locked_trades)} locked-winner trades from "
          f"crypto_backtest_trades WHERE run_id='{LOCKED_RUN_ID}'.")

    a = section_a(conn)
    b = section_b(conn, locked_trades)
    c = section_c(a, b)
    d = section_d(locked_trades)
    e = section_e(conn)
    f = section_f(conn, locked_trades)

    results = {
        "locked_run_id": LOCKED_RUN_ID,
        "full_window": {"start": FULL_START, "end": FULL_END},
        "section_a": a,
        "section_b": b,
        "section_c": c,
        "section_d": d,
        "section_e": e,
        "section_f": f,
    }
    results["synthesis"] = synthesize(results)

    _print_section("Synthesis")
    syn = results["synthesis"]
    print(f"  Confidence: {syn['confidence_label']}")
    print()
    print(f"  {syn['recommendation']}")
    print()
    print("  Signals tripped:")
    for sig, tripped in syn["signals"].items():
        marker = "YES" if tripped else " no"
        print(f"    {marker}  {sig}")

    clean = _strip_series(results)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(clean, indent=2, default=_json_default, sort_keys=False)
    )
    _write_markdown(OUT_MD, results)
    print()
    print(f"  Wrote {OUT_JSON}")
    print(f"  Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
