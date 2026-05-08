"""Phase 1B reports — ranking table, per-run detail, simulated $1000 portfolio.

Per ``crypto/execution/backtest/SPEC.md`` § "Reporting" and § "Simulated
portfolio projection". Reads from ``crypto_backtest_runs`` /
``crypto_backtest_trades`` / ``crypto_backtest_summary``; never writes
to the DB.

Public API
----------

* :func:`generate_ranking_table(conn, sort_by, limit) -> str` — Markdown
  ranking table for the leaderboard.
* :func:`generate_run_detail(conn, run_id) -> str` — config, metrics
  with methodology disclaimer, exit + cost breakdown, monthly P&L.
* :class:`PortfolioResult` — output of :func:`simulate_portfolio`.
* :func:`simulate_portfolio(conn, run_id, *, starting_capital,
  max_positions, deploy_fraction, leverage) -> PortfolioResult` —
  rotates a fixed bankroll through trades with a concurrent-position
  cap, producing realistic absolute Sharpe / drawdown / annualized
  return that the sum-of-fractions metrics cannot.
* :func:`generate_top_n_detail(conn, n) -> str` — bundles ranking row +
  run detail + portfolio projection for the top ``n`` runs.

Decision-criteria evaluation
----------------------------

:meth:`PortfolioResult.evaluate_decision_criteria` returns
``{"annualized_return", "sharpe", "max_drawdown", "profit_factor"} ->
("pass" | "fail" | nan, value)`` against the spec's Phase 1B gates:

    annualized > 5 %, Sharpe > 1.0, max drawdown < 25 %,
    profit factor > 1.3.

These are the **realistic** numbers — sum-of-fractions inflation is
gone because the portfolio simulation sizes each position out of a
fixed $1000 bankroll.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import duckdb
import numpy as np
import pandas as pd

logger = logging.getLogger("mhde.crypto.backtest.report")


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────


VALID_SORT_COLUMNS: frozenset[str] = frozenset({
    "sharpe_ratio",
    "net_pnl_total_pct",
    "net_pnl_annualized_pct",
    "max_drawdown_pct",
    "hit_rate",
    "profit_factor",
})

DECISION_CRITERIA = {
    "annualized_return": ("> 5%",   lambda v: v is not None and v > 0.05),
    "sharpe":            ("> 1.0",  lambda v: v is not None and v > 1.0),
    "max_drawdown":      ("< 25%",  lambda v: v is not None and v > -0.25),
    "profit_factor":     ("> 1.3",  lambda v: v is not None and v > 1.3),
}

SHARPE_PERIODS_PER_YEAR = 252
ANNUALIZATION_DAYS_PER_YEAR = 365.0


# ──────────────────────────────────────────────────────────────────────
# Ranking table
# ──────────────────────────────────────────────────────────────────────


def generate_ranking_table(
    conn: duckdb.DuckDBPyConnection,
    sort_by: str = "sharpe_ratio",
    limit: int = 20,
) -> str:
    """Markdown ranking table joining runs and summary.

    ``sort_by`` is whitelisted against :data:`VALID_SORT_COLUMNS`. The
    table renders rank, truncated run_id, horizon, policy, selection,
    Sharpe, max drawdown, annualized P&L (% — note: sum-of-fractions
    inflation), profit factor, hit rate, and trade count.
    """
    if sort_by not in VALID_SORT_COLUMNS:
        raise ValueError(
            f"sort_by must be one of {sorted(VALID_SORT_COLUMNS)}; "
            f"got {sort_by!r}"
        )
    rows = conn.execute(
        f"""
        SELECT s.run_id, r.horizon, r.exit_policy, r.selection_rule,
               s.sharpe_ratio, s.max_drawdown_pct,
               s.net_pnl_annualized_pct, s.profit_factor, s.hit_rate,
               r.n_trades
        FROM crypto_backtest_summary s
        JOIN crypto_backtest_runs r USING (run_id)
        WHERE s.run_id LIKE 'backtest_%'
        ORDER BY s.{sort_by} DESC NULLS LAST
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    lines = [
        f"### Ranking (top {min(limit, len(rows))} by `{sort_by}`)",
        "",
        "| # | run_id | horizon | policy | selection | Sharpe | "
        "MaxDD | AnnPnL | PF | Hit | Trades |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for i, (rid, hz, pol, sel, sh, dd, ann, pf, hr, n) in enumerate(rows, 1):
        sh_s   = f"{sh:.3f}" if sh is not None else "—"
        dd_s   = f"{dd*100:+.2f}%" if dd is not None else "—"
        ann_s  = f"{ann*100:+.2f}%" if ann is not None else "—"
        pf_s   = ("inf"      if pf == float("inf")
                  else f"{pf:.2f}" if pf is not None else "—")
        hr_s   = f"{hr*100:.1f}%" if hr is not None else "—"
        lines.append(
            f"| {i} | `{rid}` | {hz} | {pol} | {sel} | "
            f"{sh_s} | {dd_s} | {ann_s} | {pf_s} | {hr_s} | "
            f"{int(n):,} |"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Per-run detail
# ──────────────────────────────────────────────────────────────────────


_METHODOLOGY_DISCLAIMER = (
    "Note: Sharpe / drawdown / annualized return below come from the "
    "sum-of-fractions daily P&L methodology (see SPEC.md "
    "\"Metrics methodology\"). Absolute values are inflated relative to "
    "a true portfolio simulation. Ranking is preserved across configs "
    "because the methodology is consistent. See the simulated portfolio "
    "block for realistic absolutes."
)


def generate_run_detail(
    conn: duckdb.DuckDBPyConnection, run_id: str,
) -> str:
    """Per-run detail report for one ``run_id``.

    Sections: config, summary metrics + disclaimer, exit-reason
    breakdown, cost breakdown, per-month P&L."""
    run = conn.execute(
        "SELECT horizon, exit_policy, selection_rule, parameters, "
        "       date_start, date_end, n_predictions_seen, n_trades, "
        "       n_skipped_duplicates, n_skipped_missing_atr, "
        "       n_data_gap_exits, n_forward_fills, "
        "       n_excluded_by_funding_floor, n_missing_funding_warnings "
        "FROM crypto_backtest_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if run is None:
        raise ValueError(f"run_id {run_id!r} not found")
    (horizon, policy, selection, params_json, date_start, date_end,
     n_pred, n_trades, n_dup, n_atr, n_gap, n_ff, n_floor, n_warn) = run

    summary = conn.execute(
        """
        SELECT net_pnl_total_pct, net_pnl_annualized_pct, sharpe_ratio,
               max_drawdown_pct, hit_rate, avg_winner_pct, avg_loser_pct,
               profit_factor, avg_holding_days,
               pct_exits_tp, pct_exits_sl, pct_exits_trailing,
               pct_exits_time, pct_exits_data_gap,
               total_fees_paid_pct, total_funding_paid_pct,
               total_slippage_paid_pct
        FROM crypto_backtest_summary WHERE run_id = ?
        """,
        [run_id],
    ).fetchone()

    lines: list[str] = []
    lines.append(f"## Run detail — `{run_id}`")
    lines.append("")
    lines.append("### Configuration")
    lines.append("")
    lines.append(f"- horizon: **{horizon}**")
    lines.append(f"- policy: **{policy}**")
    lines.append(f"- selection: **{selection}**")
    lines.append(f"- parameters: `{params_json}`")
    lines.append(f"- prediction-date range: {date_start} → {date_end}")
    lines.append(f"- predictions seen: {int(n_pred):,}")
    lines.append(f"- trades opened: {int(n_trades):,}")
    lines.append(f"- skipped (duplicate): {int(n_dup):,}")
    lines.append(f"- skipped (missing ATR): {int(n_atr):,}")
    lines.append(f"- data-gap exits: {int(n_gap):,}")
    lines.append(f"- forward-fills: {int(n_ff):,}")
    lines.append(f"- excluded by funding floor: {int(n_floor):,}")
    lines.append(f"- missing-funding warnings: {int(n_warn):,}")

    lines.append("")
    lines.append("### Summary metrics (sum-of-fractions methodology)")
    lines.append("")
    lines.append(f"> {_METHODOLOGY_DISCLAIMER}")
    lines.append("")
    if summary is None:
        lines.append("_(no summary row — re-run `compute_and_persist_summary`)_")
    else:
        (total, ann, sharpe, dd, hr, aw, al, pf, ah,
         pe_tp, pe_sl, pe_tr, pe_ti, pe_dg,
         tot_fee, tot_fund, tot_slip) = summary
        def f(v, frac_pct=False, sign=False, suffix="", fallback="—"):
            if v is None:
                return fallback
            try:
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    if math.isinf(v):
                        return "inf" if v > 0 else "-inf"
                    return fallback
                if frac_pct:
                    fmt = "{:+.2f}%" if sign else "{:.2f}%"
                    return fmt.format(v * 100)
                fmt = "{:+.4f}" if sign else "{:.4f}"
                return fmt.format(v) + suffix
            except Exception:
                return fallback
        lines.append(f"- net P&L total: **{f(total, sign=True)}**")
        lines.append(f"- net P&L annualized: **{f(ann, frac_pct=True, sign=True)}**")
        lines.append(f"- Sharpe: **{f(sharpe)}**")
        lines.append(f"- max drawdown: **{f(dd, frac_pct=True, sign=True)}**")
        lines.append(f"- hit rate: **{f(hr, frac_pct=True)}**")
        lines.append(f"- avg winner: {f(aw, frac_pct=True, sign=True)}; "
                     f"avg loser: {f(al, frac_pct=True, sign=True)}")
        lines.append(f"- profit factor: **{f(pf)}**")
        lines.append(f"- avg holding days: {f(ah)}")

        lines.append("")
        lines.append("### Exit-reason breakdown")
        lines.append("")
        lines.append("| reason | % of trades |")
        lines.append("|---|---:|")
        for label, val in [
            ("tp", pe_tp), ("sl", pe_sl), ("trailing", pe_tr),
            ("time", pe_ti), ("data_gap", pe_dg),
        ]:
            lines.append(f"| {label} | {f(val, frac_pct=True)} |")

        lines.append("")
        lines.append("### Cost breakdown (sum across trades; positive = paid)")
        lines.append("")
        lines.append("| component | total | per-trade avg |")
        lines.append("|---|---:|---:|")
        if int(n_trades) > 0:
            avg_fee = tot_fee / n_trades
            avg_fund = tot_fund / n_trades
            avg_slip = tot_slip / n_trades
        else:
            avg_fee = avg_fund = avg_slip = 0.0
        lines.append(f"| fees | {f(tot_fee, sign=True)} | {f(avg_fee, sign=True)} |")
        lines.append(f"| slippage | {f(tot_slip, sign=True)} | {f(avg_slip, sign=True)} |")
        lines.append(f"| funding | {f(tot_fund, sign=True)} | {f(avg_fund, sign=True)} |")

    # Per-month P&L (uses trade-level data)
    monthly = conn.execute(
        """
        SELECT
            STRFTIME(exit_date, '%Y-%m') AS yyyymm,
            COUNT(*) AS n_trades,
            SUM(gross_pnl_pct) AS gross_total,
            SUM(fee_pct + slippage_pct + funding_pct) AS cost_total,
            SUM(net_pnl_pct) AS net_total
        FROM crypto_backtest_trades
        WHERE run_id = ? AND exit_date IS NOT NULL
        GROUP BY yyyymm
        ORDER BY yyyymm
        """,
        [run_id],
    ).fetchall()
    if monthly:
        lines.append("")
        lines.append("### Per-month P&L (sum-of-fractions, trade-level)")
        lines.append("")
        lines.append("| month | trades | gross | costs | net |")
        lines.append("|---|---:|---:|---:|---:|")
        for ym, n, gross, costs, net in monthly:
            lines.append(
                f"| {ym} | {int(n):,} | {gross:+.4f} | {costs:+.4f} "
                f"| {net:+.4f} |"
            )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Simulated portfolio
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PortfolioResult:
    """Output of :func:`simulate_portfolio` for one run."""

    run_id: str
    starting_capital: float
    deploy_fraction: float
    max_positions: int
    leverage: float

    final_equity: float
    n_trades_taken: int
    n_trades_skipped_capacity: int

    total_return_pct: float        # (final - start) / start
    annualized_return_pct: float
    sharpe_ratio: float            # from daily portfolio returns
    max_drawdown_pct: float
    max_drawdown_dollars: float
    best_month_dollars: float
    worst_month_dollars: float
    n_months_in_drawdown: int
    profit_factor: float

    span_days: int
    equity_curve: pd.DataFrame = field(repr=False)
    trade_log: pd.DataFrame = field(repr=False)

    def evaluate_decision_criteria(self) -> dict[str, tuple[str, float, bool]]:
        """For each Phase 1B decision criterion: ``(rule, value, passed)``."""
        checks = {
            "annualized_return": (
                "> 5%", self.annualized_return_pct,
                DECISION_CRITERIA["annualized_return"][1](self.annualized_return_pct),
            ),
            "sharpe": (
                "> 1.0", self.sharpe_ratio,
                DECISION_CRITERIA["sharpe"][1](self.sharpe_ratio),
            ),
            "max_drawdown": (
                "< 25%", self.max_drawdown_pct,
                DECISION_CRITERIA["max_drawdown"][1](self.max_drawdown_pct),
            ),
            "profit_factor": (
                "> 1.3", self.profit_factor,
                DECISION_CRITERIA["profit_factor"][1](self.profit_factor),
            ),
        }
        return checks

    @property
    def passes_all_criteria(self) -> bool:
        return all(passed for _, _, passed in self.evaluate_decision_criteria().values())


def _coerce_to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        return value.to_pydatetime().date()
    if isinstance(value, date):
        return value
    return None


def simulate_portfolio(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    starting_capital: float = 1000.0,
    max_positions: int = 6,
    deploy_fraction: float = 0.8,
    leverage: float = 1.0,
) -> PortfolioResult:
    """Simulate a fixed-bankroll portfolio rotating through trades.

    Logic per SPEC.md "Simulated portfolio projection":
      - Per-position notional at trade entry =
        ``current_equity * deploy_fraction * leverage / max_positions``.
      - Trades sized off **current** equity → compounding.
      - Hard cap at ``max_positions`` concurrent positions.
        Excess trades on any given entry-date are dropped.
      - Equity changes only when a position closes:
        ``equity += position_notional * trade.net_pnl_pct``.

    Args:
        conn: DuckDB connection.
        run_id: a run already persisted to ``crypto_backtest_trades``.
        starting_capital: dollars at t=0.
        max_positions: concurrent-trade cap.
        deploy_fraction: fraction of equity deployed across positions.
        leverage: multiplier on per-position notional. 1.0 = baseline.
    """
    if max_positions <= 0:
        raise ValueError(f"max_positions must be positive; got {max_positions}")
    if not 0.0 < deploy_fraction <= 1.0:
        raise ValueError(
            f"deploy_fraction must be in (0, 1]; got {deploy_fraction}"
        )
    if leverage <= 0:
        raise ValueError(f"leverage must be positive; got {leverage}")

    trades = conn.execute(
        """
        SELECT trade_id, coin, entry_date, exit_date, net_pnl_pct
        FROM crypto_backtest_trades
        WHERE run_id = ?
          AND entry_date IS NOT NULL
          AND exit_date IS NOT NULL
          AND net_pnl_pct IS NOT NULL
        ORDER BY entry_date, trade_id
        """,
        [run_id],
    ).fetchdf()

    equity = float(starting_capital)
    open_positions: list[dict[str, Any]] = []
    closes: list[dict[str, Any]] = []   # (date, pnl_dollars, trade_id)
    n_taken = 0
    n_skipped = 0

    if trades.empty:
        return PortfolioResult(
            run_id=run_id,
            starting_capital=starting_capital,
            deploy_fraction=deploy_fraction,
            max_positions=max_positions,
            leverage=leverage,
            final_equity=starting_capital,
            n_trades_taken=0, n_trades_skipped_capacity=0,
            total_return_pct=0.0, annualized_return_pct=0.0,
            sharpe_ratio=float("nan"),
            max_drawdown_pct=0.0, max_drawdown_dollars=0.0,
            best_month_dollars=0.0, worst_month_dollars=0.0,
            n_months_in_drawdown=0, profit_factor=float("nan"),
            span_days=0,
            equity_curve=pd.DataFrame(columns=["date", "equity"]),
            trade_log=pd.DataFrame(),
        )

    # Walk the trades, sequencing closes that happen STRICTLY BEFORE
    # each new entry (same-day close + open is conservatively treated as
    # the slot still occupied, since an exit price is a same-day event).
    for t in trades.itertuples(index=False):
        entry_d = _coerce_to_date(t.entry_date)
        exit_d = _coerce_to_date(t.exit_date)
        net = float(t.net_pnl_pct)

        # Close prior positions whose exit_date is strictly before today's
        # entry. Sort to be deterministic when several share an exit_date.
        still_open: list[dict[str, Any]] = []
        for pos in sorted(open_positions, key=lambda p: (p["exit_date"], p["trade_id"])):
            if pos["exit_date"] < entry_d:
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
            "trade_id": t.trade_id, "coin": t.coin,
            "entry_date": entry_d, "exit_date": exit_d,
            "size": size, "net_pnl_pct": net,
        })
        n_taken += 1

    # Close remaining positions in exit-date order.
    for pos in sorted(open_positions, key=lambda p: (p["exit_date"], p["trade_id"])):
        pnl = pos["size"] * pos["net_pnl_pct"]
        equity += pnl
        closes.append({
            "date": pos["exit_date"], "pnl_dollars": pnl,
            "trade_id": pos["trade_id"],
        })

    # Daily equity curve = starting_capital + cumulative dollar PnL by close date.
    closes_df = pd.DataFrame(closes)
    if closes_df.empty:
        equity_curve = pd.DataFrame(
            [{"date": _coerce_to_date(trades["entry_date"].min()),
              "equity": starting_capital}]
        )
    else:
        daily_pnl = (
            closes_df.groupby("date", sort=True)["pnl_dollars"].sum()
        )
        cumulative = starting_capital + daily_pnl.cumsum()
        # Insert a starting-row at one day before the first close so the curve
        # starts at starting_capital.
        first_date = daily_pnl.index.min()
        prelude = pd.Series(
            {first_date - pd.Timedelta(days=1): starting_capital}
        ).rename_axis("date")
        equity_series = pd.concat([prelude, cumulative]).sort_index()
        equity_curve = pd.DataFrame({
            "date": equity_series.index,
            "equity": equity_series.values,
        })

    final_equity = float(equity_curve["equity"].iloc[-1])

    # Span (days) — first trade entry → last trade exit (or final equity date).
    first_entry = _coerce_to_date(trades["entry_date"].min())
    last_exit = _coerce_to_date(trades["exit_date"].max())
    span_days = max(0, (last_exit - first_entry).days) if first_entry and last_exit else 0

    total_return_pct = (final_equity - starting_capital) / starting_capital
    if span_days > 0:
        annualized = total_return_pct * (
            ANNUALIZATION_DAYS_PER_YEAR / float(span_days)
        )
    else:
        annualized = 0.0

    # Sharpe: daily portfolio returns from equity curve.
    if len(equity_curve) >= 2:
        eq = equity_curve["equity"].values
        # Forward-fill is implicit in the cumulative series but for Sharpe we
        # want only event-day returns to match metrics.py event-day Sharpe.
        # Use pct_change on the equity curve (ignoring NaN at the prelude).
        rets = pd.Series(eq).pct_change().dropna()
        if len(rets) >= 2 and rets.std(ddof=1) > 0:
            sharpe = float(
                rets.mean() / rets.std(ddof=1) * (SHARPE_PERIODS_PER_YEAR ** 0.5)
            )
        else:
            sharpe = float("nan")
    else:
        sharpe = float("nan")

    # Max drawdown on the equity curve.
    eq_series = equity_curve["equity"].astype(float)
    peak = eq_series.cummax()
    dd_pct_series = (eq_series - peak) / peak
    dd_dollar_series = eq_series - peak
    max_dd_pct = float(dd_pct_series.min()) if len(dd_pct_series) else 0.0
    max_dd_dol = float(dd_dollar_series.min()) if len(dd_dollar_series) else 0.0

    # Months in drawdown — count distinct year-month pairs with dd_pct < 0.
    if not equity_curve.empty:
        in_dd = dd_pct_series < 0
        months_in_dd = (
            pd.to_datetime(equity_curve.loc[in_dd, "date"])
            .dt.to_period("M").nunique()
        )
    else:
        months_in_dd = 0

    # Best / worst month from closes_df (event-driven dollar P&L).
    if not closes_df.empty:
        m = (
            closes_df.assign(
                month=pd.to_datetime(closes_df["date"]).dt.to_period("M")
            )
            .groupby("month")["pnl_dollars"].sum()
        )
        best_month = float(m.max()) if not m.empty else 0.0
        worst_month = float(m.min()) if not m.empty else 0.0
    else:
        best_month = worst_month = 0.0

    # Profit factor in dollar terms.
    if not closes_df.empty:
        winners = closes_df.loc[closes_df["pnl_dollars"] > 0, "pnl_dollars"].sum()
        losers = closes_df.loc[closes_df["pnl_dollars"] <= 0, "pnl_dollars"].sum()
        if losers == 0 and winners > 0:
            pf = float("inf")
        elif winners == 0 and losers == 0:
            pf = float("nan")
        else:
            pf = float(winners / abs(losers)) if losers != 0 else float("nan")
    else:
        pf = float("nan")

    trade_log = pd.DataFrame(closes)

    return PortfolioResult(
        run_id=run_id,
        starting_capital=starting_capital,
        deploy_fraction=deploy_fraction,
        max_positions=max_positions,
        leverage=leverage,
        final_equity=final_equity,
        n_trades_taken=n_taken,
        n_trades_skipped_capacity=n_skipped,
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_dollars=max_dd_dol,
        best_month_dollars=best_month,
        worst_month_dollars=worst_month,
        n_months_in_drawdown=int(months_in_dd),
        profit_factor=pf,
        span_days=span_days,
        equity_curve=equity_curve,
        trade_log=trade_log,
    )


def format_portfolio_result(result: PortfolioResult) -> str:
    """Markdown summary of one :class:`PortfolioResult` + decision-criteria."""
    lines = []
    lines.append(
        f"### Simulated portfolio (${result.starting_capital:,.0f} start, "
        f"{int(result.deploy_fraction*100)}% deployed across "
        f"{result.max_positions} concurrent, {result.leverage:.0f}× leverage)"
    )
    lines.append("")
    lines.append(f"- final equity: **${result.final_equity:,.2f}**")
    lines.append(
        f"- total return: **{result.total_return_pct*100:+.2f}%** "
        f"over {result.span_days} days → "
        f"annualized **{result.annualized_return_pct*100:+.2f}%**"
    )
    sharpe_s = (
        f"{result.sharpe_ratio:.3f}"
        if not math.isnan(result.sharpe_ratio) else "—"
    )
    lines.append(f"- Sharpe (daily portfolio returns): **{sharpe_s}**")
    lines.append(
        f"- max drawdown: **{result.max_drawdown_pct*100:+.2f}%** "
        f"(${result.max_drawdown_dollars:+,.2f})"
    )
    lines.append(
        f"- best month: ${result.best_month_dollars:+,.2f}; "
        f"worst month: ${result.worst_month_dollars:+,.2f}"
    )
    lines.append(f"- months in drawdown: {result.n_months_in_drawdown}")
    pf_s = (
        "inf" if result.profit_factor == float("inf")
        else (f"{result.profit_factor:.2f}"
              if not math.isnan(result.profit_factor) else "—")
    )
    lines.append(f"- profit factor: **{pf_s}**")
    lines.append(
        f"- trades taken: {result.n_trades_taken:,}; "
        f"skipped at cap: {result.n_trades_skipped_capacity:,}"
    )

    lines.append("")
    lines.append("**Decision criteria (realistic-portfolio):**")
    lines.append("")
    lines.append("| criterion | rule | value | pass? |")
    lines.append("|---|---|---:|---:|")
    checks = result.evaluate_decision_criteria()
    name_map = {
        "annualized_return": "annualized return",
        "sharpe":            "Sharpe ratio",
        "max_drawdown":      "max drawdown",
        "profit_factor":     "profit factor",
    }
    for k, (rule, val, ok) in checks.items():
        if k in {"annualized_return", "max_drawdown"}:
            val_s = (f"{val*100:+.2f}%" if val is not None and not math.isnan(val)
                     else "—")
        elif k == "profit_factor":
            val_s = ("inf" if val == float("inf")
                     else f"{val:.2f}" if val is not None and not math.isnan(val)
                     else "—")
        else:
            val_s = (f"{val:.3f}" if val is not None and not math.isnan(val)
                     else "—")
        lines.append(
            f"| {name_map[k]} | {rule} | {val_s} | "
            f"{'✅' if ok else '❌'} |"
        )
    if result.passes_all_criteria:
        overall = "**Overall: PASSES all four criteria.**"
    else:
        n_pass = sum(1 for _, _, ok in checks.values() if ok)
        n_total = len(checks)
        failed = [
            name_map[k] for k, (_, _, ok) in checks.items() if not ok
        ]
        overall = (
            f"**Overall: {n_pass}/{n_total} criteria passed; "
            f"failed on {', '.join(failed)}.**"
        )
    lines.append("")
    lines.append(overall)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Top-N detail bundle
# ──────────────────────────────────────────────────────────────────────


def generate_top_n_detail(
    conn: duckdb.DuckDBPyConnection,
    n: int = 3,
    *,
    sort_by: str = "sharpe_ratio",
) -> str:
    """For each of the top ``n`` runs by ``sort_by``: ranking row +
    full run detail + simulated portfolio with decision criteria."""
    if sort_by not in VALID_SORT_COLUMNS:
        raise ValueError(
            f"sort_by must be one of {sorted(VALID_SORT_COLUMNS)}; "
            f"got {sort_by!r}"
        )
    top_ids = [
        r[0] for r in conn.execute(
            f"""
            SELECT s.run_id
            FROM crypto_backtest_summary s
            JOIN crypto_backtest_runs r USING (run_id)
            WHERE s.run_id LIKE 'backtest_%'
            ORDER BY s.{sort_by} DESC NULLS LAST
            LIMIT ?
            """,
            [n],
        ).fetchall()
    ]
    if not top_ids:
        return f"_(no runs found in crypto_backtest_summary)_"

    sections: list[str] = [
        f"# Phase 1B top-{len(top_ids)} detail",
        "",
        f"_Sorted by `{sort_by}`._",
        "",
        "---",
    ]
    for run_id in top_ids:
        sections.append(generate_run_detail(conn, run_id))
        sections.append("")
        portfolio = simulate_portfolio(conn, run_id)
        sections.append(format_portfolio_result(portfolio))
        sections.append("")
        sections.append("---")
    return "\n".join(sections)
