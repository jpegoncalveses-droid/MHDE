"""Phase 1B out-of-sample validation (pre-registered).

Tests whether the Phase 1B winner's headline metrics
(portfolio Sharpe 5.10, max DD -23.7 %, trade-P&L hit rate 87 %) survive
a clean train/test split of the executable trade window.

Pre-registered methodology
--------------------------

Full executable window (mirrors active_spec / Phase 1B):
    2025-04-05 -> 2026-05-09   ~398 days

Train window (policy/parameter selection):
    2025-04-05 -> 2026-01-31   ~302 days  (~76 %)

Test window (held out; never used for any decision):
    2026-02-01 -> 2026-05-09   ~ 97 days  (~24 %)

The test window must not inform:
  * which exit policy / selection rule / parameters win
  * any threshold or rule used during winner selection

Procedure
---------
1. Refit the Phase 1B grid on the TRAIN window only:
   base grid (2 horizons x 5 policies x 2 selection rules = 20 configs)
   + sensitivity sweep around the top-3 train-window base winners.
2. Pick winner_oos by the same procedure Phase 1B used:
   apply the 4 gates (ann.ret > 5 %, Sharpe > 1.0, max DD > -25 %,
   profit factor > 1.3), pick the highest portfolio Sharpe among
   the gate-passers. If no config passes all 4 gates, fall back to
   the top portfolio Sharpe and flag.
3. Run BOTH winner_oos AND the locked Phase 1B winner on the TEST window.
4. Run the locked Phase 1B winner on the TRAIN window for an apples-to-
   apples in-sample baseline.
5. Leave-one-month-out sensitivity on the locked Phase 1B winner's
   full-window trade set: drop each calendar month in turn, recompute
   portfolio Sharpe, flag months whose absence collapses the headline.
6. Monthly portfolio-return distribution from the locked winner's
   full-window equity curve.
7. BTC monthly-return regime classification (bull >= +5 %, bear <= -5 %,
   neutral otherwise) and per-regime Sharpe.

Reads
-----
crypto_ml_predictions (walkfold ids), crypto_prices_daily,
crypto_funding_rates, crypto_ml_features. Strictly read-only against
ML / price tables.

Writes
------
crypto_backtest_runs / crypto_backtest_trades / crypto_backtest_summary
get new rows for each (config, window) combination. Because
make_run_id folds (date_start, date_end) into the digest, every new
run_id is distinct from the locked Phase 1B run_id
'backtest_10d_D_top_n_a02e15a0'. The locked row is read-only.

Outputs
-------
data/exports/phase1b_oos_validation.json   structured results
data/exports/phase1b_oos_validation.md     human-readable report
also prints to terminal.
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

from crypto.execution.backtest.harness import (  # noqa: E402
    make_run_id,
    run_backtest,
)
from crypto.execution.backtest.metrics import (  # noqa: E402
    compute_and_persist_summary,
)
from crypto.execution.backtest.report import (  # noqa: E402
    PortfolioResult,
    simulate_portfolio,
)
from crypto.execution.backtest.runner import (  # noqa: E402
    GridConfig,
    base_grid_configs,
    sensitivity_grid_configs,
)
from storage.db import get_connection  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Pre-registered constants — DO NOT TUNE AFTER SEEING RESULTS
# ──────────────────────────────────────────────────────────────────────

FULL_START = date(2025, 4, 5)
FULL_END = date(2026, 5, 9)
TRAIN_START = FULL_START
TRAIN_END = date(2026, 1, 31)
TEST_START = date(2026, 2, 1)
TEST_END = FULL_END

LOCKED_RUN_ID = "backtest_10d_D_top_n_a02e15a0"
LOCKED_CFG = GridConfig(
    horizon="10d",
    policy="D",
    selection="top_n",
    selection_params={"n": 6},
    policy_params={"trail_pct": 0.3},
)

GATE_ANN_RET_MIN = 0.05          # > 5 %
GATE_SHARPE_MIN = 1.0            # > 1.0
GATE_MAX_DD_MIN = -0.25          # max_drawdown_pct > -0.25 (less negative)
GATE_PF_MIN = 1.3                # > 1.3

PORTFOLIO_SIM_KWARGS = dict(
    starting_capital=1000.0,
    max_positions=6,
    deploy_fraction=0.8,
    leverage=1.0,
)

REGIME_BULL_THRESHOLD = 0.05     # BTC monthly return >= +5 %
REGIME_BEAR_THRESHOLD = -0.05    # BTC monthly return <= -5 %

LOO_COLLAPSE_THRESHOLD = 0.5     # exclude-month Sharpe / full Sharpe < 0.5 flagged

# Sharpe annualisation constants — mirror report.simulate_portfolio.
SHARPE_PERIODS_PER_YEAR = 252
ANN_DAYS_PER_YEAR = 365.0

OUT_JSON = PROJECT_ROOT / "data" / "exports" / "phase1b_oos_validation.json"
OUT_MD = PROJECT_ROOT / "data" / "exports" / "phase1b_oos_validation.md"

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("validate_phase1b_oos")
log.setLevel(logging.INFO)


# ──────────────────────────────────────────────────────────────────────
# Harness-with-window wrapper
# ──────────────────────────────────────────────────────────────────────


def _run_one_with_window(
    conn: duckdb.DuckDBPyConnection,
    cfg: GridConfig,
    start: date,
    end: date,
) -> str:
    """Run ``cfg`` over ``[start, end]`` (idempotent). Returns run_id.

    Because :func:`make_run_id` folds date_start/date_end into the digest,
    train-window and test-window runs always get distinct run_ids, and
    neither can collide with the locked Phase 1B run_id (which was
    persisted with date_start=date_end=None).
    """
    rid = make_run_id(
        horizon=cfg.horizon,
        exit_policy_id=cfg.policy,
        selection_rule=cfg.selection,
        selection_params=cfg.selection_params,
        policy_params=cfg.policy_params,
        date_start=start,
        date_end=end,
    )
    exists = (
        conn.execute(
            "SELECT 1 FROM crypto_backtest_runs WHERE run_id = ?", [rid]
        ).fetchone()
        is not None
    )
    has_summary = exists and (
        conn.execute(
            "SELECT 1 FROM crypto_backtest_summary WHERE run_id = ?", [rid]
        ).fetchone()
        is not None
    )
    if not exists:
        run_backtest(
            conn,
            horizon=cfg.horizon,
            exit_policy_id=cfg.policy,
            selection_rule=cfg.selection,
            selection_params=cfg.selection_params,
            policy_params=cfg.policy_params,
            date_start=start,
            date_end=end,
            dry_run=False,
            force=False,
        )
    if not has_summary:
        compute_and_persist_summary(conn, rid)
    return rid


def _run_grid_with_window(
    conn: duckdb.DuckDBPyConnection,
    configs: list[GridConfig],
    start: date,
    end: date,
    *,
    label: str,
) -> dict[str, GridConfig]:
    """Run each cfg on [start, end]; return {run_id: cfg}."""
    out: dict[str, GridConfig] = {}
    n = len(configs)
    for i, cfg in enumerate(configs, start=1):
        rid = _run_one_with_window(conn, cfg, start, end)
        out[rid] = cfg
        log.info(
            "  [%s %d/%d] %s -> %s", label, i, n,
            f"{cfg.horizon}/{cfg.policy}/{cfg.selection} "
            f"sel={cfg.selection_params} pol={cfg.policy_params}",
            rid,
        )
    return out


# ──────────────────────────────────────────────────────────────────────
# Portfolio sim on an in-memory trades frame (for LOO + regime)
# ──────────────────────────────────────────────────────────────────────


def _coerce_date(x: Any) -> Optional[date]:
    if x is None:
        return None
    if hasattr(x, "to_pydatetime"):
        return x.to_pydatetime().date()
    if hasattr(x, "date") and not isinstance(x, date):
        return x.date()
    return x


def _load_trades(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT trade_id, coin, entry_date, exit_date,
               net_pnl_pct, probability_at_entry
        FROM crypto_backtest_trades
        WHERE run_id = ?
          AND entry_date IS NOT NULL
          AND exit_date IS NOT NULL
          AND net_pnl_pct IS NOT NULL
        ORDER BY entry_date, trade_id
        """,
        [run_id],
    ).fetchdf()


