"""Tests for crypto/execution/backtest/report.py."""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from crypto.execution.backtest.harness import ensure_backtest_tables
from crypto.execution.backtest.metrics import compute_and_persist_summary
from crypto.execution.backtest.report import (
    PortfolioResult,
    VALID_SORT_COLUMNS,
    format_portfolio_result,
    generate_ranking_table,
    generate_run_detail,
    generate_top_n_detail,
    simulate_portfolio,
)


# ──────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────


def _setup(conn) -> None:
    ensure_backtest_tables(conn)


def _seed_run(
    conn, run_id: str,
    *,
    horizon: str = "5d", policy: str = "A", selection: str = "top_n",
    parameters: str = '{"selection_params": {"n": 6}, "policy_params": {}}',
    date_start: date = date(2025, 4, 5),
    date_end: date = date(2025, 5, 5),
) -> None:
    conn.execute(
        """
        INSERT INTO crypto_backtest_runs (
            run_id, horizon, exit_policy, selection_rule, parameters,
            date_start, date_end, n_predictions_seen, n_trades,
            n_skipped_duplicates, n_skipped_missing_atr,
            n_data_gap_exits, n_forward_fills,
            n_excluded_by_funding_floor, n_missing_funding_warnings
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 100, 0, 0, 0, 0, 0, 0, 0)
        """,
        [run_id, horizon, policy, selection, parameters, date_start, date_end],
    )


def _seed_trade(
    conn, run_id: str, trade_id: str,
    *,
    coin: str = "BTCUSDT",
    entry_date: date = date(2025, 4, 6),
    exit_date: date | None = None,
    entry_price: float = 100.0,
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
        exit_date = entry_date + timedelta(days=holding_days)
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
# generate_ranking_table
# ──────────────────────────────────────────────────────────────────────


def test_generate_ranking_table_orders_by_sharpe_desc(temp_db):
    _setup(temp_db)
    # Three runs with hand-set summary rows.
    for i, (rid, sharpe) in enumerate([
        ("backtest_5d_A_top_n_aaa1", 1.5),
        ("backtest_5d_B_top_n_bbb2", 0.5),
        ("backtest_5d_C_top_n_ccc3", 2.5),
    ]):
        _seed_run(temp_db, rid, policy=chr(ord("A") + i))
        temp_db.execute(
            """
            INSERT INTO crypto_backtest_summary (
                run_id, net_pnl_total_pct, net_pnl_annualized_pct,
                sharpe_ratio, max_drawdown_pct, hit_rate, avg_winner_pct,
                avg_loser_pct, profit_factor, avg_holding_days,
                pct_exits_tp, pct_exits_sl, pct_exits_trailing,
                pct_exits_time, pct_exits_data_gap,
                total_fees_paid_pct, total_funding_paid_pct,
                total_slippage_paid_pct
            ) VALUES (?, 0.10, 0.36, ?, -0.05, 0.6, 0.05, -0.02, 1.5,
                      3.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.001, 0.0, 0.002)
            """,
            [rid, sharpe],
        )

    text = generate_ranking_table(temp_db, sort_by="sharpe_ratio", limit=10)
    assert "ccc3" in text
    assert "aaa1" in text
    assert "bbb2" in text
    # ccc3 (sharpe=2.5) appears before aaa1 (1.5) which appears before bbb2 (0.5).
    pos_ccc3 = text.index("ccc3")
    pos_aaa1 = text.index("aaa1")
    pos_bbb2 = text.index("bbb2")
    assert pos_ccc3 < pos_aaa1 < pos_bbb2
    # Markdown header rendered.
    assert "| # |" in text
    assert "Sharpe" in text


def test_generate_ranking_table_rejects_unknown_sort_by(temp_db):
    _setup(temp_db)
    with pytest.raises(ValueError, match="sort_by"):
        generate_ranking_table(temp_db, sort_by="bogus_metric")


# ──────────────────────────────────────────────────────────────────────
# simulate_portfolio
# ──────────────────────────────────────────────────────────────────────


def test_simulate_portfolio_basic_correctness(temp_db):
    """One trade: $1000 start, 80% deploy, 6 max → first position size
    = $1000 × 0.8 / 6 = $133.33. Trade returns net +5% → +$6.67 → final
    equity $1006.67."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-basic")
    _seed_trade(
        temp_db, "rt-basic", "t1",
        entry_date=date(2025, 4, 6), exit_date=date(2025, 4, 11),
        net_pnl_pct=0.05,
    )
    pr = simulate_portfolio(
        temp_db, "rt-basic",
        starting_capital=1000.0, max_positions=6,
        deploy_fraction=0.8, leverage=1.0,
    )
    expected_size = 1000.0 * 0.8 / 6     # 133.33...
    expected_pnl = expected_size * 0.05  # 6.666...
    assert pr.final_equity == pytest.approx(1000.0 + expected_pnl, rel=1e-9)
    assert pr.n_trades_taken == 1
    assert pr.n_trades_skipped_capacity == 0


def test_simulate_portfolio_max_positions_cap(temp_db):
    """5 trades opening on the same day with max_positions=3 — only 3
    opened, 2 dropped."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-cap")
    for i in range(5):
        _seed_trade(
            temp_db, "rt-cap", f"t{i}",
            entry_date=date(2025, 4, 6),
            exit_date=date(2025, 4, 11),
            net_pnl_pct=0.05,
        )
    pr = simulate_portfolio(
        temp_db, "rt-cap",
        starting_capital=1000.0, max_positions=3,
        deploy_fraction=0.9, leverage=1.0,
    )
    assert pr.n_trades_taken == 3
    assert pr.n_trades_skipped_capacity == 2


