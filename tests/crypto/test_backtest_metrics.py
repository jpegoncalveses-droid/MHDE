"""Tests for crypto/execution/backtest/metrics.py.

Covers all required cases from step-4 spec:
  - Trivial 1-trade case
  - Sharpe correctness on synthetic returns
  - Max drawdown on a synthetic equity curve
  - Profit factor correctness
  - Hit-rate boundary (0.0 = loser)
  - Empty trades — no division-by-zero
  - Persistence / idempotency on re-compute
  - Cost diagnostics aggregation

Plus light coverage of internal helpers and edge cases.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from crypto.execution.backtest.harness import ensure_backtest_tables
from crypto.execution.backtest.metrics import (
    EXIT_REASONS,
    SHARPE_PERIODS_PER_YEAR,
    SummaryRow,
    _build_daily_pnl,
    _max_drawdown_pct,
    _nan_to_none,
    _profit_factor,
    _sharpe_ratio,
    compute_and_persist_summary,
    compute_summary,
)


# ──────────────────────────────────────────────────────────────────────
# Test helpers — seed runs / trades rows directly into a temp_db
# ──────────────────────────────────────────────────────────────────────


def _setup(conn) -> None:
    """Create the backtest tables on a fresh temp_db."""
    ensure_backtest_tables(conn)


def _seed_run(
    conn,
    run_id: str = "rt-1",
    *,
    horizon: str = "5d",
    exit_policy: str = "A",
    selection_rule: str = "top_n",
    parameters: str = '{"n": 6}',
    date_start: date = date(2025, 4, 5),
    date_end: date = date(2025, 5, 5),
    n_predictions_seen: int = 100,
    n_trades: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO crypto_backtest_runs (
            run_id, horizon, exit_policy, selection_rule, parameters,
            date_start, date_end, n_predictions_seen, n_trades
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_id, horizon, exit_policy, selection_rule, parameters,
         date_start, date_end, n_predictions_seen, n_trades],
    )


def _seed_trade(
    conn, run_id: str, trade_id: str,
    *,
    coin: str = "BTCUSDT",
    entry_date: date = date(2025, 4, 6),
    entry_price: float = 100.0,
    exit_date: date | None = None,
    exit_price: float = 105.0,
    exit_reason: str = "tp",
    holding_days: int = 1,
    gross_pnl_pct: float = 0.05,
    fee_pct: float = 0.0007,
    slippage_pct: float = 0.0010,
    funding_pct: float = 0.0,
    net_pnl_pct: float | None = None,
    probability_at_entry: float = 0.7,
    forward_fill_days: int = 0,
) -> None:
    if exit_date is None:
        exit_date = entry_date  # safe default for tests
    if net_pnl_pct is None:
        net_pnl_pct = gross_pnl_pct - (fee_pct + slippage_pct + funding_pct)
    conn.execute(
        """
        INSERT INTO crypto_backtest_trades (
            run_id, trade_id, coin, entry_date, entry_price,
            exit_date, exit_price, exit_reason, holding_days,
            gross_pnl_pct, fee_pct, slippage_pct, funding_pct,
            net_pnl_pct, probability_at_entry, forward_fill_days
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_id, trade_id, coin, entry_date, entry_price,
         exit_date, exit_price, exit_reason, holding_days,
         gross_pnl_pct, fee_pct, slippage_pct, funding_pct,
         net_pnl_pct, probability_at_entry, forward_fill_days],
    )


# ──────────────────────────────────────────────────────────────────────
# Internal helpers — pure-function tests
# ──────────────────────────────────────────────────────────────────────


def test_max_drawdown_correctness_synthetic_curve():
    """Equity curve [100, 110, 120, 90, 100, 130]: peak=120, trough=90,
    drawdown = (90-120)/120 = -0.25."""
    equity = pd.Series([100.0, 110.0, 120.0, 90.0, 100.0, 130.0])
    assert _max_drawdown_pct(equity) == pytest.approx(-0.25)


def test_max_drawdown_zero_when_monotonically_up():
    equity = pd.Series([100.0, 110.0, 120.0, 130.0])
    assert _max_drawdown_pct(equity) == pytest.approx(0.0)


def test_max_drawdown_zero_when_empty():
    assert _max_drawdown_pct(pd.Series([], dtype=float)) == 0.0