def _simulate_on_trades(
    trades: pd.DataFrame,
    *,
    starting_capital: float = 1000.0,
    max_positions: int = 6,
    deploy_fraction: float = 0.8,
    leverage: float = 1.0,
) -> dict[str, Any]:
    """Portfolio compounding on an in-memory trade frame.

    Mirrors :func:`crypto.execution.backtest.report.simulate_portfolio`
    so leave-one-month-out and regime-conditional metrics are computed
    on the same definition of portfolio Sharpe used by Phase 1B's
    decision logic.
    """
    empty_result = {
        "final_equity": starting_capital,
        "total_return_pct": 0.0,
        "annualized_return_pct": 0.0,
        "sharpe_ratio": None,
        "max_drawdown_pct": 0.0,
        "profit_factor": None,
        "n_trades_taken": 0,
        "n_trades_skipped_capacity": 0,
        "span_days": 0,
        "n_trades_input": len(trades),
    }
    if trades.empty:
        return empty_result

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
        entry_d = row.entry_date
        exit_d = row.exit_date
        net = float(row.net_pnl_pct)

        still_open: list[dict[str, Any]] = []
        for pos in sorted(
            open_positions, key=lambda p: (p["exit_date"], p["trade_id"])
        ):
            if pos["exit_date"] < entry_d:
                pnl = pos["size"] * pos["net_pnl_pct"]
                equity += pnl
                closes.append(
                    {
                        "date": pos["exit_date"],
                        "pnl_dollars": pnl,
                        "trade_id": pos["trade_id"],
                    }
                )
            else:
                still_open.append(pos)
        open_positions = still_open

        if len(open_positions) >= max_positions:
            n_skipped += 1
            continue

        size = (equity * deploy_fraction * leverage) / max_positions
        open_positions.append(
            {
                "trade_id": row.trade_id,
                "coin": row.coin,
                "entry_date": entry_d,
                "exit_date": exit_d,
                "size": size,
                "net_pnl_pct": net,
            }
        )
        n_taken += 1

    for pos in sorted(
        open_positions, key=lambda p: (p["exit_date"], p["trade_id"])
    ):
        pnl = pos["size"] * pos["net_pnl_pct"]
        equity += pnl
        closes.append(
            {
                "date": pos["exit_date"],
                "pnl_dollars": pnl,
                "trade_id": pos["trade_id"],
            }
        )

    if not closes:
        return empty_result

    closes_df = pd.DataFrame(closes)
    daily_pnl = closes_df.groupby("date", sort=True)["pnl_dollars"].sum()
    cumulative = starting_capital + daily_pnl.cumsum()
    first_date = daily_pnl.index.min()
    prelude = pd.Series(
        {first_date - pd.Timedelta(days=1): starting_capital}
    ).rename_axis("date")
    equity_series = pd.concat([prelude, cumulative]).sort_index()

    final_equity = float(equity_series.iloc[-1])
    first_entry = t["entry_date"].min()
    last_exit = t["exit_date"].max()
    span_days = (
        max(0, (last_exit - first_entry).days)
        if first_entry and last_exit
        else 0
    )

    total_ret = (final_equity - starting_capital) / starting_capital
    annualized = (
        total_ret * (ANN_DAYS_PER_YEAR / float(span_days))
        if span_days > 0
        else 0.0
    )

    rets = pd.Series(equity_series.values).pct_change().dropna()
    if len(rets) >= 2 and rets.std(ddof=1) > 0:
        sharpe = float(
            rets.mean()
            / rets.std(ddof=1)
            * (SHARPE_PERIODS_PER_YEAR**0.5)
        )
    else:
        sharpe = None

    eq = equity_series.astype(float)
    peak = eq.cummax()
    dd = float(((eq - peak) / peak).min()) if len(eq) else 0.0

    winners = float(
        closes_df.loc[closes_df["pnl_dollars"] > 0, "pnl_dollars"].sum()
    )
    losers = float(
        closes_df.loc[closes_df["pnl_dollars"] <= 0, "pnl_dollars"].sum()
    )
    if winners == 0 and losers == 0:
        pf: Optional[float] = None
    elif losers == 0:
        pf = math.inf if winners > 0 else None
    else:
        pf = winners / abs(losers)

    return {
        "final_equity": final_equity,
        "total_return_pct": float(total_ret),
        "annualized_return_pct": float(annualized),
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": dd,
        "profit_factor": pf,
        "n_trades_taken": int(n_taken),
        "n_trades_skipped_capacity": int(n_skipped),
        "span_days": int(span_days),
        "n_trades_input": int(len(t)),
        "equity_series": equity_series,  # for monthly distribution
        "trade_pnl_pct_series": t["net_pnl_pct"].astype(float),
    }


