"""Phase 1B post-run metrics — summarise one backtest run into a single row.

Reads from ``crypto_backtest_trades`` and ``crypto_backtest_runs`` for one
``run_id``, computes the metrics enumerated in
``crypto/execution/backtest/SPEC.md`` § "Output Schema —
crypto_backtest_summary", and writes/replaces the row in
``crypto_backtest_summary``.

Public API
----------

* :class:`SummaryRow` — frozen dataclass mirroring the summary table.
* :func:`compute_summary(conn, run_id) -> SummaryRow` — pure read; no writes.
* :func:`compute_and_persist_summary(conn, run_id) -> SummaryRow` — runs
  ``compute_summary`` and persists the row, replacing any existing row
  for the same ``run_id`` inside a single transaction.

Conventions / edge cases
------------------------

* All percentage-named columns store **fractions** (``0.05`` = 5 %), matching
  the convention already used by ``costs.py`` and ``crypto_backtest_trades``.
* **Daily Sharpe** is computed from a per-exit-date P&L series (sum of
  ``net_pnl_pct`` per exit day), with ``ddof=1`` standard deviation,
  annualized by ``sqrt(252)``. This is event-day Sharpe, not
  calendar-day Sharpe — days with zero exits are not in the series.
* **Max drawdown** is peak-to-trough on the cumulative-equity curve
  (``equity[t] = 1 + cumsum(daily_pnl)``), expressed as a signed
  fraction of peak equity. ``-0.25`` means 25 % drawdown; zero means no
  drawdown was observed.
* **Hit-rate boundary**: ``net_pnl_pct == 0`` counts as a **loser**
  (the rule is strict ``> 0`` for a winner).
* **Profit factor**: ``sum(winners) / abs(sum(losers))``. Returns
  ``+inf`` when there is at least one winner and no losers; ``NaN``
  when there are no trades at all.
* **Empty trades**: zero-trade run → all sums are 0, all means /
  ratios are ``NaN``; the run row is still written. No
  division-by-zero crash.
* **NaN → NULL on persistence.** ``+inf`` is preserved (it has a
  meaningful interpretation for ``profit_factor``).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from typing import Any, Optional

import duckdb
import numpy as np
import pandas as pd

from crypto.execution.backtest.harness import ensure_backtest_tables

logger = logging.getLogger("mhde.crypto.backtest.metrics")


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────


SHARPE_PERIODS_PER_YEAR = 252
ANNUALIZATION_DAYS_PER_YEAR = 365.0
EXIT_REASONS = ("tp", "sl", "trailing", "time", "data_gap")


# ──────────────────────────────────────────────────────────────────────
# SummaryRow dataclass — mirrors crypto_backtest_summary columns
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SummaryRow:
    """One row of computed metrics for a single backtest run."""

    run_id: str
    # Performance
    net_pnl_total_pct: float
    net_pnl_annualized_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    # Trade quality
    hit_rate: float
    avg_winner_pct: float
    avg_loser_pct: float
    profit_factor: float
    avg_holding_days: float
    # Exit-reason breakdown (each as fraction of total trades)
    pct_exits_tp: float
    pct_exits_sl: float
    pct_exits_trailing: float
    pct_exits_time: float
    pct_exits_data_gap: float
    # Cost diagnostics (signed fractions; sums across all trades)
    total_fees_paid_pct: float
    total_funding_paid_pct: float
    total_slippage_paid_pct: float


# ──────────────────────────────────────────────────────────────────────
# Internal helpers — pure functions, easy to unit-test
# ──────────────────────────────────────────────────────────────────────


def _build_daily_pnl(trades_df: pd.DataFrame) -> pd.Series:
    """Daily P&L series indexed by ``exit_date``.

    ``daily_pnl[d] = sum(net_pnl_pct)`` over trades whose ``exit_date == d``.
    Trades with NULL ``exit_date`` or NULL ``net_pnl_pct`` are dropped.
    """
    if trades_df.empty:
        return pd.Series(dtype=float, name="daily_pnl_pct")
    valid = trades_df.dropna(subset=["exit_date", "net_pnl_pct"])
    if valid.empty:
        return pd.Series(dtype=float, name="daily_pnl_pct")
    daily = (
        valid.groupby("exit_date", sort=True)["net_pnl_pct"].sum().astype(float)
    )
    daily.name = "daily_pnl_pct"
    return daily


def _max_drawdown_pct(equity: pd.Series) -> float:
    """Peak-to-trough drawdown as a signed fraction of peak equity.

    Returns ``0.0`` if the curve is empty or never declined; otherwise a
    negative value (e.g. ``-0.25`` for a 25 % drawdown). Peaks at zero
    are guarded against — no division by zero."""
    if len(equity) == 0:
        return 0.0
    peak = equity.cummax()
    safe_peak = peak.replace(0, np.nan)
    drawdown = (equity - peak) / safe_peak
    drawdown = drawdown.dropna()
    if drawdown.empty:
        return 0.0
    return float(drawdown.min())


def _sharpe_ratio(
    daily_returns: pd.Series,
    *, periods_per_year: int = SHARPE_PERIODS_PER_YEAR,
) -> float:
    """Annualized Sharpe ratio.

    ``mean(daily_returns) / std(daily_returns, ddof=1) * sqrt(N)``

    Returns ``NaN`` when fewer than 2 observations exist or when the
    standard deviation is zero (constant returns)."""
    if len(daily_returns) < 2:
        return float("nan")
    mean = float(daily_returns.mean())
    std = float(daily_returns.std(ddof=1))
    if std == 0.0 or np.isnan(std):
        return float("nan")
    return mean / std * float(periods_per_year ** 0.5)


def _profit_factor(winners_sum: float, losers_sum: float) -> float:
    """``sum(winners) / abs(sum(losers))``, with documented edge cases."""
    if winners_sum == 0.0 and losers_sum == 0.0:
        return float("nan")
    if losers_sum == 0.0:
        return float("inf") if winners_sum > 0 else float("nan")
    return winners_sum / abs(losers_sum)


# ──────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────


def _load_run_metadata(
    conn: duckdb.DuckDBPyConnection, run_id: str,
) -> tuple[Optional[date], Optional[date]]:
    row = conn.execute(
        "SELECT date_start, date_end FROM crypto_backtest_runs "
        "WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        raise ValueError(
            f"run_id {run_id!r} not found in crypto_backtest_runs"
        )
    return row[0], row[1]


def _load_trades(
    conn: duckdb.DuckDBPyConnection, run_id: str,
) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT exit_date, exit_reason, holding_days,
               net_pnl_pct, fee_pct, slippage_pct, funding_pct
        FROM crypto_backtest_trades
        WHERE run_id = ?
        """,
        [run_id],
    ).fetchdf()


