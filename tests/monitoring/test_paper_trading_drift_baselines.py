"""Strategy-baseline tests for the paper-trading drift monitor.

These tests live in a separate file from ``test_paper_trading_drift.py``
so they can exercise the real ``_latest_baseline_date`` resolver without
the autouse fixture that the main test file installs to keep its
existing scenarios independent of any ``config/monitoring.yaml``.

Scenarios:
    * multiple baselines configured → latest date wins
    * empty / unconfigured baselines → no exclusion (parity with legacy behavior)
    * pre-baseline closed trades are excluded from the win-rate denominator
    * post-baseline sample below ``MIN_CLOSED_FOR_HITRATE`` falls back to the
      insufficient-sample OK status (so a reset does not flap to red)
    * label hit rate respects the baseline on ``entry_date``
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb
import pytest

from monitoring import paper_trading_drift as ptd


NOW = datetime(2026, 5, 14, 9, 0, 0)  # post-baseline run-time


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


def _new_engine_db() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE engine_runs (
            id VARCHAR, phase VARCHAR, started_at TIMESTAMP,
            completed_at TIMESTAMP, success BOOLEAN, error_message VARCHAR
        )""")
    conn.execute("""
        CREATE TABLE positions (
            id VARCHAR, symbol VARCHAR, entry_date DATE, entry_price DOUBLE,
            qty DOUBLE, peak_price DOUBLE, current_state VARCHAR,
            horizon_expiry_date DATE, spec_version VARCHAR, spec_hash VARCHAR,
            created_at TIMESTAMP, updated_at TIMESTAMP
        )""")
    conn.execute("""
        CREATE TABLE orders (
            id VARCHAR, position_id VARCHAR, binance_order_id VARCHAR,
            client_order_id VARCHAR, order_type VARCHAR, side VARCHAR,
            price DOUBLE, qty DOUBLE, status VARCHAR,
            placed_at TIMESTAMP, filled_at TIMESTAMP
        )""")
    return conn


def _add_run(conn, phase, started_at, success=True):
    conn.execute(
        "INSERT INTO engine_runs (id, phase, started_at, success) VALUES (?,?,?,?)",
        [f"r-{phase}-{started_at.isoformat()}", phase, started_at, success],
    )


def _healthy_engine(conn, now=NOW):
    _add_run(conn, "monitor", now - timedelta(seconds=40))
    _add_run(conn, "entry", now.replace(hour=8, minute=2, second=0, microsecond=0))


def _add_closed_trade(conn, pid, symbol, entry_date, entry_price, qty,
                      sell_price, exit_ts):
    conn.execute(
        "INSERT INTO positions (id, symbol, entry_date, entry_price, qty, "
        "current_state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        [pid, symbol, entry_date, entry_price, qty, "exit_filled",
         exit_ts - timedelta(hours=3), exit_ts],
    )
    conn.execute(
        "INSERT INTO orders (id, position_id, order_type, side, price, qty, "
        "status, filled_at) VALUES (?,?,?,?,?,?,?,?)",
        [f"buy-{pid}", pid, "LIMIT", "BUY", entry_price, qty, "FILLED",
         exit_ts - timedelta(hours=3)],
    )
    conn.execute(
        "INSERT INTO orders (id, position_id, order_type, side, price, qty, "
        "status, filled_at) VALUES (?,?,?,?,?,?,?,?)",
        [f"sell-{pid}", pid, "MARKET", "SELL", sell_price, qty, "FILLED", exit_ts],
    )


def _populate_pre_and_post_baseline(eng):
    """The KI-138 reproduction shape: 17 pre-baseline losers, 8 post-baseline winners.

    Pre-baseline trades exited on 2026-05-09 (3 days before the 2026-05-12
    baseline). Post-baseline trades exited on 2026-05-13.
    """
    pre_exit = datetime(2026, 5, 9, 12, 0, 0)
    pre_entry = date(2026, 5, 8)
    for i in range(17):
        _add_closed_trade(eng, f"pre-{i}", f"PRE{i}USDT", pre_entry,
                          100.0, 10.0, 97.0, pre_exit)
    post_exit = datetime(2026, 5, 13, 12, 0, 0)
    post_entry = date(2026, 5, 12)
    for i in range(8):
        _add_closed_trade(eng, f"post-{i}", f"POST{i}USDT", post_entry,
                          100.0, 10.0, 105.0, post_exit)


def test_no_baseline_configured_counts_all_in_window(temp_db, monkeypatch):
    """Empty baselines list → behavior matches today: every trade in the
    rolling window counts. The 8/25 mix flips below the 60% critical floor."""
    monkeypatch.setattr(ptd, "_latest_baseline_date", lambda: None)

    eng = _new_engine_db()
    _healthy_engine(eng)
    _populate_pre_and_post_baseline(eng)

    res = ptd.run(engine_conn=eng, mhde_conn=temp_db, now=NOW)
    assert res.metrics["closed_trade_n"] == 25
    # 8/25 = 32% — below the 60% critical floor.
    assert res.status == "fail"
    assert res.severity == "critical"
    # metrics surface baseline-related fields even when unset
    assert res.metrics.get("active_strategy_baseline_date") is None
    assert res.metrics.get("closed_trade_n_excluded_pre_baseline") == 0


