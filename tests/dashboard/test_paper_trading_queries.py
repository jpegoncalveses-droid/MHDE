"""Unit tests for the Paper Trading tab's query/transform layer (Gap 3).

These exercise the pure functions in ``dashboard.services.queries`` that read
the crypto-trading-engine DuckDB; the Streamlit ``with tab_paper:`` block
itself is not unit-tested (consistent with the rest of ``dashboard/app.py``).
A synthetic engine DuckDB is built in memory — same minimal-schema approach as
``tests/monitoring/test_paper_trading_drift.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import duckdb
import pytest

from dashboard.services import queries as q

NOW = datetime(2026, 5, 11, 12, 0, 0)


def _engine_db() -> duckdb.DuckDBPyConnection:
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
            created_at TIMESTAMP, updated_at TIMESTAMP,
            exit_price DOUBLE, realized_pnl_usd DOUBLE
        )""")
    conn.execute("""
        CREATE TABLE events (
            id VARCHAR, timestamp TIMESTAMP, position_id VARCHAR,
            event_type VARCHAR, payload JSON
        )""")
    # Mirrors crypto-trading-engine/engine/state/schema.sql:46-55. NOT NULL
    # on every column except spec_version, so test inserts must supply them.
    conn.execute("""
        CREATE TABLE daily_pnl (
            date                       DATE PRIMARY KEY,
            realized_pnl_usd           DOUBLE NOT NULL,
            unrealized_pnl_usd         DOUBLE NOT NULL,
            account_equity_usd         DOUBLE NOT NULL,
            num_open                   INTEGER NOT NULL,
            num_closed                 INTEGER NOT NULL,
            num_skipped_below_minimum  INTEGER NOT NULL,
            spec_version               VARCHAR
        )""")
    return conn


def _daily(conn, on_date, equity, realized=0.0, unrealized=0.0):
    """Insert one daily_pnl row with defaults for unused columns."""
    conn.execute(
        "INSERT INTO daily_pnl (date, realized_pnl_usd, unrealized_pnl_usd, "
        "account_equity_usd, num_open, num_closed, num_skipped_below_minimum) "
        "VALUES (?, ?, ?, ?, 0, 0, 0)",
        [on_date, realized, unrealized, equity],
    )


def _pos(conn, id, symbol, state, *, entry_date=None, entry_price=None, qty=None,
         peak_price=None, updated_at=None, exit_price=None, realized_pnl_usd=None):
    conn.execute(
        "INSERT INTO positions (id, symbol, entry_date, entry_price, qty, "
        "peak_price, current_state, created_at, updated_at, exit_price, realized_pnl_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [id, symbol, entry_date or NOW.date(), entry_price, qty, peak_price,
         state, (updated_at or NOW) - timedelta(hours=2), updated_at or NOW,
         exit_price, realized_pnl_usd],
    )


def _event(conn, position_id, event_type, payload, ts=None):
    conn.execute(
        "INSERT INTO events (id, timestamp, position_id, event_type, payload) "
        "VALUES (?,?,?,?,?)",
        [f"e-{position_id}-{event_type}-{ts or NOW}", ts or NOW, position_id,
         event_type, json.dumps(payload)],
    )


def _run(conn, phase, started_at, success=True):
    conn.execute("INSERT INTO engine_runs (id, phase, started_at, success) "
                 "VALUES (?,?,?,?)",
                 [f"r-{phase}-{started_at}", phase, started_at, success])


# ── _connect_engine ──────────────────────────────────────────────────

def test_connect_engine_uses_env_var(tmp_path, monkeypatch):
    db_file = tmp_path / "trading_engine.duckdb"
    c = duckdb.connect(str(db_file))
    c.execute("CREATE TABLE positions (id VARCHAR)")
    c.close()
    monkeypatch.setenv("CRYPTO_ENGINE_DB_PATH", str(db_file))
    conn = q._connect_engine()
    assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
    conn.close()


# ── get_paper_open_positions ─────────────────────────────────────────

def test_open_positions_filters_to_live_states():
    e = _engine_db()
    _pos(e, "a", "AUSDT", "entry_pending", entry_price=10.0, qty=5.0, peak_price=10.0)
    _pos(e, "b", "BUSDT", "trailing_active", entry_price=10.0, qty=5.0, peak_price=12.0)
    _pos(e, "c", "CUSDT", "exit_filled", entry_price=10.0, qty=5.0, peak_price=11.0)  # excluded
    _pos(e, "d", "DUSDT", "failed")  # excluded
    df = q.get_paper_open_positions(e, trail_pct=0.30, activation_pct=0.01)
    assert set(df["symbol"]) == {"AUSDT", "BUSDT"}