# ──────────────────────────────────────────────────────────────────────
# Public — compute_summary
# ──────────────────────────────────────────────────────────────────────


def compute_summary(
    conn: duckdb.DuckDBPyConnection, run_id: str,
) -> SummaryRow:
    """Compute summary metrics for ``run_id`` from
    ``crypto_backtest_trades`` and ``crypto_backtest_runs``."""
    date_start, date_end = _load_run_metadata(conn, run_id)
    trades = _load_trades(conn, run_id)
    n_trades = len(trades)

    # ── Performance ───────────────────────────────────────────────────
    if n_trades == 0:
        net_pnl_total = 0.0
        net_pnl_annual = float("nan")
        sharpe = float("nan")
        max_dd = 0.0
    else:
        net_pnl_total = float(trades["net_pnl_pct"].sum())

        if date_start is not None and date_end is not None:
            ds = date_start.date() if isinstance(date_start, datetime) else date_start
            de = date_end.date() if isinstance(date_end, datetime) else date_end
            span_days = (de - ds).days
        else:
            span_days = 0
        if span_days > 0:
            net_pnl_annual = net_pnl_total * (
                ANNUALIZATION_DAYS_PER_YEAR / float(span_days)
            )
        else:
            net_pnl_annual = float("nan")

        daily_pnl = _build_daily_pnl(trades)
        sharpe = _sharpe_ratio(daily_pnl)
        equity = 1.0 + daily_pnl.cumsum()
        max_dd = _max_drawdown_pct(equity)

    # ── Trade quality ────────────────────────────────────────────────
    if n_trades == 0:
        hit_rate = float("nan")
        avg_winner = float("nan")
        avg_loser = float("nan")
        profit_factor = float("nan")
        avg_holding = float("nan")
    else:
        winners = trades[trades["net_pnl_pct"] > 0]
        losers = trades[trades["net_pnl_pct"] <= 0]
        hit_rate = float(len(winners)) / float(n_trades)
        avg_winner = (
            float(winners["net_pnl_pct"].mean()) if len(winners) > 0
            else float("nan")
        )
        avg_loser = (
            float(losers["net_pnl_pct"].mean()) if len(losers) > 0
            else float("nan")
        )
        sum_w = float(winners["net_pnl_pct"].sum()) if len(winners) > 0 else 0.0
        sum_l = float(losers["net_pnl_pct"].sum()) if len(losers) > 0 else 0.0
        profit_factor = _profit_factor(sum_w, sum_l)
        holding_clean = trades["holding_days"].dropna()
        avg_holding = (
            float(holding_clean.mean()) if not holding_clean.empty
            else float("nan")
        )

    # ── Exit-reason breakdown ────────────────────────────────────────
    pct_exits = {r: 0.0 for r in EXIT_REASONS}
    if n_trades > 0:
        counts = trades["exit_reason"].fillna("").value_counts()
        for r in EXIT_REASONS:
            pct_exits[r] = float(counts.get(r, 0)) / float(n_trades)

    # ── Cost diagnostics ─────────────────────────────────────────────
    total_fees = float(trades["fee_pct"].sum()) if n_trades > 0 else 0.0
    total_funding = float(trades["funding_pct"].sum()) if n_trades > 0 else 0.0
    total_slippage = float(trades["slippage_pct"].sum()) if n_trades > 0 else 0.0

    return SummaryRow(
        run_id=run_id,
        net_pnl_total_pct=net_pnl_total,
        net_pnl_annualized_pct=net_pnl_annual,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        hit_rate=hit_rate,
        avg_winner_pct=avg_winner,
        avg_loser_pct=avg_loser,
        profit_factor=profit_factor,
        avg_holding_days=avg_holding,
        pct_exits_tp=pct_exits["tp"],
        pct_exits_sl=pct_exits["sl"],
        pct_exits_trailing=pct_exits["trailing"],
        pct_exits_time=pct_exits["time"],
        pct_exits_data_gap=pct_exits["data_gap"],
        total_fees_paid_pct=total_fees,
        total_funding_paid_pct=total_funding,
        total_slippage_paid_pct=total_slippage,
    )


