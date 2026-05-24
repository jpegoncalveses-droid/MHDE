"""Tests for crypto/execution/backtest/harness.py.

Step 2 covers:
  - Date-floor enforcement (4 tests)
  - Lifecycle helpers (_walk_position_forward, _gross_pnl_pct,
    _build_position) — 7 tests
  - Duplicate-position guard via end-to-end run_backtest — 1 test

The wider Step 4 suite (deterministic run_id round-trip, run isolation
across two parallel backtests, broader integration) lands later.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from crypto.execution.backtest.harness import (
    DATA_GAP_EXIT_DAYS,
    FORWARD_FILL_MAX_DAYS,
    MIN_FUNDING_DATA_DATE,
    Position,
    SkippedPrediction,
    _build_position,
    _gross_pnl_pct,
    _walk_position_forward,
    count_predictions_below_funding_floor,
    load_oos_predictions,
    run_backtest,
)
from crypto.execution.backtest.policies import (
    ExitEvent,
    ExitPolicy,
    build_policy,
)


def _seed_walkfold_pair(conn, horizon: str = "5d") -> None:
    """Insert one walkfold model_run + two predictions straddling the
    funding-data floor (one excluded, one included)."""
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs
            (model_id, horizon, target_threshold,
             train_start, train_end, test_start, test_end, is_active)
        VALUES (?, ?, 0.10,
                '2024-01-01', '2025-04-03',
                '2025-04-04', '2025-04-30', false)
        """,
        [f"crypto_{horizon}_walkfold_2025_04", horizon],
    )
    # Day-before-floor (2025-04-04) and floor day (2025-04-05).
    for d in [date(2025, 4, 4), date(2025, 4, 5)]:
        conn.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold, market_cap_bucket)
            VALUES (?, ?, ?, ?, ?, 0.10, 'unknown')
            """,
            ["BTCUSDT", d, f"crypto_{horizon}_walkfold_2025_04", horizon, 0.7],
        )


def _as_date(value) -> date:
    """DuckDB DATE round-trips as pandas.Timestamp via fetchdf; coerce
    to a plain Python date so equality checks stay readable."""
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    return value


def test_load_oos_predictions_respects_funding_floor(temp_db):
    """Prediction with date 2025-04-04 (one day before floor) must be
    excluded; prediction with date 2025-04-05 (floor day) included."""
    _seed_walkfold_pair(temp_db, "5d")
    df = load_oos_predictions(temp_db, "5d")
    assert len(df) == 1, f"expected 1 row, got {len(df)}: {df}"
    assert _as_date(df.iloc[0]["date"]) == MIN_FUNDING_DATA_DATE
    assert df.iloc[0]["coin"] == "BTCUSDT"


def test_load_oos_predictions_floor_can_be_bypassed_for_testing(temp_db):
    """``apply_funding_floor=False`` lets tests inspect the full set."""
    _seed_walkfold_pair(temp_db, "5d")
    df = load_oos_predictions(temp_db, "5d", apply_funding_floor=False)
    assert len(df) == 2
    assert sorted([_as_date(d) for d in df["date"]]) == [
        date(2025, 4, 4), date(2025, 4, 5)
    ]


def test_count_predictions_below_funding_floor_returns_excluded_count(temp_db):
    """The harness logs this count; verify it matches the row excluded."""
    _seed_walkfold_pair(temp_db, "5d")
    n = count_predictions_below_funding_floor(temp_db, "5d")
    assert n == 1


def test_min_funding_data_date_is_2025_04_05():
    """The floor must be the calibrated value from the spec, not drift."""
    assert MIN_FUNDING_DATA_DATE == date(2025, 4, 5)


# ──────────────────────────────────────────────────────────────────────
# Lifecycle helpers — _walk_position_forward / _gross_pnl_pct / _build_position
# ──────────────────────────────────────────────────────────────────────


def _make_position(entry_price: float = 100.0, horizon_days: int = 5,
                    policy: ExitPolicy | None = None,
                    coin: str = "BTC",
                    entry_date: date = date(2025, 4, 6)) -> Position:
    if policy is None:
        policy = build_policy("A", entry_price=entry_price,
                               horizon_days=horizon_days,
                               params={"tp_pct": 0.05})
    return Position(
        trade_id="t1", coin=coin, entry_date=entry_date,
        entry_price=entry_price, horizon=f"{horizon_days}d",
        policy=policy, probability_at_entry=0.7,
    )


def test_walk_forward_contiguous_path_tp_fires():
    """Policy A TP at +5%; bar 2 has high=106 → TP fires at exit_price=105."""
    pos = _make_position()
    ohlcv = {
        ("BTC", date(2025, 4, 7)): (100.0, 102.0,  99.0, 101.0),  # day 1: no fire
        ("BTC", date(2025, 4, 8)): (101.0, 106.0, 100.0, 104.0),  # day 2: high=106 → TP
        ("BTC", date(2025, 4, 9)): (104.0, 105.0, 103.0, 104.0),
        ("BTC", date(2025, 4, 10)): (104.0, 105.0, 103.0, 104.0),
        ("BTC", date(2025, 4, 11)): (104.0, 105.0, 103.0, 104.0),
    }
    data_gap, n_ff = _walk_position_forward(
        pos, horizon_days=5, ohlcv_by_key=ohlcv,
    )
    assert data_gap is False
    assert n_ff == 0
    assert pos.policy.is_complete
    assert len(pos.exits) == 1
    assert pos.exits[0].reason == "tp"
    assert pos.exits[0].exit_price == pytest.approx(105.0)
    assert pos.exit_date == date(2025, 4, 8)


def test_walk_forward_two_day_gap_forward_fills_then_exits_normally():
    """2 missing days mid-window → forward-fill, position survives, time stop fires."""
    # High TP keeps the policy quiet so the time stop is what closes the trade.
    policy = build_policy("A", entry_price=100.0, horizon_days=5,
                           params={"tp_pct": 0.50})
    pos = _make_position(policy=policy)
    ohlcv = {
        ("BTC", date(2025, 4, 7)): (100.0, 102.0, 99.0, 101.0),  # day 1
        # day 2 (4-08) missing — forward-fill
        # day 3 (4-09) missing — forward-fill
        ("BTC", date(2025, 4, 10)): (101.0, 102.0, 100.0, 101.5),  # day 4
        ("BTC", date(2025, 4, 11)): (101.5, 103.0, 100.5, 102.0),  # day 5: time stop
    }
    data_gap, n_ff = _walk_position_forward(
        pos, horizon_days=5, ohlcv_by_key=ohlcv,
    )
    assert data_gap is False
    assert n_ff == 2
    assert pos.forward_fill_days == 2
    assert pos.exits[-1].reason == "time"
    assert pos.exit_date == date(2025, 4, 11)


def test_walk_forward_three_day_gap_emits_data_gap_exit():
    """3 consecutive missing days → exit at last known close before the gap."""
    pos = _make_position()
    # Day 1 has a real bar; days 2/3/4 are missing → exit on day 4 detection.
    ohlcv = {
        ("BTC", date(2025, 4, 7)): (100.0, 101.0, 99.0, 101.0),  # day 1: last real
        # days 2, 3, 4 missing
        ("BTC", date(2025, 4, 11)): (100.0, 101.0, 99.0, 100.0),  # day 5 — never reached
    }
    data_gap, n_ff = _walk_position_forward(
        pos, horizon_days=5, ohlcv_by_key=ohlcv,
    )
    assert data_gap is True
    assert pos.exits[-1].reason == "data_gap"
    assert pos.exits[-1].exit_price == pytest.approx(101.0)
    assert pos.exit_date == date(2025, 4, 7)
    # The first 2 missing days were forward-filled before the third triggered exit.
    assert pos.forward_fill_days == FORWARD_FILL_MAX_DAYS


def test_walk_forward_defensive_fallback_fires_when_policy_never_completes():
    """A pathological policy that never emits + never completes still
    triggers the harness's defensive time stop."""

    class _NeverFires(ExitPolicy):
        def __init__(self):
            super().__init__(entry_price=100.0, horizon_days=5)

        def step(self, day_idx, high, low, close, open_=None):
            return []   # never emits

    pos = Position(
        trade_id="t1", coin="BTC", entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="5d", policy=_NeverFires(),
        probability_at_entry=0.7,
    )
    ohlcv = {
        ("BTC", date(2025, 4, 7)): (100.0, 100.0, 100.0, 100.0),
        ("BTC", date(2025, 4, 8)): (100.0, 100.0, 100.0, 100.0),
        ("BTC", date(2025, 4, 9)): (100.0, 100.0, 100.0, 100.0),
        ("BTC", date(2025, 4, 10)): (100.0, 100.0, 100.0, 100.0),
        ("BTC", date(2025, 4, 11)): (100.0, 100.0, 100.0, 100.0),
    }
    data_gap, _ = _walk_position_forward(pos, horizon_days=5, ohlcv_by_key=ohlcv)
    assert data_gap is False
    # Defensive fallback emitted a time-stop event.
    assert len(pos.exits) == 1
    assert pos.exits[0].reason == "time"
    assert pos.exit_date == date(2025, 4, 11)


