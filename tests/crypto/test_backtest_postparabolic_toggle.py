"""Tests for the post-parabolic filter toggle in the execution backtest
harness (``run_backtest(apply_postparabolic_filter=...)``).

The toggle defaults to ``False`` (baseline behaviour unchanged). When
``True`` it drops post-parabolic candidates from the daily prediction
batch *before* selection — exactly as the live export does — using
``crypto/ml/postparabolic_filter.should_exclude`` against the
candidate's ``drawdown_from_90d_high`` / ``return_60d`` from
``crypto_ml_features`` at the prediction date.
"""
from __future__ import annotations

from datetime import date, timedelta

from crypto.execution.backtest.harness import (
    load_dd90_ret60_at_entry,
    make_run_id,
    run_backtest,
)


def test_make_run_id_changes_when_filter_enabled():
    base = dict(horizon="10d", exit_policy_id="D", selection_rule="top_n",
                selection_params={"n": 6}, policy_params={"trail_pct": 0.3})
    off = make_run_id(**base)
    off_explicit = make_run_id(**base, apply_postparabolic_filter=False)
    on = make_run_id(**base, apply_postparabolic_filter=True)
    # Off (explicit) must equal omitting the kwarg — no run_id churn for
    # existing baseline runs.
    assert off == off_explicit
    # On must be a distinct run_id so paired A/B runs don't collide.
    assert on != off
    assert on.startswith("backtest_10d_D_top_n_")


def test_load_dd90_ret60_at_entry(temp_db):
    d = date(2025, 4, 5)
    # full-ish feature rows; only dd90 / ret60 matter here
    temp_db.execute(
        "INSERT INTO crypto_ml_features (symbol, trade_date, drawdown_from_90d_high, return_60d) "
        "VALUES ('AUSDT', ?, -0.05, 0.40)", [d])
    temp_db.execute(
        "INSERT INTO crypto_ml_features (symbol, trade_date, drawdown_from_90d_high, return_60d) "
        "VALUES ('BUSDT', ?, -0.30, 3.00)", [d])
    out = load_dd90_ret60_at_entry(temp_db, [("AUSDT", d), ("BUSDT", d), ("CUSDT", d)])
    assert out[("AUSDT", d)] == (-0.05, 0.40)
    assert out[("BUSDT", d)] == (-0.30, 3.00)
    assert ("CUSDT", d) not in out  # no feature row → absent


def _seed_two_coin_run(conn) -> None:
    """One 5d walkfold model + two predictions on 2025-04-05: AUSDT (clean
    features) and BUSDT (post-parabolic: dd90=-0.30, ret60=3.00). Flat
    prices so Policy A closes both via the time stop."""
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs
            (model_id, horizon, target_threshold, train_start, train_end,
             test_start, test_end, is_active)
        VALUES ('crypto_5d_walkfold_2025_04', '5d', 0.10,
                '2024-01-01', '2025-04-04', '2025-04-05', '2025-04-30', false)
        """
    )
    for sym, prob in [("AUSDT", 0.80), ("BUSDT", 0.95)]:
        conn.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold, market_cap_bucket)
            VALUES (?, '2025-04-05', 'crypto_5d_walkfold_2025_04', '5d',
                    ?, 0.10, 'unknown')
            """,
            [sym, prob],
        )
    conn.execute(
        "INSERT INTO crypto_ml_features (symbol, trade_date, drawdown_from_90d_high, return_60d) "
        "VALUES ('AUSDT', '2025-04-05', -0.04, 0.10)")
    conn.execute(
        "INSERT INTO crypto_ml_features (symbol, trade_date, drawdown_from_90d_high, return_60d) "
        "VALUES ('BUSDT', '2025-04-05', -0.30, 3.00)")
    for sym in ("AUSDT", "BUSDT"):
        for offset in range(0, 15):
            d = date(2025, 4, 5) + timedelta(days=offset)
            conn.execute(
                """
                INSERT INTO crypto_prices_daily
                    (symbol, trade_date, open, high, low, close,
                     volume, trades, taker_buy_volume, source)
                VALUES (?, ?, 100.0, 100.5, 99.5, 100.0, 1000.0, 1, 100.0, 'test')
                """,
                [sym, d],
            )


def test_run_backtest_filter_off_keeps_postparabolic_candidate(temp_db):
    _seed_two_coin_run(temp_db)
    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 2},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 5),
        dry_run=True,
    )
    assert state.apply_postparabolic_filter is False
    assert state.n_excluded_by_postparabolic == 0
    assert {t.coin for t in state.closed_trades} == {"AUSDT", "BUSDT"}


def test_run_backtest_filter_on_drops_postparabolic_candidate(temp_db):
    _seed_two_coin_run(temp_db)
    state = run_backtest(
        temp_db, horizon="5d", exit_policy_id="A",
        selection_rule="top_n", selection_params={"n": 2},
        date_start=date(2025, 4, 5), date_end=date(2025, 4, 5),
        apply_postparabolic_filter=True,
        dry_run=True,
    )
    assert state.apply_postparabolic_filter is True
    # BUSDT (dd90=-0.30, ret60=3.00) tripped the gate; AUSDT survives.
    assert state.n_excluded_by_postparabolic == 1
    assert {t.coin for t in state.closed_trades} == {"AUSDT"}
    # n_predictions_seen reflects the *pre-filter* universe (like the
    # funding-floor counter — the exclusion is reported separately).
    assert state.n_predictions_seen == 2