def test_open_positions_calc_stop_when_activated():
    e = _engine_db()
    # peak 12 vs entry 10 → +20% > activation 1% → active. stop = 12 - 0.3*(12-10) = 11.4
    _pos(e, "b", "BUSDT", "trailing_active", entry_price=10.0, qty=5.0, peak_price=12.0)
    df = q.get_paper_open_positions(e, trail_pct=0.30, activation_pct=0.01)
    row = df[df["symbol"] == "BUSDT"].iloc[0]
    assert pytest.approx(float(row["calc_stop"]), rel=1e-9) == 11.4


def test_open_positions_calc_stop_not_activated():
    e = _engine_db()
    # peak == entry → not past activation → "— (not activated)"
    _pos(e, "a", "AUSDT", "entry_filled", entry_price=10.0, qty=5.0, peak_price=10.0)
    df = q.get_paper_open_positions(e, trail_pct=0.30, activation_pct=0.01)
    row = df[df["symbol"] == "AUSDT"].iloc[0]
    assert "not activated" in str(row["calc_stop"])


def test_open_positions_null_entry_price_renders_dash():
    e = _engine_db()
    _pos(e, "p", "PHANTOMUSDT", "entry_pending", entry_price=None, qty=None, peak_price=None)
    df = q.get_paper_open_positions(e, trail_pct=0.30, activation_pct=0.01)
    row = df[df["symbol"] == "PHANTOMUSDT"].iloc[0]
    assert str(row["calc_stop"]) == "—"
    # entry_price / qty rendered as a dash too (string column, never NaN-crashes)
    assert str(row["entry_price"]) == "—"


# ── get_paper_closed_trades ──────────────────────────────────────────

def test_closed_trades_order_and_limit():
    e = _engine_db()
    for i in range(5):
        _pos(e, f"c{i}", f"C{i}USDT", "exit_filled", entry_price=10.0, qty=5.0,
             peak_price=11.0, updated_at=NOW - timedelta(hours=i))
    df = q.get_paper_closed_trades(e, limit=3)
    assert len(df) == 3
    # newest first → C0, C1, C2
    assert list(df["symbol"]) == ["C0USDT", "C1USDT", "C2USDT"]


def test_closed_trades_exit_price_and_pnl_from_columns():
    # EXIT-PRICE-001 / KI-136: when positions.exit_price / realized_pnl_usd are
    # populated (engine-recorded SELL fill or reconcile backfill), show them —
    # exit_price verbatim, realized P&L rounded to cents.
    e = _engine_db()
    _pos(e, "c", "SKYAIUSDT", "exit_filled", entry_price=0.47512, qty=1403.0,
         peak_price=0.47601, exit_price=0.38288, realized_pnl_usd=-129.41271999999998)
    df = q.get_paper_closed_trades(e, limit=10)
    row = df.iloc[0]
    assert pytest.approx(float(row["exit_price"]), rel=1e-12) == 0.38288
    assert pytest.approx(float(row["realized_pnl"]), abs=1e-9) == -129.41


def test_closed_trades_null_exit_columns_show_uncomputable():
    # Pre-EXIT-PRICE-001 closes: exit columns NULL → placeholder, not a fake 0.
    e = _engine_db()
    _pos(e, "c", "CUSDT", "exit_filled", entry_price=10.0, qty=5.0, peak_price=11.0,
         exit_price=None, realized_pnl_usd=None)
    df = q.get_paper_closed_trades(e, limit=10)
    row = df.iloc[0]
    assert "uncomputable" in str(row["exit_price"]).lower()
    assert "uncomputable" in str(row["realized_pnl"]).lower()


def test_closed_trades_exit_price_known_but_pnl_null():
    # Reconcile backfill recovered the SELL fill price but entry_price was NULL,
    # so realized P&L couldn't be computed — columns handled independently.
    e = _engine_db()
    _pos(e, "c", "CUSDT", "exit_filled", entry_price=None, qty=None, peak_price=None,
         exit_price=0.0013717, realized_pnl_usd=None)
    df = q.get_paper_closed_trades(e, limit=10)
    row = df.iloc[0]
    assert pytest.approx(float(row["exit_price"]), rel=1e-12) == 0.0013717
    assert "uncomputable" in str(row["realized_pnl"]).lower()