def test_gross_pnl_pct_policy_e_partial_fill_weighted_average():
    """Policy E partial fill: 50% at +5%, 50% at +10% → weighted +7.5%."""
    pos = _make_position(
        policy=build_policy("E", entry_price=100.0, horizon_days=10,
                              params={"tp_pct": 0.05, "tp_fraction": 0.5,
                                      "trail_pct": 0.5}),
        horizon_days=10,
    )
    pos.exits = [
        ExitEvent(exit_price=105.0, fraction=0.5, reason="tp"),
        ExitEvent(exit_price=110.0, fraction=0.5, reason="trailing"),
    ]
    assert _gross_pnl_pct(pos) == pytest.approx(0.075)


def test_gross_pnl_pct_full_exit_single_event():
    """Single full-position exit: gross P&L = (exit/entry - 1)."""
    pos = _make_position()
    pos.exits = [ExitEvent(exit_price=110.0, fraction=1.0, reason="time")]
    assert _gross_pnl_pct(pos) == pytest.approx(0.10)


def test_build_position_policy_c_skips_when_atr_missing():
    """Policy C requires atr_pct; missing → SkippedPrediction reason='missing_atr'."""
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5),
        entry_date=date(2025, 4, 6), entry_price=100.0,
        horizon="5d", horizon_days=5, exit_policy_id="C",
        policy_params={}, probability=0.7,
        atr_lookup={},   # ← empty = missing
        trade_id="t1",
    )
    assert pos is None
    assert reason == "missing_atr"