# ──────────────────────────────────────────────────────────────────────
# Step 1-2: refit grid on train window, pick winner_oos
# ──────────────────────────────────────────────────────────────────────


def _portfolio_metrics_for_run(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> dict[str, Any]:
    """Run simulate_portfolio + read trade-level hit rate from summary."""
    pr = simulate_portfolio(conn, run_id, **PORTFOLIO_SIM_KWARGS)
    summary = conn.execute(
        "SELECT hit_rate, profit_factor FROM crypto_backtest_summary "
        "WHERE run_id = ?",
        [run_id],
    ).fetchone()
    trade_hit_rate = float(summary[0]) if summary and summary[0] is not None else None
    return {
        "run_id": run_id,
        "portfolio_sharpe": (
            None if (pr.sharpe_ratio is None or math.isnan(pr.sharpe_ratio))
            else float(pr.sharpe_ratio)
        ),
        "portfolio_max_dd_pct": float(pr.max_drawdown_pct),
        "portfolio_annualized_return_pct": float(pr.annualized_return_pct),
        "portfolio_profit_factor": (
            None if (pr.profit_factor is None or math.isnan(pr.profit_factor)
                     or math.isinf(pr.profit_factor))
            else float(pr.profit_factor)
        ),
        "portfolio_final_equity": float(pr.final_equity),
        "n_trades_taken": int(pr.n_trades_taken),
        "n_trades_skipped_capacity": int(pr.n_trades_skipped_capacity),
        "span_days": int(pr.span_days),
        "trade_hit_rate": trade_hit_rate,
        "passes_all_gates": pr.passes_all_criteria,
    }


def _select_winner_oos(
    conn: duckdb.DuckDBPyConnection,
    candidate_run_ids: list[str],
) -> tuple[str, list[dict[str, Any]], bool]:
    """Pick the train-window winner by Phase 1B's decision rule.

    Phase 1B: among configs that pass all 4 gates, pick highest portfolio
    Sharpe. If no config passes, fall back to top portfolio Sharpe (and
    flag).

    Returns ``(winner_run_id, all_candidate_metrics, all_gates_passed)``.
    """
    metrics = [_portfolio_metrics_for_run(conn, rid) for rid in candidate_run_ids]
    passers = [m for m in metrics if m["passes_all_gates"]]
    if passers:
        winner = max(
            passers,
            key=lambda m: m["portfolio_sharpe"] or -math.inf,
        )
        return winner["run_id"], metrics, True
    winner = max(
        metrics, key=lambda m: m["portfolio_sharpe"] or -math.inf
    )
    return winner["run_id"], metrics, False


# ──────────────────────────────────────────────────────────────────────
# Step 5: leave-one-month-out
# ──────────────────────────────────────────────────────────────────────


def _leave_one_month_out(trades: pd.DataFrame) -> dict[str, Any]:
    full = _simulate_on_trades(trades, **PORTFOLIO_SIM_KWARGS)
    full_sharpe = full["sharpe_ratio"]

    t = trades.copy()
    t["_exit_month"] = (
        pd.to_datetime(t["exit_date"]).dt.to_period("M").astype(str)
    )
    months = sorted(t["_exit_month"].unique())

    rows: list[dict[str, Any]] = []
    for m in months:
        n_excluded = int((t["_exit_month"] == m).sum())
        subset = t.loc[t["_exit_month"] != m].drop(columns=["_exit_month"])
        sim = _simulate_on_trades(subset, **PORTFOLIO_SIM_KWARGS)
        sh = sim["sharpe_ratio"]
        if (
            sh is not None
            and full_sharpe is not None
            and full_sharpe not in (0.0, None)
        ):
            ratio = float(sh) / float(full_sharpe)
        else:
            ratio = None
        rows.append(
            {
                "excluded_month": m,
                "n_trades_excluded": n_excluded,
                "sharpe_without_month": sh,
                "ratio_to_full_sharpe": ratio,
                "final_equity_without_month": float(sim["final_equity"]),
                "max_dd_without_month": float(sim["max_drawdown_pct"]),
                "collapse_flagged": (
                    ratio is not None and ratio < LOO_COLLAPSE_THRESHOLD
                ),
            }
        )
    return {
        "full_sharpe": full_sharpe,
        "full_final_equity": float(full["final_equity"]),
        "full_max_dd": float(full["max_drawdown_pct"]),
        "by_month": rows,
    }


# ──────────────────────────────────────────────────────────────────────
# Step 6: monthly return distribution from equity curve
# ──────────────────────────────────────────────────────────────────────


def _monthly_distribution(equity_series: pd.Series) -> dict[str, Any]:
    """Monthly portfolio returns from a date-indexed equity series."""
    if equity_series.empty:
        return {"months": [], "percentiles": {}, "n_months": 0}
    df = pd.DataFrame({"equity": equity_series.values})
    df["date"] = pd.to_datetime(equity_series.index)
    df = df.set_index("date").sort_index()
    monthly_last = df["equity"].resample("ME").last().ffill()
    monthly_ret = monthly_last.pct_change().dropna()
    # First-month return computed against the starting capital (the prelude).
    first_month = monthly_last.index.min()
    if first_month is not None:
        starting = float(equity_series.iloc[0])
        first_ret = (monthly_last.iloc[0] - starting) / starting
        monthly_ret = pd.concat(
            [pd.Series({first_month: first_ret}), monthly_ret]
        ).sort_index()

    pcts = {
        "p5": float(monthly_ret.quantile(0.05)) if len(monthly_ret) else None,
        "p25": float(monthly_ret.quantile(0.25)) if len(monthly_ret) else None,
        "p50": float(monthly_ret.quantile(0.50)) if len(monthly_ret) else None,
        "p75": float(monthly_ret.quantile(0.75)) if len(monthly_ret) else None,
        "p95": float(monthly_ret.quantile(0.95)) if len(monthly_ret) else None,
        "mean": float(monthly_ret.mean()) if len(monthly_ret) else None,
        "std": float(monthly_ret.std(ddof=1)) if len(monthly_ret) > 1 else None,
    }
    months = [
        {
            "month": ts.to_period("M").strftime("%Y-%m"),
            "return_pct": float(r),
        }
        for ts, r in monthly_ret.items()
    ]
    return {"months": months, "percentiles": pcts, "n_months": len(months)}


# ──────────────────────────────────────────────────────────────────────
# Step 7: BTC regime classification + per-regime Sharpe
# ──────────────────────────────────────────────────────────────────────


def _btc_regime_map(
    conn: duckdb.DuckDBPyConnection, start: date, end: date
) -> dict[str, dict[str, Any]]:
    btc = conn.execute(
        """
        SELECT trade_date, close FROM crypto_prices_daily
        WHERE symbol = 'BTCUSDT' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [start, end],
    ).fetchdf()
    if btc.empty:
        return {}
    btc["trade_date"] = pd.to_datetime(btc["trade_date"])
    btc = btc.set_index("trade_date").sort_index()
    m_first = btc["close"].resample("ME").first()
    m_last = btc["close"].resample("ME").last()
    monthly_ret = (m_last / m_first) - 1.0

    out: dict[str, dict[str, Any]] = {}
    for ts, ret in monthly_ret.items():
        ym = ts.to_period("M").strftime("%Y-%m")
        if pd.isna(ret):
            regime = "unknown"
            ret_v: Optional[float] = None
        else:
            ret_v = float(ret)
            if ret_v >= REGIME_BULL_THRESHOLD:
                regime = "bull"
            elif ret_v <= REGIME_BEAR_THRESHOLD:
                regime = "bear"
            else:
                regime = "neutral"
        out[ym] = {"btc_ret": ret_v, "regime": regime}
    return out


def _regime_conditional_metrics(
    trades: pd.DataFrame, regimes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Compute per-regime portfolio Sharpe by re-simulating on trades
    whose ``exit_date`` falls in regime months."""
    t = trades.copy()
    t["_exit_month"] = (
        pd.to_datetime(t["exit_date"]).dt.to_period("M").astype(str)
    )
    months_by_regime: dict[str, list[str]] = {
        "bull": [], "neutral": [], "bear": [], "unknown": []
    }
    for ym, info in regimes.items():
        months_by_regime.setdefault(info["regime"], []).append(ym)

    by_regime: dict[str, Any] = {}
    for regime, months in months_by_regime.items():
        sub = t.loc[t["_exit_month"].isin(months)].drop(
            columns=["_exit_month"]
        )
        sim = _simulate_on_trades(sub, **PORTFOLIO_SIM_KWARGS)
        # Trade win-rate within the regime
        if len(sub):
            win_rate = float((sub["net_pnl_pct"] > 0).mean())
            mean_pnl = float(sub["net_pnl_pct"].mean())
        else:
            win_rate = None
            mean_pnl = None
        by_regime[regime] = {
            "n_months": len(months),
            "n_trades": int(len(sub)),
            "trade_win_rate": win_rate,
            "mean_trade_net_pnl_pct": mean_pnl,
            "portfolio_sharpe_in_regime": sim["sharpe_ratio"],
            "portfolio_max_dd_in_regime": sim["max_drawdown_pct"],
        }
    return {
        "regimes_by_month": regimes,
        "metrics_by_regime": by_regime,
        "regime_thresholds": {
            "bull_min_btc_monthly_ret": REGIME_BULL_THRESHOLD,
            "bear_max_btc_monthly_ret": REGIME_BEAR_THRESHOLD,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# Verdict
# ──────────────────────────────────────────────────────────────────────


def _verdict_for_oos_sharpe(sharpe: Optional[float]) -> str:
    if sharpe is None or math.isnan(sharpe):
        return (
            "INDETERMINATE: test-window Sharpe is undefined "
            "(too few trades or zero-variance returns)."
        )
    s = float(sharpe)
    if s > 3.0:
        return (
            "REAL EDGE, HEADLINE REASONABLE: out-of-sample Sharpe > 3.0 "
            "is consistent with the in-sample headline."
        )
    if s >= 1.5:
        return (
            "REAL BUT SMALLER EDGE: out-of-sample Sharpe in 1.5-3.0 — "
            "the signal survives the holdout but the headline overstates it."
        )
    if s >= 0.5:
        return (
            "MARGINAL EDGE: out-of-sample Sharpe in 0.5-1.5 — "
            "headline was inflated 2-3x by in-sample selection bias."
        )
    return (
        "NO REAL EDGE DETECTED: out-of-sample Sharpe < 0.5 — "
        "the headline metrics are consistent with selection bias only."
    )


# ──────────────────────────────────────────────────────────────────────
# Output formatting
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


def _cfg_to_dict(cfg: GridConfig) -> dict[str, Any]:
    return {
        "horizon": cfg.horizon,
        "policy": cfg.policy,
        "selection": cfg.selection,
        "selection_params": dict(cfg.selection_params),
        "policy_params": dict(cfg.policy_params),
    }


def _print_section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _print_compare_row(
    label: str,
    in_sample: dict[str, Any],
    out_of_sample: dict[str, Any],
) -> None:
    is_sh = in_sample.get("portfolio_sharpe")
    oos_sh = out_of_sample.get("portfolio_sharpe")
    ratio = (
        (oos_sh / is_sh)
        if (is_sh and oos_sh and is_sh != 0)
        else None
    )
    print(f"  {label}")
    print(
        f"    In-sample (train) : Sharpe={_fmt(is_sh)}  "
        f"DD={_fmt(in_sample.get('portfolio_max_dd_pct'), '.2%')}  "
        f"AnnRet={_fmt(in_sample.get('portfolio_annualized_return_pct'), '.2%')}  "
        f"PF={_fmt(in_sample.get('portfolio_profit_factor'))}  "
        f"hit_rate={_fmt(in_sample.get('trade_hit_rate'), '.2%')}  "
        f"n_trades={in_sample.get('n_trades_taken')}"
    )
    print(
        f"    OOS (test)        : Sharpe={_fmt(oos_sh)}  "
        f"DD={_fmt(out_of_sample.get('portfolio_max_dd_pct'), '.2%')}  "
        f"AnnRet={_fmt(out_of_sample.get('portfolio_annualized_return_pct'), '.2%')}  "
        f"PF={_fmt(out_of_sample.get('portfolio_profit_factor'))}  "
        f"hit_rate={_fmt(out_of_sample.get('trade_hit_rate'), '.2%')}  "
        f"n_trades={out_of_sample.get('n_trades_taken')}"
    )
    print(
        f"    Degradation ratio (OOS / IS Sharpe) = {_fmt(ratio)}"
    )


def _print_loo(loo: dict[str, Any]) -> None:
    full_sh = loo["full_sharpe"]
    print(f"  full-window Sharpe: {_fmt(full_sh)}    "
          f"(threshold for 'collapse' flag: ratio < {LOO_COLLAPSE_THRESHOLD})")
    print()
    print(f"  {'month':<9}  {'n_excluded':>10}  {'Sharpe wo':>10}  "
          f"{'ratio':>7}  {'final $':>10}  {'maxDD':>7}  flag")
    print("  " + "-" * 70)
    for r in loo["by_month"]:
        flag = " <- COLLAPSE" if r["collapse_flagged"] else ""
        print(
            f"  {r['excluded_month']:<9}  "
            f"{r['n_trades_excluded']:>10}  "
            f"{_fmt(r['sharpe_without_month']):>10}  "
            f"{_fmt(r['ratio_to_full_sharpe']):>7}  "
            f"{_fmt(r['final_equity_without_month'], '.0f'):>10}  "
            f"{_fmt(r['max_dd_without_month'], '.1%'):>7}"
            f"{flag}"
        )


def _print_monthly_dist(dist: dict[str, Any]) -> None:
    p = dist["percentiles"]
    print(f"  n_months: {dist['n_months']}")
    print(f"  mean: {_fmt(p['mean'], '.2%')}    std: {_fmt(p['std'], '.2%')}")
    print(
        f"  p5: {_fmt(p['p5'], '.2%')}   p25: {_fmt(p['p25'], '.2%')}   "
        f"p50: {_fmt(p['p50'], '.2%')}   p75: {_fmt(p['p75'], '.2%')}   "
        f"p95: {_fmt(p['p95'], '.2%')}"
    )
    print()
    print(f"  {'month':<9}  {'return':>8}")
    print("  " + "-" * 22)
    for m in dist["months"]:
        print(f"  {m['month']:<9}  {_fmt(m['return_pct'], '.2%'):>8}")


def _print_regimes(reg: dict[str, Any]) -> None:
    print(
        f"  Regime thresholds: bull >= +{REGIME_BULL_THRESHOLD:.0%}, "
        f"bear <= {REGIME_BEAR_THRESHOLD:.0%}"
    )
    rbm = reg["regimes_by_month"]
    counts: dict[str, int] = {"bull": 0, "neutral": 0, "bear": 0, "unknown": 0}
    for info in rbm.values():
        counts[info["regime"]] = counts.get(info["regime"], 0) + 1
    print(
        f"  Month counts: bull={counts.get('bull', 0)}  "
        f"neutral={counts.get('neutral', 0)}  bear={counts.get('bear', 0)}  "
        f"unknown={counts.get('unknown', 0)}"
    )
    if counts.get("bear", 0) < 6:
        print(
            "  NOTE: fewer than 6 bear months in sample — there is NO "
            "sustained broad-bear period to validate against."
        )
    print()
    print(f"  {'regime':<8}  {'months':>6}  {'trades':>6}  "
          f"{'trade win':>9}  {'mean P&L':>9}  {'port Sharpe':>11}  "
          f"{'port maxDD':>10}")
    print("  " + "-" * 70)
    for regime in ("bull", "neutral", "bear", "unknown"):
        info = reg["metrics_by_regime"].get(regime)
        if info is None or info["n_months"] == 0:
            continue
        print(
            f"  {regime:<8}  {info['n_months']:>6}  "
            f"{info['n_trades']:>6}  "
            f"{_fmt(info['trade_win_rate'], '.2%'):>9}  "
            f"{_fmt(info['mean_trade_net_pnl_pct'], '.2%'):>9}  "
            f"{_fmt(info['portfolio_sharpe_in_regime']):>11}  "
            f"{_fmt(info['portfolio_max_dd_in_regime'], '.2%'):>10}"
        )


# ──────────────────────────────────────────────────────────────────────
# Markdown writer
# ──────────────────────────────────────────────────────────────────────


def _write_markdown(out_path: Path, results: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# Phase 1B Out-of-Sample Validation")
    lines.append("")
    lines.append("**Pre-registered.** Methodology was locked before any "
                 "results were inspected. See module docstring of "
                 "`crypto/execution/backtest/scripts/validate_phase1b_oos.py`.")
    lines.append("")
    lines.append(f"- Full window: {FULL_START} → {FULL_END}")
    lines.append(f"- Train window: {TRAIN_START} → {TRAIN_END}")
    lines.append(f"- Test window:  {TEST_START} → {TEST_END}")
    lines.append("")
    lines.append("## Headline verdict")
    lines.append("")
    locked = results["locked_winner"]
    oos = results["oos_refit_winner"]
    lines.append(f"**Locked Phase 1B winner ({LOCKED_RUN_ID}):**")
    lines.append("")
    lines.append(
        f"- Out-of-sample Sharpe: **{_fmt(locked['test']['portfolio_sharpe'])}**  "
        f"(in-sample train Sharpe: {_fmt(locked['train']['portfolio_sharpe'])}, "
        f"full-window headline Sharpe: {_fmt(locked['full']['portfolio_sharpe'])})"
    )
    lines.append(f"- Degradation ratio (OOS / train Sharpe): "
                 f"**{_fmt(results['degradation']['locked_oos_over_train'])}**")
    lines.append("")
    lines.append(f"- Verdict: {results['verdict']['locked']}")
    lines.append("")
    lines.append(
        f"**OOS-refit winner ({oos['winner_run_id']}):** "
        f"config = `{json.dumps(_cfg_to_dict(oos['winner_cfg']))}`"
    )
    lines.append("")
    if not oos["all_gates_passed_in_train"]:
        lines.append(
            "> **Flag:** no train-window config passed all 4 Phase 1B gates; "
            "winner_oos is the top portfolio-Sharpe candidate regardless."
        )
        lines.append("")
    lines.append(
        f"- Out-of-sample Sharpe: **{_fmt(oos['test']['portfolio_sharpe'])}**  "
        f"(in-sample train Sharpe: {_fmt(oos['train']['portfolio_sharpe'])})"
    )
    lines.append(f"- Degradation ratio (OOS / train Sharpe): "
                 f"**{_fmt(results['degradation']['oos_refit_oos_over_train'])}**")
    lines.append(f"- Verdict: {results['verdict']['oos_refit']}")
    lines.append("")

    lines.append("## In-sample vs out-of-sample table")
    lines.append("")
    lines.append("| config | window | Sharpe | maxDD | ann.ret | PF | trade hit-rate | n_trades |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")

    def _row(label: str, window: str, m: dict[str, Any]) -> str:
        return (
            f"| {label} | {window} | "
            f"{_fmt(m['portfolio_sharpe'])} | "
            f"{_fmt(m['portfolio_max_dd_pct'], '.2%')} | "
            f"{_fmt(m['portfolio_annualized_return_pct'], '.2%')} | "
            f"{_fmt(m['portfolio_profit_factor'])} | "
            f"{_fmt(m['trade_hit_rate'], '.2%')} | "
            f"{m['n_trades_taken']} |"
        )

    lines.append(_row("locked Phase 1B", "FULL", locked["full"]))
    lines.append(_row("locked Phase 1B", "TRAIN", locked["train"]))
    lines.append(_row("locked Phase 1B", "TEST (OOS)", locked["test"]))
    lines.append(_row("OOS-refit winner", "TRAIN (in-sample for refit)", oos["train"]))
    lines.append(_row("OOS-refit winner", "TEST (OOS)", oos["test"]))
    lines.append("")

    lines.append("## Train-window grid candidates (top 10 by portfolio Sharpe)")
    lines.append("")
    lines.append("| rank | run_id | gates | Sharpe | maxDD | PF | n_trades |")
    lines.append("|---:|---|:---:|---:|---:|---:|---:|")
    sorted_cands = sorted(
        results["train_grid_candidates"],
        key=lambda m: m["portfolio_sharpe"] or -math.inf,
        reverse=True,
    )
    for i, m in enumerate(sorted_cands[:10], start=1):
        gates = "PASS" if m["passes_all_gates"] else "fail"
        lines.append(
            f"| {i} | `{m['run_id']}` | {gates} | "
            f"{_fmt(m['portfolio_sharpe'])} | "
            f"{_fmt(m['portfolio_max_dd_pct'], '.2%')} | "
            f"{_fmt(m['portfolio_profit_factor'])} | "
            f"{m['n_trades_taken']} |"
        )
    lines.append("")

    lines.append("## Leave-one-month-out (locked Phase 1B winner, full window)")
    lines.append("")
    loo = results["loo"]
    lines.append(
        f"Full-window Sharpe: **{_fmt(loo['full_sharpe'])}**. "
        f"Collapse threshold: ratio < {LOO_COLLAPSE_THRESHOLD}."
    )
    lines.append("")
    lines.append("| excluded month | n_trades excluded | Sharpe without month | ratio to full | final $ | maxDD | collapse? |")
    lines.append("|---|---:|---:|---:|---:|---:|:---:|")
    for r in loo["by_month"]:
        lines.append(
            f"| {r['excluded_month']} | {r['n_trades_excluded']} | "
            f"{_fmt(r['sharpe_without_month'])} | "
            f"{_fmt(r['ratio_to_full_sharpe'])} | "
            f"{_fmt(r['final_equity_without_month'], '.0f')} | "
            f"{_fmt(r['max_dd_without_month'], '.1%')} | "
            f"{'YES' if r['collapse_flagged'] else ''} |"
        )
    lines.append("")

    lines.append("## Monthly portfolio-return distribution")
    lines.append("")
    md = results["monthly_distribution"]
    p = md["percentiles"]
    lines.append(f"n_months: {md['n_months']}")
    lines.append("")
    lines.append("| percentile | monthly return |")
    lines.append("|---|---:|")
    for k in ("p5", "p25", "p50", "p75", "p95"):
        lines.append(f"| {k} | {_fmt(p[k], '.2%')} |")
    lines.append(f"| mean | {_fmt(p['mean'], '.2%')} |")
    lines.append(f"| std  | {_fmt(p['std'], '.2%')} |")
    lines.append("")
    lines.append("| month | return |")
    lines.append("|---|---:|")
    for m in md["months"]:
        lines.append(f"| {m['month']} | {_fmt(m['return_pct'], '.2%')} |")
    lines.append("")

    lines.append("## BTC-regime-conditional metrics")
    lines.append("")
    reg = results["regimes"]
    lines.append(
        f"Regime thresholds: bull ≥ +{REGIME_BULL_THRESHOLD:.0%} BTC monthly "
        f"return; bear ≤ {REGIME_BEAR_THRESHOLD:.0%}; neutral otherwise."
    )
    lines.append("")
    lines.append("| regime | months | trades | trade win-rate | mean trade P&L | port Sharpe | port maxDD |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for regime in ("bull", "neutral", "bear", "unknown"):
        info = reg["metrics_by_regime"].get(regime)
        if info is None or info["n_months"] == 0:
            continue
        lines.append(
            f"| {regime} | {info['n_months']} | {info['n_trades']} | "
            f"{_fmt(info['trade_win_rate'], '.2%')} | "
            f"{_fmt(info['mean_trade_net_pnl_pct'], '.2%')} | "
            f"{_fmt(info['portfolio_sharpe_in_regime'])} | "
            f"{_fmt(info['portfolio_max_dd_in_regime'], '.2%')} |"
        )
    lines.append("")

    lines.append("## Caveats and known limits")
    lines.append("")
    lines.append(
        f"- Test window is **{(TEST_END - TEST_START).days} days** "
        f"({results['locked_winner']['test']['n_trades_taken']} trades for the "
        f"locked winner). Sharpe estimates on a window this short are noisy; "
        f"a degradation ratio of (e.g.) 0.6 may reflect either real loss of "
        f"edge OR the small-sample variance, and the two are not separable "
        f"from a single split."
    )
    lines.append(
        "- Walk-forward over **configurations** is not done here — this is a "
        "single train/test split, which is itself a single sample from the "
        "space of possible splits. A more powerful validation would use rolling "
        "or expanding-window walk-forward of the policy-selection step."
    )
    lines.append(
        "- No sustained broad-bear period is present in the data. Regime-bear "
        "rows are individual months, not a multi-month drawdown."
    )
    lines.append(
        "- `trade_hit_rate` is the **trade-level net P&L positivity** "
        "(`crypto_backtest_summary.hit_rate`). The model's true label-hit rate "
        "on top-6 is ~48 % (see `docs/strategy_analysis_2026-05-10.md`)."
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")


# ──────────────────────────────────────────────────────────────────────
# JSON serializer
# ──────────────────────────────────────────────────────────────────────


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (date,)):
        return obj.isoformat()
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, GridConfig):
        return _cfg_to_dict(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, float):
        if math.isnan(obj):
            return None
        if math.isinf(obj):
            return None
        return obj
    raise TypeError(f"not JSON-serializable: {type(obj)}")


def _strip_series_for_json(obj: Any) -> Any:
    """Drop pandas Series objects (equity_series, trade_pnl_pct_series) so
    the JSON dump stays compact and serializable."""
    if isinstance(obj, dict):
        return {
            k: _strip_series_for_json(v)
            for k, v in obj.items()
            if not isinstance(v, (pd.Series, pd.DataFrame))
        }
    if isinstance(obj, list):
        return [_strip_series_for_json(x) for x in obj]
    return obj


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    conn = get_connection()

    _print_section("Phase 1B OOS validation — pre-registered methodology")
    print(f"  Full window  : {FULL_START} -> {FULL_END}")
    print(f"  Train window : {TRAIN_START} -> {TRAIN_END}")
    print(f"  Test window  : {TEST_START} -> {TEST_END}")
    print(f"  Locked winner: {LOCKED_RUN_ID}")

    # ─ Step 1: base grid on train ───────────────────────────────────────
    _print_section("Step 1 — refit base grid on TRAIN window")
    base_cfgs = base_grid_configs()
    print(f"  base grid size: {len(base_cfgs)} configs")
    train_base = _run_grid_with_window(
        conn, base_cfgs, TRAIN_START, TRAIN_END, label="train-base"
    )

    # ─ Step 2: top-3 by summary.sharpe_ratio (sum-of-fractions), refit
    #          sensitivity around them ───────────────────────────────────
    base_summaries = conn.execute(
        f"""
        SELECT run_id, sharpe_ratio FROM crypto_backtest_summary
        WHERE run_id IN ({','.join(['?'] * len(train_base))})
        ORDER BY sharpe_ratio DESC NULLS LAST
        """,
        list(train_base),
    ).fetchall()
    top3 = [r[0] for r in base_summaries[:3]]
    _print_section("Step 2 — sensitivity sweep around top-3 train-window bases")
    print(f"  top-3 train bases (by sum-of-fractions Sharpe):")
    for rid, sh in base_summaries[:3]:
        print(f"    {rid}    sharpe(sum-of-fractions)={sh:.3f}")
    sens_cfgs = sensitivity_grid_configs(conn, top3)
    print(f"  sensitivity configs emitted: {len(sens_cfgs)} "
          f"(some may collide with base; idempotent runner handles that)")
    train_sens = _run_grid_with_window(
        conn, sens_cfgs, TRAIN_START, TRAIN_END, label="train-sens"
    )

    # ─ Step 3: pick winner_oos on TRAIN portfolio metrics ───────────────
    _print_section("Step 3 — pick winner_oos from train-window candidates")
    all_train = {**train_base, **train_sens}
    winner_rid, all_metrics, all_passed = _select_winner_oos(
        conn, list(all_train)
    )
    winner_cfg = all_train[winner_rid]
    print(f"  candidates evaluated: {len(all_metrics)}")
    gate_pass_count = sum(1 for m in all_metrics if m["passes_all_gates"])
    print(f"  candidates passing all 4 Phase 1B gates: {gate_pass_count}")
    print(f"  winner_oos run_id: {winner_rid}")
    print(f"  winner_oos cfg   : {_cfg_to_dict(winner_cfg)}")
    if not all_passed:
        print("  >> NOTE: zero train-window configs passed all 4 gates; "
              "winner_oos is top-portfolio-Sharpe fallback.")

    # ─ Step 4: apply both winners to test window + locked to train ──────
    _print_section("Step 4 — apply winners to test window")
    locked_train_rid = _run_one_with_window(
        conn, LOCKED_CFG, TRAIN_START, TRAIN_END
    )
    locked_test_rid = _run_one_with_window(
        conn, LOCKED_CFG, TEST_START, TEST_END
    )
    winner_oos_test_rid = _run_one_with_window(
        conn, winner_cfg, TEST_START, TEST_END
    )

    locked_train = _portfolio_metrics_for_run(conn, locked_train_rid)
    locked_test = _portfolio_metrics_for_run(conn, locked_test_rid)
    locked_full = _portfolio_metrics_for_run(conn, LOCKED_RUN_ID)
    winner_oos_train = _portfolio_metrics_for_run(conn, winner_rid)
    winner_oos_test = _portfolio_metrics_for_run(conn, winner_oos_test_rid)

    _print_section("In-sample vs out-of-sample comparison")
    print()
    _print_compare_row("LOCKED Phase 1B winner", locked_train, locked_test)
    print()
    _print_compare_row("OOS-REFIT winner", winner_oos_train, winner_oos_test)
    print()
    print(f"  Full-window LOCKED reference (the headline):")
    print(
        f"    Sharpe={_fmt(locked_full['portfolio_sharpe'])}  "
        f"DD={_fmt(locked_full['portfolio_max_dd_pct'], '.2%')}  "
        f"AnnRet={_fmt(locked_full['portfolio_annualized_return_pct'], '.2%')}  "
        f"PF={_fmt(locked_full['portfolio_profit_factor'])}  "
        f"hit_rate={_fmt(locked_full['trade_hit_rate'], '.2%')}  "
        f"n_trades={locked_full['n_trades_taken']}"
    )

    # ─ Step 5: leave-one-month-out on locked full-window trades ─────────
    _print_section("Step 5 — leave-one-month-out (locked winner, full window)")
    locked_full_trades = _load_trades(conn, LOCKED_RUN_ID)
    loo = _leave_one_month_out(locked_full_trades)
    _print_loo(loo)

    # ─ Step 6: monthly distribution ─────────────────────────────────────
    _print_section("Step 6 — monthly portfolio-return distribution (locked, full)")
    locked_full_sim = _simulate_on_trades(
        locked_full_trades, **PORTFOLIO_SIM_KWARGS
    )
    monthly_dist = _monthly_distribution(locked_full_sim["equity_series"])
    _print_monthly_dist(monthly_dist)

    # ─ Step 7: BTC regime + per-regime metrics ──────────────────────────
    _print_section("Step 7 — BTC monthly regime + per-regime metrics")
    regimes = _btc_regime_map(conn, FULL_START, FULL_END)
    regime_results = _regime_conditional_metrics(locked_full_trades, regimes)
    _print_regimes(regime_results)

    # ─ Verdict ──────────────────────────────────────────────────────────
    _print_section("Verdict")
    locked_oos_sharpe = locked_test["portfolio_sharpe"]
    oos_refit_oos_sharpe = winner_oos_test["portfolio_sharpe"]
    verdict_locked = _verdict_for_oos_sharpe(locked_oos_sharpe)
    verdict_oos_refit = _verdict_for_oos_sharpe(oos_refit_oos_sharpe)

    def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None or b == 0:
            return None
        return float(a) / float(b)

    degradation = {
        "locked_oos_over_train": _ratio(
            locked_oos_sharpe, locked_train["portfolio_sharpe"]
        ),
        "locked_oos_over_full": _ratio(
            locked_oos_sharpe, locked_full["portfolio_sharpe"]
        ),
        "oos_refit_oos_over_train": _ratio(
            oos_refit_oos_sharpe, winner_oos_train["portfolio_sharpe"]
        ),
    }
    print(f"  LOCKED Phase 1B winner OOS Sharpe = {_fmt(locked_oos_sharpe)}")
    print(f"  -> {verdict_locked}")
    print()
    print(f"  OOS-REFIT winner OOS Sharpe       = {_fmt(oos_refit_oos_sharpe)}")
    print(f"  -> {verdict_oos_refit}")
    print()
    print(f"  Degradation ratios:")
    print(f"    locked  OOS/train = {_fmt(degradation['locked_oos_over_train'])}")
    print(f"    locked  OOS/full  = {_fmt(degradation['locked_oos_over_full'])}")
    print(f"    refit   OOS/train = {_fmt(degradation['oos_refit_oos_over_train'])}")
    print()
    print("  Methodology caveat: test window is ~97 days / ~85 trades. A single "
          "split is a noisy estimator of the true OOS Sharpe.")

    # ─ Assemble final results ───────────────────────────────────────────
    results = {
        "methodology": {
            "full_start": FULL_START,
            "full_end": FULL_END,
            "train_start": TRAIN_START,
            "train_end": TRAIN_END,
            "test_start": TEST_START,
            "test_end": TEST_END,
            "gates": {
                "annualized_return_min": GATE_ANN_RET_MIN,
                "sharpe_min": GATE_SHARPE_MIN,
                "max_drawdown_min": GATE_MAX_DD_MIN,
                "profit_factor_min": GATE_PF_MIN,
            },
            "portfolio_sim_kwargs": PORTFOLIO_SIM_KWARGS,
            "regime_thresholds": {
                "bull_min": REGIME_BULL_THRESHOLD,
                "bear_max": REGIME_BEAR_THRESHOLD,
            },
            "loo_collapse_threshold_ratio": LOO_COLLAPSE_THRESHOLD,
        },
        "locked_winner": {
            "config": _cfg_to_dict(LOCKED_CFG),
            "full_run_id": LOCKED_RUN_ID,
            "train_run_id": locked_train_rid,
            "test_run_id": locked_test_rid,
            "full": locked_full,
            "train": locked_train,
            "test": locked_test,
        },
        "oos_refit_winner": {
            "winner_cfg": winner_cfg,
            "winner_run_id": winner_rid,
            "test_run_id": winner_oos_test_rid,
            "all_gates_passed_in_train": all_passed,
            "train": winner_oos_train,
            "test": winner_oos_test,
        },
        "train_grid_candidates": all_metrics,
        "top3_train_base_run_ids": top3,
        "loo": loo,
        "monthly_distribution": monthly_dist,
        "regimes": regime_results,
        "degradation": degradation,
        "verdict": {
            "locked": verdict_locked,
            "oos_refit": verdict_oos_refit,
            "headline_locked_oos_sharpe": locked_oos_sharpe,
            "headline_oos_refit_oos_sharpe": oos_refit_oos_sharpe,
        },
    }

    # Strip non-serializable pandas objects (equity_series held in
    # locked_full_sim is not in `results` — but be defensive).
    clean = _strip_series_for_json(results)

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
