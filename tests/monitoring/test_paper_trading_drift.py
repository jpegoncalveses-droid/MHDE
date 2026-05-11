"""Unit tests for the paper-trading drift monitor (Gap 2).

The monitor reads the crypto-trading-engine's DuckDB (read-only) plus MHDE's
``crypto_ml_labels``. These tests build a synthetic engine DB in memory (and,
for the env-var path, on disk) and use the project ``temp_db`` fixture for the
MHDE side. ``MONITORING_DRY_RUN`` is forced so no real Telegram send happens.
"""
from __future__ import annotations

from datetime import datetime, timedelta, date

import duckdb
import pytest

from monitoring import paper_trading_drift as ptd


# ──────────────────────────────────────────────────────────────────────
# fixtures / builders
# ──────────────────────────────────────────────────────────────────────

NOW = datetime(2026, 5, 11, 9, 0, 0)  # naive UTC, well past the entry cutoff


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


def _new_engine_db(path: str | None = None) -> duckdb.DuckDBPyConnection:
    """A minimal engine DuckDB: just the columns the monitor reads."""
    conn = duckdb.connect(path if path else ":memory:")
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
    """Engine that is alive: monitor ticked a minute ago, entry ran today."""
    _add_run(conn, "monitor", now - timedelta(seconds=40))
    _add_run(conn, "monitor", now - timedelta(seconds=100))
    _add_run(conn, "entry", now.replace(hour=8, minute=2, second=0, microsecond=0))


def _add_closed_trade(conn, pid, symbol, entry_date, entry_price, qty,
                      sell_price, exit_ts):
    """A real round-trip: filled BUY, exit_filled position, FILLED SELL MARKET."""
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


def _add_label(mhde_conn, symbol, trade_date, label_10d_10pct):
    mhde_conn.execute(
        "INSERT INTO crypto_ml_labels (symbol, trade_date, label_10d_10pct) "
        "VALUES (?,?,?)",
        [symbol, trade_date, label_10d_10pct],
    )


def _run(engine_conn, mhde_conn, now=NOW):
    return ptd.run(engine_conn=engine_conn, mhde_conn=mhde_conn, now=now)


# ──────────────────────────────────────────────────────────────────────
# all-healthy baseline
# ──────────────────────────────────────────────────────────────────────