def test_build_position_policy_c_succeeds_when_atr_present():
    """Sanity counterpart: ATR present → position constructed."""
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5),
        entry_date=date(2025, 4, 6), entry_price=100.0,
        horizon="5d", horizon_days=5, exit_policy_id="C",
        policy_params={}, probability=0.7,
        atr_lookup={("BTC", date(2025, 4, 5)): 0.02},
        trade_id="t1",
    )
    assert pos is not None
    assert reason is None
    # Policy C's stop sits at entry × (1 - 2 × 0.02) = 96.0
    from crypto.execution.backtest.policies import FixedTpAtrSl
    assert isinstance(pos.policy, FixedTpAtrSl)
    assert pos.policy.sl_price == pytest.approx(96.0)


# ──────────────────────────────────────────────────────────────────────
# Duplicate-position guard via end-to-end run_backtest
# ──────────────────────────────────────────────────────────────────────


def _seed_walkfold_run_for_dup_test(conn) -> None:
    """One 5d walkfold model_run + 3 BTC predictions on 2025-04-05,
    2025-04-07, 2025-04-13. With Policy A and horizon 5d, the first
    trade runs from 2025-04-06 to 2025-04-11 (tp/time stop somewhere
    in the window). The 04-07 prediction's entry would be 04-08 (inside
    that hold) → must be skipped. The 04-13 prediction's entry is
    04-14 (after exit) → must be allowed."""
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs
            (model_id, horizon, target_threshold,
             train_start, train_end, test_start, test_end, is_active)
        VALUES ('crypto_5d_walkfold_2025_04', '5d', 0.10,
                '2024-01-01', '2025-04-04',
                '2025-04-05', '2025-04-30', false)
        """
    )
    for d in [date(2025, 4, 5), date(2025, 4, 7), date(2025, 4, 13)]:
        conn.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold, market_cap_bucket)
            VALUES ('BTCUSDT', ?, 'crypto_5d_walkfold_2025_04', '5d',
                    0.7, 0.10, 'unknown')
            """,
            [d],
        )

    # Quiet flat price path so neither TP nor SL fires — every trade
    # closes via time stop on day 5.
    for offset in range(0, 25):
        d = date(2025, 4, 5) + timedelta(days=offset)
        conn.execute(
            """
            INSERT INTO crypto_prices_daily
                (symbol, trade_date, open, high, low, close,
                 volume, trades, taker_buy_volume, source)
            VALUES ('BTCUSDT', ?, 100.0, 100.5, 99.5, 100.0,
                    1000.0, 1, 100.0, 'test')
            """,
            [d],
        )