def test_simulate_portfolio_compounding_grows_position_size(temp_db):
    """Sequential trades: after a winning trade, the next position is
    sized off the larger equity. Two non-overlapping trades; verify the
    P&L on the second is more than the P&L on the first because size
    has grown."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-comp")
    # Trade 1: enters Apr 6, exits Apr 8.
    _seed_trade(
        temp_db, "rt-comp", "t1",
        entry_date=date(2025, 4, 6), exit_date=date(2025, 4, 8),
        net_pnl_pct=0.10,
    )
    # Trade 2: enters Apr 9 (after t1 closes), exits Apr 11.
    _seed_trade(
        temp_db, "rt-comp", "t2",
        entry_date=date(2025, 4, 9), exit_date=date(2025, 4, 11),
        net_pnl_pct=0.10,
    )
    pr = simulate_portfolio(
        temp_db, "rt-comp",
        starting_capital=1000.0, max_positions=6,
        deploy_fraction=0.8, leverage=1.0,
    )
    # First trade size: 1000 × 0.8 / 6 = 133.33; PnL = +13.33; new equity = 1013.33.
    # Second trade size: 1013.33 × 0.8 / 6 = 135.11; PnL = +13.51 (larger).
    assert pr.n_trades_taken == 2
    # Final equity = 1000 + 13.33 + 13.51 = 1026.84... bigger than just 2 × 13.33.
    assert pr.final_equity > 1000 + 2 * (1000 * 0.8 / 6 * 0.10)


def test_simulate_portfolio_empty_trades_returns_starting_capital(temp_db):
    _setup(temp_db)
    _seed_run(temp_db, "rt-empty")
    pr = simulate_portfolio(temp_db, "rt-empty", starting_capital=1000.0)
    assert pr.final_equity == 1000.0
    assert pr.n_trades_taken == 0
    assert pr.n_trades_skipped_capacity == 0
    assert pr.total_return_pct == 0.0
    assert pr.annualized_return_pct == 0.0
    assert math.isnan(pr.sharpe_ratio)
    assert pr.max_drawdown_pct == 0.0


def test_simulate_portfolio_decision_criteria_evaluation(temp_db):
    """All four criteria evaluated correctly on a portfolio with known
    metrics."""
    _setup(temp_db)
    _seed_run(temp_db, "rt-criteria")
    # 10 winning trades → strong portfolio.
    for i in range(10):
        d = date(2025, 4, 6) + timedelta(days=i * 10)
        _seed_trade(
            temp_db, "rt-criteria", f"t{i}",
            entry_date=d, exit_date=d + timedelta(days=2),
            net_pnl_pct=0.10,
        )
    pr = simulate_portfolio(temp_db, "rt-criteria")
    checks = pr.evaluate_decision_criteria()
    assert set(checks.keys()) == {
        "annualized_return", "sharpe", "max_drawdown", "profit_factor"
    }
    for k, (rule, value, passed) in checks.items():
        assert isinstance(rule, str)
        assert isinstance(passed, bool)


# ──────────────────────────────────────────────────────────────────────
# generate_run_detail
# ──────────────────────────────────────────────────────────────────────


def test_generate_run_detail_produces_non_empty_output(temp_db):
    _setup(temp_db)
    _seed_run(temp_db, "rt-detail")
    _seed_trade(temp_db, "rt-detail", "t1", net_pnl_pct=0.05)
    compute_and_persist_summary(temp_db, "rt-detail")
    text = generate_run_detail(temp_db, "rt-detail")
    assert "rt-detail" in text
    assert "## Run detail" in text
    assert "### Configuration" in text
    assert "### Summary metrics" in text
    assert "### Exit-reason breakdown" in text
    assert "### Cost breakdown" in text
    assert "### Per-month P&L" in text
    # Methodology disclaimer present.
    assert "sum-of-fractions" in text


def test_generate_run_detail_raises_on_unknown_run(temp_db):
    _setup(temp_db)
    with pytest.raises(ValueError, match="not found"):
        generate_run_detail(temp_db, "no-such-run")


# ──────────────────────────────────────────────────────────────────────
# generate_top_n_detail
# ──────────────────────────────────────────────────────────────────────


def test_generate_top_n_detail_aggregates_n_runs(temp_db):
    _setup(temp_db)
    for i, sharpe in enumerate([1.5, 0.5, 2.5]):
        rid = f"backtest_5d_A_top_n_n{i}"
        _seed_run(temp_db, rid)
        _seed_trade(
            temp_db, rid, "t1",
            entry_date=date(2025, 4, 6), exit_date=date(2025, 4, 11),
            net_pnl_pct=0.05,
        )
        compute_and_persist_summary(temp_db, rid)
        # Override the sharpe column with a hand-picked value to control ordering.
        temp_db.execute(
            "UPDATE crypto_backtest_summary SET sharpe_ratio = ? "
            "WHERE run_id = ?",
            [sharpe, rid],
        )

    text = generate_top_n_detail(temp_db, n=2)
    # Only the 2 highest-Sharpe runs (n2 with 2.5, n0 with 1.5) appear.
    assert "backtest_5d_A_top_n_n2" in text
    assert "backtest_5d_A_top_n_n0" in text
    assert "backtest_5d_A_top_n_n1" not in text   # excluded (n=2)
    # Each block has a portfolio simulation render.
    assert text.count("### Simulated portfolio") == 2
    assert text.count("Decision criteria") == 2


# ──────────────────────────────────────────────────────────────────────
# format_portfolio_result
# ──────────────────────────────────────────────────────────────────────


def test_format_portfolio_result_renders_decision_block(temp_db):
    _setup(temp_db)
    _seed_run(temp_db, "rt-fmt")
    _seed_trade(temp_db, "rt-fmt", "t1", net_pnl_pct=0.10)
    pr = simulate_portfolio(temp_db, "rt-fmt")
    text = format_portfolio_result(pr)
    assert "Simulated portfolio" in text
    assert "final equity" in text
    assert "Decision criteria" in text
    assert "annualized return" in text
    assert "Sharpe ratio" in text
    assert "max drawdown" in text
    assert "profit factor" in text


# ──────────────────────────────────────────────────────────────────────
# generate_sensitivity_table
# ──────────────────────────────────────────────────────────────────────


def test_generate_sensitivity_table_orders_by_axis_value_and_marks_base(temp_db):
    """For a Policy D / threshold base, the threshold-axis sensitivity
    table renders one row per value in SENSITIVITY_THRESHOLD, ordered
    by the swept value, with the base row marked. Rows whose run_id
    isn't in crypto_backtest_summary show '—' placeholders rather
    than crashing."""
    from crypto.execution.backtest.harness import make_run_id
    from crypto.execution.backtest.report import generate_sensitivity_table
    from crypto.execution.backtest.runner import SENSITIVITY_THRESHOLD

    _setup(temp_db)
    base_id = make_run_id(
        horizon="5d", exit_policy_id="D", selection_rule="threshold",
        selection_params={"threshold": 0.55}, policy_params={},
    )
    _seed_run(
        temp_db, base_id,
        horizon="5d", policy="D", selection="threshold",
        parameters='{"selection_params": {"threshold": 0.55}, "policy_params": {}}',
    )
    # No trades / summary seeded — every row will be '—' but the
    # rendering must not crash and must still include the base marker.

    text = generate_sensitivity_table(temp_db, base_id, axis="selection")

    # One header line + 1 markdown table-divider line + 4 data rows.
    data_lines = [ln for ln in text.splitlines() if ln.startswith("|")
                  and not ln.startswith("|---")]
    # 1 header row + 4 sweep rows
    assert len(data_lines) == 1 + len(SENSITIVITY_THRESHOLD) == 5

    # Order check: thresholds rendered as p≥0.50, p≥0.55, p≥0.60, p≥0.65
    # (in the order the runner emits them).
    rendered_values = [
        ln.split("|")[1].strip().strip("*")  # strip bold markers
        for ln in data_lines[1:]
    ]
    assert rendered_values == ["p≥0.50", "p≥0.55", "p≥0.60", "p≥0.65"]

    # Base row is the one whose run_id matches base_id; it gets bolded
    # and an "← base" marker.
    assert "← base" in text
    assert "**p≥0.55**" in text  # bold marker on the base row's axis cell

    # Header references the axis and the base run_id.
    assert "Sensitivity along `selection`" in text
    assert base_id in text