# ──────────────────────────────────────────────────────────────────────
# Public — compute_and_persist_summary
# ──────────────────────────────────────────────────────────────────────


def _nan_to_none(value: Any) -> Any:
    """Convert NaN to None for DuckDB INSERT (NULL). Preserve ±inf as-is —
    profit_factor uses ``+inf`` to mean "no losers, all winners"."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:   # NaN check
        return None
    return value


def compute_and_persist_summary(
    conn: duckdb.DuckDBPyConnection, run_id: str,
) -> SummaryRow:
    """Compute the summary and write/replace the row in
    ``crypto_backtest_summary``. Idempotent: re-running for the same
    ``run_id`` replaces the prior row inside a single transaction."""
    ensure_backtest_tables(conn)
    summary = compute_summary(conn, run_id)

    insert_columns = [f.name for f in fields(SummaryRow)]
    placeholders = ", ".join(["?"] * len(insert_columns))
    column_list = ", ".join(insert_columns)
    values = [_nan_to_none(getattr(summary, n)) for n in insert_columns]

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "DELETE FROM crypto_backtest_summary WHERE run_id = ?",
            [run_id],
        )
        conn.execute(
            f"INSERT INTO crypto_backtest_summary ({column_list}) "
            f"VALUES ({placeholders})",
            values,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    logger.info(
        "compute_and_persist_summary: run_id=%s n_trades=via DB sharpe=%.3f "
        "max_dd=%.3f hit_rate=%s pf=%s",
        run_id, summary.sharpe_ratio if summary.sharpe_ratio == summary.sharpe_ratio else float("nan"),
        summary.max_drawdown_pct,
        f"{summary.hit_rate:.3f}" if summary.hit_rate == summary.hit_rate else "nan",
        f"{summary.profit_factor:.2f}" if summary.profit_factor == summary.profit_factor else "nan",
    )
    return summary


def summary_as_dict(summary: SummaryRow) -> dict[str, Any]:
    """Convenience for callers who want a plain dict (e.g., for JSON / report)."""
    return asdict(summary)