def test_run_backtest_skips_overlapping_signal_for_same_coin(temp_db):
    """A second prediction on coin X whose entry lands inside the still-open
    first trade is skipped with reason='duplicate_open_position', and the
    counter increments. A later prediction whose entry is AFTER the first
    exit opens normally."""
    _seed_walkfold_run_for_dup_test(temp_db)
    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
        dry_run=True,
    )
    # 3 predictions, 2 trades opened (04-05, 04-13), 1 skipped as duplicate.
    assert state.n_predictions_seen == 3
    assert len(state.closed_trades) == 2
    assert state.n_skipped_duplicates == 1
    assert any(s.reason == "duplicate_open_position" for s in state.skipped)
    skipped = [s for s in state.skipped
               if s.reason == "duplicate_open_position"]
    assert len(skipped) == 1
    assert skipped[0].coin == "BTCUSDT"
    assert skipped[0].date == date(2025, 4, 7)


def test_run_backtest_does_not_skip_when_prior_trade_already_exited(temp_db):
    """Sanity counterpart: the 04-13 prediction's entry (04-14) is AFTER
    the first trade's exit (~04-11) → opens normally, no skip."""
    _seed_walkfold_run_for_dup_test(temp_db)
    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
        dry_run=True,
    )
    # The 04-13 prediction must have produced a trade.
    trades_from_apr_13 = [
        t for t in state.closed_trades
        if t.entry_date == date(2025, 4, 14)
    ]
    assert len(trades_from_apr_13) == 1


# ──────────────────────────────────────────────────────────────────────
# Persistence + idempotency
# ──────────────────────────────────────────────────────────────────────


def _table_count(conn, table: str, run_id: str | None = None) -> int:
    if run_id is None:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", [run_id]
    ).fetchone()[0])


def test_persistence_writes_one_run_row_and_n_trade_rows(temp_db):
    _seed_walkfold_run_for_dup_test(temp_db)
    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
    )
    assert _table_count(temp_db, "crypto_backtest_runs", state.run_id) == 1
    assert _table_count(
        temp_db, "crypto_backtest_trades", state.run_id
    ) == len(state.closed_trades)

    # Spot-check a few persisted columns.
    row = temp_db.execute(
        "SELECT horizon, exit_policy, selection_rule, n_predictions_seen, "
        "n_trades, n_skipped_duplicates "
        "FROM crypto_backtest_runs WHERE run_id = ?",
        [state.run_id],
    ).fetchone()
    assert row[0] == "5d"
    assert row[1] == "A"
    assert row[2] == "top_n"
    assert row[3] == state.n_predictions_seen
    assert row[4] == len(state.closed_trades)
    assert row[5] == state.n_skipped_duplicates


def test_persistence_rollback_on_trades_insert_failure(temp_db):
    """Inject a duplicate trade_id mid-batch so the second INSERT into
    crypto_backtest_trades violates the (run_id, trade_id) PK and triggers
    ROLLBACK. After the failure both tables must be empty for the run_id."""
    from crypto.execution.backtest.costs import TradeCosts
    from crypto.execution.backtest.harness import (
        Position, RunState, _persist_run, ensure_backtest_tables,
    )
    from crypto.execution.backtest.policies import build_policy

    ensure_backtest_tables(temp_db)
    run_id = "backtest_5d_A_top_n_DUPLICATE"
    state = RunState(
        run_id=run_id, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", parameters={},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 30),
    )
    state.n_predictions_seen = 2

    pol = build_policy("A", entry_price=100.0, horizon_days=5,
                        params={"tp_pct": 0.05})
    p = Position(
        trade_id="DUP", coin="BTC", entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="5d", policy=pol,
        probability_at_entry=0.7,
    )
    p.exit_date = date(2025, 4, 10)
    p.exit_price = 105.0
    p.exit_reason = "tp"
    p.costs = TradeCosts(
        entry_fee=0.0002, exit_fee=0.0005,
        entry_slippage=0.0010, exit_slippage=0.0010, funding=0.0,
    )

    # Two Position objects with the SAME trade_id — second INSERT will
    # collide on the (run_id, trade_id) PK.
    state.closed_trades = [p, p]

    pre_runs = _table_count(temp_db, "crypto_backtest_runs")
    pre_trades = _table_count(temp_db, "crypto_backtest_trades")

    with pytest.raises(Exception):
        _persist_run(temp_db, state, force=False)

    # ROLLBACK leaves both tables exactly as they were before the call.
    assert _table_count(temp_db, "crypto_backtest_runs") == pre_runs
    assert _table_count(temp_db, "crypto_backtest_trades") == pre_trades
    assert _table_count(temp_db, "crypto_backtest_runs", run_id) == 0
    assert _table_count(temp_db, "crypto_backtest_trades", run_id) == 0