def test_sharpe_correctness_synthetic_returns():
    """Compute via numpy directly so the test pins the formula exactly."""
    returns = pd.Series([0.01, 0.02, -0.01, 0.005, 0.015])
    expected = float(
        returns.mean() / returns.std(ddof=1)
        * np.sqrt(SHARPE_PERIODS_PER_YEAR)
    )
    assert _sharpe_ratio(returns) == pytest.approx(expected)


def test_sharpe_returns_nan_on_constant_returns():
    """Zero std → NaN, no division-by-zero."""
    constant = pd.Series([0.01, 0.01, 0.01, 0.01])
    assert math.isnan(_sharpe_ratio(constant))


def test_sharpe_returns_nan_on_too_few_observations():
    assert math.isnan(_sharpe_ratio(pd.Series([0.01])))
    assert math.isnan(_sharpe_ratio(pd.Series([], dtype=float)))


def test_profit_factor_basic():
    """Three winners summing to +0.10, two losers summing to -0.04 → 2.5."""
    assert _profit_factor(0.10, -0.04) == pytest.approx(2.5)


def test_profit_factor_no_losers_returns_inf():
    assert _profit_factor(0.05, 0.0) == float("inf")


def test_profit_factor_no_trades_returns_nan():
    assert math.isnan(_profit_factor(0.0, 0.0))


# ──────────────────────────────────────────────────────────────────────
# compute_summary — end-to-end with seeded DB
# ──────────────────────────────────────────────────────────────────────


def test_trivial_one_trade(temp_db):
    _setup(temp_db)
    _seed_run(temp_db)
    _seed_trade(
        temp_db, "rt-1", "t1",
        entry_date=date(2025, 4, 6), exit_date=date(2025, 4, 8),
        entry_price=100.0, exit_price=105.0, exit_reason="tp",
        gross_pnl_pct=0.05, fee_pct=0.0007, slippage_pct=0.0020,
        funding_pct=0.0, holding_days=2,
    )
    s = compute_summary(temp_db, "rt-1")
    # net_pnl = gross - costs = 0.05 - (0.0007 + 0.0020 + 0) = 0.0473
    assert s.net_pnl_total_pct == pytest.approx(0.0473)
    # Single trade → std undefined → NaN sharpe.
    assert math.isnan(s.sharpe_ratio)
    # Single positive trade → hit rate = 1.0, no losers → profit_factor inf
    assert s.hit_rate == pytest.approx(1.0)
    assert s.profit_factor == float("inf")
    assert s.avg_winner_pct == pytest.approx(0.0473)
    assert math.isnan(s.avg_loser_pct)
    # Single trade with no drawdown observed → 0.
    assert s.max_drawdown_pct == pytest.approx(0.0)
    # Exit breakdown
    assert s.pct_exits_tp == pytest.approx(1.0)
    for r in ("sl", "trailing", "time", "data_gap"):
        assert getattr(s, f"pct_exits_{r}") == 0.0
    # Costs
    assert s.total_fees_paid_pct == pytest.approx(0.0007)
    assert s.total_slippage_paid_pct == pytest.approx(0.0020)
    assert s.total_funding_paid_pct == pytest.approx(0.0)
    # Holding days
    assert s.avg_holding_days == pytest.approx(2.0)