def test_baseline_excludes_pre_baseline_trades(temp_db, monkeypatch):
    """With the 2026-05-12 baseline applied: pre-baseline losers drop out;
    post-baseline winners alone (n=8) fall below MIN_CLOSED_FOR_HITRATE
    → insufficient-sample OK status (no false alert)."""
    monkeypatch.setattr(ptd, "_latest_baseline_date",
                        lambda: date(2026, 5, 12))

    eng = _new_engine_db()
    _healthy_engine(eng)
    _populate_pre_and_post_baseline(eng)

    res = ptd.run(engine_conn=eng, mhde_conn=temp_db, now=NOW)
    assert res.status == "ok"
    assert res.metrics["closed_trade_n"] == 8
    assert res.metrics["closed_trade_n_excluded_pre_baseline"] == 17
    assert res.metrics["active_strategy_baseline_date"] == "2026-05-12"
    assert "insufficient sample" in res.body.lower()
    # effective_window_start should be the baseline date (later than now-14d)
    assert res.metrics["effective_window_start"] == "2026-05-12"


def test_baseline_with_enough_post_baseline_sample(temp_db, monkeypatch):
    """If post-baseline trades clear MIN_CLOSED_FOR_HITRATE, the rate
    computes off the post-baseline slice only."""
    monkeypatch.setattr(ptd, "_latest_baseline_date",
                        lambda: date(2026, 5, 12))

    eng = _new_engine_db()
    _healthy_engine(eng)
    # 5 pre-baseline losers (excluded) + 20 post-baseline winners
    pre_exit = datetime(2026, 5, 9, 12, 0, 0)
    for i in range(5):
        _add_closed_trade(eng, f"pre-{i}", f"PRE{i}USDT",
                          date(2026, 5, 8), 100.0, 10.0, 90.0, pre_exit)
    post_exit = datetime(2026, 5, 13, 12, 0, 0)
    # 18 winners + 2 losers = 90%, inside the [0.74, 0.99] walkfold band
    for i in range(18):
        _add_closed_trade(eng, f"post-{i}", f"POST{i}USDT",
                          date(2026, 5, 12), 100.0, 10.0, 105.0, post_exit)
    for i in range(2):
        _add_closed_trade(eng, f"post-l{i}", f"POSTL{i}USDT",
                          date(2026, 5, 12), 100.0, 10.0, 97.0, post_exit)

    res = ptd.run(engine_conn=eng, mhde_conn=temp_db, now=NOW)
    assert res.status == "ok"
    assert res.metrics["closed_trade_n"] == 20
    assert res.metrics["closed_trade_n_excluded_pre_baseline"] == 5
    assert res.metrics["closed_trade_win_rate"] == 0.9


def test_multiple_baselines_latest_wins(temp_db, monkeypatch, tmp_path):
    """Two baselines configured; the most recent date is used as the floor."""
    cfg = """
paper_trading_drift:
  strategy_baselines:
    - date: "2026-04-01"
      reason: "earlier baseline"
    - date: "2026-05-12"
      reason: "KI-138 OHLCV repair"
"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "monitoring.yaml").write_text(cfg)
    # Other monitoring config files don't need to exist; load_engine_config
    # tolerates missing yaml.

    # Drive _latest_baseline_date through the real config path.
    monkeypatch.setattr(
        ptd, "_load_monitoring_config",
        lambda: __import__("yaml").safe_load((cfg_dir / "monitoring.yaml").read_text()),
    )

    eng = _new_engine_db()
    _healthy_engine(eng)
    _populate_pre_and_post_baseline(eng)

    res = ptd.run(engine_conn=eng, mhde_conn=temp_db, now=NOW)
    # Latest baseline (2026-05-12) used → pre-baseline excluded
    assert res.metrics["active_strategy_baseline_date"] == "2026-05-12"
    assert res.metrics["closed_trade_n_excluded_pre_baseline"] == 17


def test_baseline_floor_clamps_below_rolling_window(temp_db, monkeypatch):
    """When the baseline is OLDER than now - 14d, the rolling window stays
    the effective floor (no shrinkage); excluded count must be 0."""
    monkeypatch.setattr(ptd, "_latest_baseline_date",
                        lambda: date(2026, 1, 1))  # months ago

    eng = _new_engine_db()
    _healthy_engine(eng)
    _populate_pre_and_post_baseline(eng)

    res = ptd.run(engine_conn=eng, mhde_conn=temp_db, now=NOW)
    # All in-window trades counted; old baseline does not gain extra info
    assert res.metrics["closed_trade_n"] == 25
    assert res.metrics["closed_trade_n_excluded_pre_baseline"] == 0
    # rolling window floor (now - 14d = 2026-04-30) used as effective_window_start
    assert res.metrics["effective_window_start"] == "2026-04-30"


def test_label_hit_rate_respects_baseline_entry_date(temp_db, monkeypatch):
    """Check D — label hit rate filters on entry_date. Pre-baseline entries
    must be excluded from the label denominator."""
    monkeypatch.setattr(ptd, "_latest_baseline_date",
                        lambda: date(2026, 5, 12))

    eng = _new_engine_db()
    _healthy_engine(eng)

    # Pre-baseline entries (entered 2026-04-30, settled ~2026-05-10): 25 hits.
    pre_entry = date(2026, 4, 30)
    for i in range(25):
        sym = f"PRE{i}USDT"
        _add_closed_trade(eng, f"pre-{i}", sym, pre_entry,
                          100.0, 10.0, 105.0,
                          datetime(2026, 5, 1, 12, 0, 0))
        temp_db.execute(
            "INSERT INTO crypto_ml_labels (symbol, trade_date, label_10d_10pct) "
            "VALUES (?,?,?)",
            [sym, pre_entry, 1],
        )

    res = ptd.run(engine_conn=eng, mhde_conn=temp_db, now=NOW)
    # All 25 pre-baseline candidates excluded → no settled labels remain
    assert res.metrics["label_n"] == 0
    assert "insufficient sample" in res.body.lower()