def test_all_healthy_is_ok(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    res = _run(eng, temp_db)
    assert res.status == "ok"
    assert res.severity == "info"
    assert res.monitor == "paper_trading_drift"
    # body still enumerates the checks (incl. insufficient-sample notes)
    assert "engine" in res.body.lower()


# ──────────────────────────────────────────────────────────────────────
# Check A — engine liveness
# ──────────────────────────────────────────────────────────────────────

def test_engine_monitor_stale_warn(temp_db):
    eng = _new_engine_db()
    _add_run(eng, "monitor", NOW - timedelta(minutes=7))
    _add_run(eng, "entry", NOW.replace(hour=8, minute=0))
    res = _run(eng, temp_db)
    assert res.status == "warn"
    assert res.severity == "warn"
    assert "monitor" in res.body.lower()


def test_engine_monitor_stale_critical(temp_db):
    eng = _new_engine_db()
    _add_run(eng, "monitor", NOW - timedelta(minutes=25))
    _add_run(eng, "entry", NOW.replace(hour=8, minute=0))
    res = _run(eng, temp_db)
    assert res.status == "fail"
    assert res.severity == "critical"


def test_engine_never_ran_monitor_is_critical(temp_db):
    eng = _new_engine_db()  # no engine_runs at all
    res = _run(eng, temp_db)
    assert res.status == "fail"
    assert res.severity == "critical"


def test_entry_missing_today_after_cutoff_warns(temp_db):
    eng = _new_engine_db()
    _add_run(eng, "monitor", NOW - timedelta(seconds=30))
    # entry ran *yesterday* only
    _add_run(eng, "entry", (NOW - timedelta(days=1)).replace(hour=8, minute=0))
    res = _run(eng, temp_db, now=NOW)  # NOW is 09:00, past 08:30 cutoff
    assert res.status == "warn"
    assert "entry" in res.body.lower()


def test_entry_missing_before_cutoff_is_not_alerted(temp_db):
    eng = _new_engine_db()
    early = datetime(2026, 5, 11, 8, 15, 0)  # before 08:30 cutoff
    _add_run(eng, "monitor", early - timedelta(seconds=30))
    _add_run(eng, "entry", (early - timedelta(days=1)).replace(hour=8))
    res = _run(eng, temp_db, now=early)
    assert res.status == "ok"


# ──────────────────────────────────────────────────────────────────────
# Check B — stuck-position staleness
# ──────────────────────────────────────────────────────────────────────

def test_position_stuck_pending_warn(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    eng.execute(
        "INSERT INTO positions (id, symbol, current_state, updated_at) "
        "VALUES (?,?,?,?)",
        ["p1", "FOOUSDT", "entry_pending", NOW - timedelta(minutes=12)],
    )
    res = _run(eng, temp_db)
    assert res.status == "warn"
    assert "FOOUSDT" in res.body


def test_position_stuck_pending_critical(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    eng.execute(
        "INSERT INTO positions (id, symbol, current_state, updated_at) "
        "VALUES (?,?,?,?)",
        ["p1", "BARUSDT", "exit_pending", NOW - timedelta(minutes=35)],
    )
    res = _run(eng, temp_db)
    assert res.status == "fail"
    assert res.severity == "critical"
    assert "BARUSDT" in res.body


def test_position_freshly_pending_is_fine(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    eng.execute(
        "INSERT INTO positions (id, symbol, current_state, updated_at) "
        "VALUES (?,?,?,?)",
        ["p1", "BAZUSDT", "entry_pending", NOW - timedelta(minutes=3)],
    )
    res = _run(eng, temp_db)
    assert res.status == "ok"


# ──────────────────────────────────────────────────────────────────────
# Check C — closed-trade win rate
# ──────────────────────────────────────────────────────────────────────

def _populate_closed_trades(eng, n_winners, n_losers, exit_age_days=2):
    """n_winners + n_losers closed trades, all exited `exit_age_days` ago."""
    exit_ts = NOW - timedelta(days=exit_age_days)
    entry_d = (NOW - timedelta(days=exit_age_days + 1)).date()
    i = 0
    for _ in range(n_winners):
        i += 1
        # +5% gross — clears the fee haircut comfortably
        _add_closed_trade(eng, f"w{i}", f"W{i}USDT", entry_d, 100.0, 10.0,
                          105.0, exit_ts)
    for _ in range(n_losers):
        i += 1
        # -3% gross — a clear loser
        _add_closed_trade(eng, f"l{i}", f"L{i}USDT", entry_d, 100.0, 10.0,
                          97.0, exit_ts)


def test_closed_win_rate_below_band_warns(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 13 winners / 9 losers = 0.59 win rate over 22 trades (>= 20 gate)
    _populate_closed_trades(eng, n_winners=13, n_losers=9)
    res = _run(eng, temp_db)
    assert res.status in {"warn", "fail"}
    assert "win rate" in res.body.lower()


def test_closed_win_rate_far_below_band_is_critical(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 11 winners / 11 losers = 0.50 < 0.60 critical floor
    _populate_closed_trades(eng, n_winners=11, n_losers=11)
    res = _run(eng, temp_db)
    assert res.status == "fail"
    assert res.severity == "critical"


def test_closed_win_rate_in_band_is_ok(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 20 winners / 2 losers ≈ 0.91 — inside [0.74, 0.99]
    _populate_closed_trades(eng, n_winners=20, n_losers=2)
    res = _run(eng, temp_db)
    assert res.status == "ok"


def test_closed_win_rate_insufficient_sample_no_alert(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # only 12 closed trades, all losers — would be a screaming alert if not gated
    _populate_closed_trades(eng, n_winners=0, n_losers=12)
    res = _run(eng, temp_db)
    assert res.status == "ok"
    assert "insufficient sample" in res.body.lower()


def test_phantom_autoclosed_position_excluded_from_denominator(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    _populate_closed_trades(eng, n_winners=20, n_losers=2)  # 22 real trades
    # a RECONCILE-001 phantom: exit_filled, NULL entry_price, no SELL order
    eng.execute(
        "INSERT INTO positions (id, symbol, entry_date, entry_price, qty, "
        "current_state, updated_at) VALUES (?,?,?,?,?,?,?)",
        ["phantom", "GHOSTUSDT", (NOW - timedelta(days=3)).date(), None, None,
         "exit_filled", NOW - timedelta(days=2)],
    )
    res = _run(eng, temp_db)
    # phantom must not drag N or the win rate
    assert res.status == "ok"
    assert res.metrics.get("closed_trade_n") == 22


def test_closed_trade_without_exit_price_is_reported_uncomputable(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # a real entry + a FILLED market SELL, but the engine recorded price=NULL
    eng.execute(
        "INSERT INTO positions (id, symbol, entry_date, entry_price, qty, "
        "current_state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ["px", "NOPXUSDT", (NOW - timedelta(days=3)).date(), 100.0, 10.0,
         "exit_filled", NOW - timedelta(days=2, hours=3), NOW - timedelta(days=2)],
    )
    eng.execute(
        "INSERT INTO orders (id, position_id, order_type, side, price, qty, "
        "status, filled_at) VALUES (?,?,?,?,?,?,?,?)",
        ["sell-px", "px", "MARKET", "SELL", None, 10.0, "FILLED",
         NOW - timedelta(days=2)],
    )
    res = _run(eng, temp_db)
    assert res.status == "ok"
    assert "exit fill price" in res.body.lower() or "uncomputable" in res.body.lower()
    assert res.metrics.get("closed_trade_n") == 0
    assert res.metrics.get("closed_trade_no_exit_price") == 1


def test_old_closed_trades_outside_window_excluded(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 22 losers, but they exited 30 days ago — outside the 14-day window
    _populate_closed_trades(eng, n_winners=0, n_losers=22, exit_age_days=30)
    res = _run(eng, temp_db)
    assert res.status == "ok"
    assert "insufficient sample" in res.body.lower()


# ──────────────────────────────────────────────────────────────────────
# Check D — label hit rate
# ──────────────────────────────────────────────────────────────────────

def _populate_settled_positions_and_labels(eng, mhde, n, label_value_for):
    """n closed positions whose labels settled ~12 days ago (inside window).

    `label_value_for(i)` returns the 0/1 label for the i-th position.
    """
    entry_d = (NOW - timedelta(days=22)).date()  # settled at entry+10 = 12d ago
    for i in range(n):
        pid = f"d{i}"
        sym = f"D{i}USDT"
        _add_closed_trade(eng, pid, sym, entry_d, 100.0, 10.0, 104.0,
                          NOW - timedelta(days=22) + timedelta(hours=2))
        _add_label(mhde, sym, entry_d, label_value_for(i))


def test_label_hit_rate_above_band_warns(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 20 positions, 16 hit → 0.80, above the 0.62 warn ceiling
    _populate_settled_positions_and_labels(eng, temp_db, 20,
                                           lambda i: 1 if i < 16 else 0)
    res = _run(eng, temp_db)
    assert res.status in {"warn", "fail"}
    assert "label hit rate" in res.body.lower()


def test_label_hit_rate_far_below_band_is_critical(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 20 positions, 2 hit → 0.10, below the 0.20 critical floor
    _populate_settled_positions_and_labels(eng, temp_db, 20,
                                           lambda i: 1 if i < 2 else 0)
    res = _run(eng, temp_db)
    assert res.status == "fail"
    assert res.severity == "critical"


def test_label_hit_rate_in_band_is_ok(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 20 positions, 9 hit → 0.45, inside [0.32, 0.62]
    _populate_settled_positions_and_labels(eng, temp_db, 20,
                                           lambda i: 1 if i < 9 else 0)
    res = _run(eng, temp_db)
    assert res.status == "ok"


def test_label_hit_rate_unsettled_positions_excluded(temp_db):
    eng = _new_engine_db()
    _healthy_engine(eng)
    # 25 closed positions entered only 3 days ago — labels not settled yet
    entry_d = (NOW - timedelta(days=3)).date()
    for i in range(25):
        _add_closed_trade(eng, f"u{i}", f"U{i}USDT", entry_d, 100.0, 10.0,
                          104.0, NOW - timedelta(days=2))
        _add_label(temp_db, f"U{i}USDT", entry_d, 0)  # label exists but not "settled"
    res = _run(eng, temp_db, now=NOW)
    # label arm must report insufficient sample, not a 0% hit-rate critical
    assert "label hit rate: insufficient sample" in res.body.lower()


# ──────────────────────────────────────────────────────────────────────
# severity aggregation + env-var path
# ──────────────────────────────────────────────────────────────────────

def test_severity_is_worst_of_all_checks(temp_db):
    eng = _new_engine_db()
    _add_run(eng, "monitor", NOW - timedelta(minutes=7))   # warn arm
    _add_run(eng, "entry", NOW.replace(hour=8))
    eng.execute(
        "INSERT INTO positions (id, symbol, current_state, updated_at) "
        "VALUES (?,?,?,?)",
        ["p1", "STUCKUSDT", "exit_pending", NOW - timedelta(minutes=40)],  # critical arm
    )
    res = _run(eng, temp_db)
    assert res.status == "fail"
    assert res.severity == "critical"


def test_engine_db_path_from_env_var(temp_db, tmp_path, monkeypatch):
    db_file = tmp_path / "trading_engine.duckdb"
    eng = _new_engine_db(str(db_file))
    # deliberately stale so the env-sourced DB is observably the one read
    _add_run(eng, "monitor", NOW - timedelta(minutes=25))
    _add_run(eng, "entry", NOW.replace(hour=8))
    eng.close()
    monkeypatch.setenv("CRYPTO_ENGINE_DB_PATH", str(db_file))
    res = ptd.run(mhde_conn=temp_db, now=NOW)  # engine_conn=None → opens env path
    assert res.status == "fail"
    assert res.severity == "critical"


def test_main_returns_nonzero_on_problem(temp_db, monkeypatch):
    eng = _new_engine_db()  # nothing ran → critical
    monkeypatch.setattr(ptd, "_utcnow_naive", lambda: NOW)
    monkeypatch.setattr(ptd, "_open_engine_db", lambda: eng)
    monkeypatch.setattr(ptd, "_open_mhde_db", lambda: temp_db)
    rc = ptd.main()
    assert rc == 1


def test_main_returns_zero_when_ok(temp_db, monkeypatch):
    eng = _new_engine_db()
    _healthy_engine(eng)
    monkeypatch.setattr(ptd, "_utcnow_naive", lambda: NOW)
    monkeypatch.setattr(ptd, "_open_engine_db", lambda: eng)
    monkeypatch.setattr(ptd, "_open_mhde_db", lambda: temp_db)
    rc = ptd.main()
    assert rc == 0