def test_closed_trades_orphan_auto_close_still_uncomputable():
    # engine_only_position auto-closed by reconcile: no real SELL fill, so the
    # exit columns stay NULL — still "uncomputable", and the close reason names it.
    e = _engine_db()
    _pos(e, "o", "SKYAIUSDT", "exit_filled", entry_date=NOW.date() - timedelta(days=1),
         entry_price=None, qty=None, peak_price=None,
         exit_price=None, realized_pnl_usd=None)
    _event(e, "o", "reconcile_auto_closed",
           {"kind": "engine_only_position",
            "details": {"last_known_state": "entry_pending", "entry_price": None, "qty": None},
            "note": "auto-closed by reconciliation; check Binance for cause"})
    df = q.get_paper_closed_trades(e, limit=10)
    row = df.iloc[0]
    assert "uncomputable" in str(row["exit_price"]).lower()
    assert "uncomputable" in str(row["realized_pnl"]).lower()
    assert "engine_only_position" in str(row["close_reason"])


def test_closed_trades_close_reason_from_event():
    e = _engine_db()
    _pos(e, "c", "CUSDT", "exit_filled", entry_price=10.0, qty=5.0, peak_price=11.0)
    _event(e, "c", "reconcile_action",
           {"action": "manual_close", "operator_reason": "manual_close_leverage_fix"})
    _pos(e, "d", "DUSDT", "exit_filled", entry_price=10.0, qty=5.0, peak_price=11.0,
         updated_at=NOW - timedelta(hours=1))  # no events → reason ""
    df = q.get_paper_closed_trades(e, limit=10)
    by_sym = {r["symbol"]: r for _, r in df.iterrows()}
    assert "manual_close_leverage_fix" in str(by_sym["CUSDT"]["close_reason"])
    assert str(by_sym["DUSDT"]["close_reason"]) == ""


# ── get_paper_failed_entries ─────────────────────────────────────────