def test_hit_rate_zero_counts_as_loser(temp_db):
    """Trade with net_pnl_pct == 0 must be classified as a loser."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-zero")
    _seed_trade(temp_db, "rt-zero", "w",
                exit_date=date(2025, 4, 7), gross_pnl_pct=0.06,
                fee_pct=0.0, slippage_pct=0.0, funding_pct=0.0,
                net_pnl_pct=0.06)
    _seed_trade(temp_db, "rt-zero", "z",
                exit_date=date(2025, 4, 8), gross_pnl_pct=0.0,
                fee_pct=0.0, slippage_pct=0.0, funding_pct=0.0,
                net_pnl_pct=0.0)   # exactly zero → counted as loser
    _seed_trade(temp_db, "rt-zero", "l",
                exit_date=date(2025, 4, 9), gross_pnl_pct=-0.04,
                fee_pct=0.0, slippage_pct=0.0, funding_pct=0.0,
                net_pnl_pct=-0.04)
    s = compute_summary(temp_db, "rt-zero")
    # 1 of 3 strictly > 0
    assert s.hit_rate == pytest.approx(1 / 3)
    # avg_loser averages the two non-positive trades: (0.0 + -0.04) / 2 = -0.02
    assert s.avg_loser_pct == pytest.approx(-0.02)


def test_empty_trades_no_division_by_zero(temp_db):
    """Run with zero trades → all-zero or NaN, no exception."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-empty")
    s = compute_summary(temp_db, "rt-empty")
    assert s.net_pnl_total_pct == 0.0
    assert math.isnan(s.net_pnl_annualized_pct)
    assert math.isnan(s.sharpe_ratio)
    assert s.max_drawdown_pct == 0.0
    assert math.isnan(s.hit_rate)
    assert math.isnan(s.avg_winner_pct)
    assert math.isnan(s.avg_loser_pct)
    assert math.isnan(s.profit_factor)
    assert math.isnan(s.avg_holding_days)
    for r in EXIT_REASONS:
        assert getattr(s, f"pct_exits_{r}") == 0.0
    assert s.total_fees_paid_pct == 0.0
    assert s.total_funding_paid_pct == 0.0
    assert s.total_slippage_paid_pct == 0.0


def test_profit_factor_correctness_via_compute_summary(temp_db):
    """Three winners summing to +0.10, two losers summing to -0.04 → 2.5,
    using the public compute_summary path on seeded trades."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-pf")
    pnls = [+0.04, +0.03, +0.03, -0.01, -0.03]   # sums: w=+0.10, l=-0.04
    for i, p in enumerate(pnls):
        _seed_trade(
            temp_db, "rt-pf", f"t{i}",
            entry_date=date(2025, 4, 6),
            exit_date=date(2025, 4, 7) + pd.Timedelta(days=i).to_pytimedelta(),
            gross_pnl_pct=p, fee_pct=0.0, slippage_pct=0.0, funding_pct=0.0,
            net_pnl_pct=p,
        )
    s = compute_summary(temp_db, "rt-pf")
    assert s.profit_factor == pytest.approx(2.5)


def test_cost_diagnostics_sum_correctly(temp_db):
    """Sum of fee / slippage / funding over all trades."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-costs")
    _seed_trade(temp_db, "rt-costs", "t1",
                exit_date=date(2025, 4, 7), gross_pnl_pct=0.05,
                fee_pct=0.001, slippage_pct=0.002, funding_pct=-0.0005)
    _seed_trade(temp_db, "rt-costs", "t2",
                exit_date=date(2025, 4, 8), gross_pnl_pct=0.03,
                fee_pct=0.0007, slippage_pct=0.0015, funding_pct=0.0003)
    s = compute_summary(temp_db, "rt-costs")
    assert s.total_fees_paid_pct == pytest.approx(0.0017)
    assert s.total_slippage_paid_pct == pytest.approx(0.0035)
    # Net funding = -0.0005 + 0.0003 = -0.0002 (long received net)
    assert s.total_funding_paid_pct == pytest.approx(-0.0002)