def test_run_backtest_collision_without_force_raises(temp_db):
    _seed_walkfold_run_for_dup_test(temp_db)
    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
    )
    # Re-running the same configuration must collide on the deterministic run_id.
    with pytest.raises(RuntimeError, match="already exists"):
        run_backtest(
            temp_db, horizon="5d", exit_policy_id="A",
            selection_rule="top_n", selection_params={"n": 1},
            date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
            force=False,
        )
    # Pre-existing rows are intact (not lost to the failed re-run).
    assert _table_count(temp_db, "crypto_backtest_runs", state.run_id) == 1
    assert _table_count(
        temp_db, "crypto_backtest_trades", state.run_id
    ) == len(state.closed_trades)


def test_run_backtest_collision_with_force_overwrites(temp_db):
    _seed_walkfold_run_for_dup_test(temp_db)
    first = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
    )
    second = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
        force=True,
    )
    assert second.run_id == first.run_id
    # After the force-overwrite, exactly one runs row + N trades rows survive.
    assert _table_count(temp_db, "crypto_backtest_runs", first.run_id) == 1
    assert _table_count(
        temp_db, "crypto_backtest_trades", first.run_id
    ) == len(second.closed_trades)


def test_run_backtest_dry_run_writes_nothing(temp_db):
    _seed_walkfold_run_for_dup_test(temp_db)
    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 13),
        dry_run=True,
    )
    # State is fully populated (lifecycle ran), but no DB rows exist.
    assert len(state.closed_trades) > 0
    assert _table_count(temp_db, "crypto_backtest_runs", state.run_id) == 0
    assert _table_count(temp_db, "crypto_backtest_trades", state.run_id) == 0


def test_runs_row_has_populated_effective_dates_and_annualization_works(temp_db):
    """Even when the user passes no date_start / date_end, run_backtest
    must compute the effective prediction range from the loaded data
    and persist it into crypto_backtest_runs.{date_start,date_end} so
    metrics.compute_summary can produce a non-NaN net_pnl_annualized_pct."""
    import math

    from crypto.execution.backtest.metrics import compute_summary

    _seed_walkfold_run_for_dup_test(temp_db)

    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 1},
        # Note: deliberately no date_start / date_end — we want to
        # confirm the harness derives them from the data.
    )

    # State exposes the effective range.
    assert state.effective_date_start is not None
    assert state.effective_date_end is not None
    assert state.effective_date_start <= state.effective_date_end

    # And the runs row has populated date_start / date_end now.
    row = temp_db.execute(
        "SELECT date_start, date_end FROM crypto_backtest_runs "
        "WHERE run_id = ?",
        [state.run_id],
    ).fetchone()
    assert row is not None
    persisted_start, persisted_end = row
    assert persisted_start is not None
    assert persisted_end is not None

    # And metrics.compute_summary now produces a non-NaN annualized return.
    summary = compute_summary(temp_db, state.run_id)
    assert not math.isnan(summary.net_pnl_annualized_pct)
    # With 2 trades, sum of fractions × 365/span_days, the value should be
    # finite and have the same sign as the total return.
    assert math.isfinite(summary.net_pnl_annualized_pct)
    if summary.net_pnl_total_pct != 0:
        assert (summary.net_pnl_annualized_pct >= 0) == (
            summary.net_pnl_total_pct >= 0
        )


# ──────────────────────────────────────────────────────────────────────
# CLI argument validation
# ──────────────────────────────────────────────────────────────────────


def test_cli_rejects_bad_horizon():
    from click.testing import CliRunner
    from main import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["crypto", "backtest", "--horizon", "8d",
         "--policy", "A", "--selection", "top_n"],
    )
    assert result.exit_code != 0
    assert "Invalid value for '--horizon'" in result.output


def test_cli_rejects_bad_policy():
    from click.testing import CliRunner
    from main import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["crypto", "backtest", "--horizon", "5d",
         "--policy", "Z", "--selection", "top_n"],
    )
    assert result.exit_code != 0
    assert "Invalid value for '--policy'" in result.output


def test_cli_rejects_bad_selection():
    from click.testing import CliRunner
    from main import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["crypto", "backtest", "--horizon", "5d",
         "--policy", "A", "--selection", "random"],
    )
    assert result.exit_code != 0
    assert "Invalid value for '--selection'" in result.output


def test_cli_rejects_malformed_params_json():
    from click.testing import CliRunner
    from main import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["crypto", "backtest", "--horizon", "5d",
         "--policy", "A", "--selection", "top_n",
         "--params", "{not_valid_json"],
    )
    assert result.exit_code != 0
    assert "must be valid JSON" in result.output