def test_failed_entries_only_failed_state_and_limit():
    e = _engine_db()
    for i in range(4):
        _pos(e, f"f{i}", f"F{i}USDT", "failed", updated_at=NOW - timedelta(minutes=i))
    _pos(e, "ok", "OKUSDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=1.0)
    df = q.get_paper_failed_entries(e, limit=3)
    assert len(df) == 3
    assert set(df["symbol"]) <= {"F0USDT", "F1USDT", "F2USDT", "F3USDT"}
    assert "OKUSDT" not in set(df["symbol"])


# ── get_paper_engine_runs_summary ────────────────────────────────────

def test_engine_runs_summary_basic():
    e = _engine_db()
    _run(e, "monitor", NOW - timedelta(minutes=1))
    _run(e, "entry", NOW.replace(hour=8))
    _pos(e, "o1", "O1USDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=1.0)
    _pos(e, "o2", "O2USDT", "trailing_active", entry_price=1.0, qty=1.0, peak_price=1.1)
    _pos(e, "c1", "C1USDT", "exit_filled", entry_price=1.0, qty=1.0, peak_price=1.0,
         updated_at=NOW - timedelta(days=2))
    _pos(e, "c2", "C2USDT", "exit_filled", entry_price=1.0, qty=1.0, peak_price=1.0,
         updated_at=NOW - timedelta(days=40))  # outside 14d
    s = q.get_paper_engine_runs_summary(e, now=NOW)
    assert s["last_monitor_at"] is not None
    assert s["last_entry_at"] is not None
    assert s["n_open"] == 2
    assert s["n_closed_14d"] == 1


def test_engine_runs_summary_empty():
    e = _engine_db()
    s = q.get_paper_engine_runs_summary(e, now=NOW)
    assert s["last_monitor_at"] is None
    assert s["last_entry_at"] is None
    assert s["n_open"] == 0
    assert s["n_closed_14d"] == 0


# ── get_daily_balance_since_baseline + paper_baseline_date ───────────
# The Paper Trading tab puts a "daily balance" table at the top so the
# operator can read the equity curve since the 2026-05-12 strategy reset
# without scrolling. Source: crypto-trading-engine's daily_pnl table
# (ADR-020, read-only).

from datetime import date


def test_paper_baseline_date_reads_config(tmp_path, monkeypatch):
    """When config/monitoring.yaml declares a baseline, that wins over the
    hardcoded fallback."""
    cfg_text = (
        "paper_trading_drift:\n"
        "  strategy_baselines:\n"
        "    - date: \"2026-04-01\"\n"
        "      reason: \"earlier\"\n"
        "    - date: \"2026-05-12\"\n"
        "      reason: \"KI-138 OHLCV repair\"\n"
    )
    cfg_path = tmp_path / "monitoring.yaml"
    cfg_path.write_text(cfg_text)
    import yaml
    monkeypatch.setattr(
        q, "_load_monitoring_config",
        lambda: yaml.safe_load(cfg_path.read_text()),
    )
    assert q.paper_baseline_date() == date(2026, 5, 12)


def test_paper_baseline_date_falls_back_to_hardcoded(monkeypatch):
    """No config / empty baselines list → the hardcoded 2026-05-12 anchor
    is returned so the dashboard never breaks if monitoring.yaml is absent."""
    monkeypatch.setattr(q, "_load_monitoring_config", lambda: {})
    assert q.paper_baseline_date() == date(2026, 5, 12)


def test_daily_balance_columns_and_first_row(monkeypatch):
    """Schema, ordering, and first-row sentinels.

    daily_delta is None on the earliest in-window row (no prior to diff
    against). cumulative_delta is anchored to the earliest in-window
    equity, so the first row's cumulative is exactly 0.0."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1020.0)
    _daily(e, date(2026, 5, 14), equity=1015.0)

    df = q.get_daily_balance_since_baseline(e, since=date(2026, 5, 12))
    assert list(df.columns) == ["date", "equity", "daily_delta", "cumulative_delta"]
    assert len(df) == 3
    assert df.iloc[0]["date"] == date(2026, 5, 12)
    assert df.iloc[0]["equity"] == 1000.0
    # First-row daily_delta has no prior to diff against — None from the
    # query becomes NaN in the float column, which is what Streamlit
    # renders as an empty cell.
    import pandas as _pd
    assert _pd.isna(df.iloc[0]["daily_delta"])
    assert df.iloc[0]["cumulative_delta"] == 0.0
    assert df.iloc[1]["daily_delta"] == pytest.approx(20.0)
    assert df.iloc[1]["cumulative_delta"] == pytest.approx(20.0)
    assert df.iloc[2]["daily_delta"] == pytest.approx(-5.0)
    assert df.iloc[2]["cumulative_delta"] == pytest.approx(15.0)


def test_daily_balance_excludes_pre_baseline_rows():
    e = _engine_db()
    # Pre-baseline rows that must be filtered out.
    _daily(e, date(2026, 5, 9), equity=950.0)
    _daily(e, date(2026, 5, 11), equity=975.0)
    # Post-baseline rows that count.
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)

    df = q.get_daily_balance_since_baseline(e, since=date(2026, 5, 12))
    assert list(df["date"]) == [date(2026, 5, 12), date(2026, 5, 13)]
    # cumulative_delta anchored to 5/12 — the pre-baseline 975 is invisible.
    assert df.iloc[1]["cumulative_delta"] == pytest.approx(10.0)


def test_daily_balance_empty_table_returns_empty_dataframe():
    """Pre-RECONCILE-001 reality: daily_pnl is empty. The query must return
    an empty DataFrame (correct schema, zero rows) so the tab renders an
    "no data" branch without crashing."""
    e = _engine_db()
    df = q.get_daily_balance_since_baseline(e, since=date(2026, 5, 12))
    assert df.empty
    assert list(df.columns) == ["date", "equity", "daily_delta", "cumulative_delta"]


def test_daily_balance_handles_negative_streak():
    """Equity drops — daily_delta and cumulative_delta both go negative,
    no special handling required."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=990.0)
    _daily(e, date(2026, 5, 14), equity=975.0)
    df = q.get_daily_balance_since_baseline(e, since=date(2026, 5, 12))
    assert df.iloc[1]["daily_delta"] == pytest.approx(-10.0)
    assert df.iloc[2]["daily_delta"] == pytest.approx(-15.0)
    assert df.iloc[2]["cumulative_delta"] == pytest.approx(-25.0)


def test_daily_balance_preserves_gaps():
    """If the engine's reconcile timer skipped a day, the resulting row gap
    is preserved as-is — the query doesn't backfill. Daily Δ on the row
    after a gap is the raw difference against the previous *present* row,
    not against the missing day."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    # 5/13 missing (reconcile skipped)
    _daily(e, date(2026, 5, 14), equity=1030.0)
    df = q.get_daily_balance_since_baseline(e, since=date(2026, 5, 12))
    assert list(df["date"]) == [date(2026, 5, 12), date(2026, 5, 14)]
    assert df.iloc[1]["daily_delta"] == pytest.approx(30.0)


def test_daily_balance_orders_by_date_ascending():
    """Whatever insertion order the engine used, the dashboard reads
    oldest-first — that is the natural reading order for a balance table."""
    e = _engine_db()
    _daily(e, date(2026, 5, 14), equity=1015.0)
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)
    df = q.get_daily_balance_since_baseline(e, since=date(2026, 5, 12))
    assert list(df["date"]) == [
        date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 14),
    ]


def test_daily_balance_since_override_takes_precedence():
    """The ``since`` argument is mandatory and authoritative — callers pass
    the baseline they want, the function does not silently default."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)
    df = q.get_daily_balance_since_baseline(e, since=date(2026, 5, 13))
    assert len(df) == 1
    assert df.iloc[0]["date"] == date(2026, 5, 13)
    assert df.iloc[0]["cumulative_delta"] == 0.0