def test_annualization_uses_actual_run_span(temp_db):
    """net_pnl_annualized_pct = total * 365 / span_days."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-annual",
               date_start=date(2025, 4, 5), date_end=date(2025, 7, 5))   # 91 days
    _seed_trade(temp_db, "rt-annual", "t1",
                exit_date=date(2025, 5, 1), gross_pnl_pct=0.10,
                fee_pct=0.0, slippage_pct=0.0, funding_pct=0.0,
                net_pnl_pct=0.10)
    s = compute_summary(temp_db, "rt-annual")
    expected = 0.10 * (365.0 / 91.0)
    assert s.net_pnl_annualized_pct == pytest.approx(expected)


def test_compute_summary_raises_on_unknown_run(temp_db):
    _setup(temp_db)
    with pytest.raises(ValueError, match="not found"):
        compute_summary(temp_db, "no-such-run")


# ──────────────────────────────────────────────────────────────────────
# Persistence / idempotency
# ──────────────────────────────────────────────────────────────────────


def _summary_row_count(conn, run_id: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM crypto_backtest_summary WHERE run_id = ?",
        [run_id],
    ).fetchone()[0])


def test_compute_and_persist_summary_writes_one_row(temp_db):
    _setup(temp_db)
    _seed_run(temp_db, "rt-persist")
    _seed_trade(temp_db, "rt-persist", "t1",
                exit_date=date(2025, 4, 7), gross_pnl_pct=0.05,
                fee_pct=0.0007, slippage_pct=0.002, funding_pct=0.0)
    s = compute_and_persist_summary(temp_db, "rt-persist")
    assert _summary_row_count(temp_db, "rt-persist") == 1
    persisted = temp_db.execute(
        "SELECT net_pnl_total_pct, hit_rate, total_fees_paid_pct "
        "FROM crypto_backtest_summary WHERE run_id = ?",
        ["rt-persist"],
    ).fetchone()
    assert persisted[0] == pytest.approx(s.net_pnl_total_pct)
    assert persisted[1] == pytest.approx(s.hit_rate)
    assert persisted[2] == pytest.approx(s.total_fees_paid_pct)


def test_compute_and_persist_summary_is_idempotent(temp_db):
    """Re-running for the same run_id replaces the prior row, never duplicates."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-idem")
    _seed_trade(temp_db, "rt-idem", "t1",
                exit_date=date(2025, 4, 7), gross_pnl_pct=0.05,
                net_pnl_pct=0.05)
    compute_and_persist_summary(temp_db, "rt-idem")
    assert _summary_row_count(temp_db, "rt-idem") == 1

    # Add another trade so the metrics change.
    _seed_trade(temp_db, "rt-idem", "t2",
                exit_date=date(2025, 4, 8), gross_pnl_pct=-0.02,
                net_pnl_pct=-0.02)
    new = compute_and_persist_summary(temp_db, "rt-idem")
    assert _summary_row_count(temp_db, "rt-idem") == 1   # still exactly one row
    persisted_total = temp_db.execute(
        "SELECT net_pnl_total_pct FROM crypto_backtest_summary "
        "WHERE run_id = ?", ["rt-idem"],
    ).fetchone()[0]
    # Reflects the BOTH trades, not just the first.
    assert persisted_total == pytest.approx(new.net_pnl_total_pct)
    assert persisted_total == pytest.approx(0.05 + -0.02)


def test_persistence_nan_to_null(temp_db):
    """NaN metrics persist as SQL NULL, not NaN-as-DOUBLE."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-nan")
    # No trades → most metrics are NaN.
    compute_and_persist_summary(temp_db, "rt-nan")
    row = temp_db.execute(
        "SELECT sharpe_ratio, hit_rate, profit_factor, avg_winner_pct "
        "FROM crypto_backtest_summary WHERE run_id = ?",
        ["rt-nan"],
    ).fetchone()
    # All four were NaN in SummaryRow → must persist as NULL.
    assert all(v is None for v in row), row


def test_nan_to_none_helper():
    """Direct unit test on the NaN→None helper."""
    assert _nan_to_none(None) is None
    assert _nan_to_none(float("nan")) is None
    assert _nan_to_none(0.05) == 0.05
    # Inf is preserved (profit_factor = +inf when no losers).
    assert _nan_to_none(float("inf")) == float("inf")


# ──────────────────────────────────────────────────────────────────────
# _build_daily_pnl
# ──────────────────────────────────────────────────────────────────────


def test_build_daily_pnl_aggregates_same_day_trades():
    """Two trades exiting on the same day → their net_pnl_pct sum."""
    df = pd.DataFrame({
        "exit_date": [date(2025, 4, 6), date(2025, 4, 6), date(2025, 4, 7)],
        "net_pnl_pct": [0.01, 0.03, -0.02],
    })
    daily = _build_daily_pnl(df)
    assert daily.loc[date(2025, 4, 6)] == pytest.approx(0.04)
    assert daily.loc[date(2025, 4, 7)] == pytest.approx(-0.02)


def test_build_daily_pnl_drops_null_dates_or_pnls():
    df = pd.DataFrame({
        "exit_date":   [date(2025, 4, 6), None,            date(2025, 4, 7)],
        "net_pnl_pct": [0.01,             0.05,            None],
    })
    daily = _build_daily_pnl(df)
    assert len(daily) == 1
    assert daily.iloc[0] == pytest.approx(0.01)


def test_build_daily_pnl_empty_returns_empty():
    df = pd.DataFrame({"exit_date": [], "net_pnl_pct": []})
    daily = _build_daily_pnl(df)
    assert daily.empty
