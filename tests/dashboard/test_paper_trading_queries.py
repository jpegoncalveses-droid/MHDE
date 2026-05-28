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
    conn.execute("""
        CREATE TABLE price_snapshots (
            position_id VARCHAR NOT NULL,
            timestamp   TIMESTAMP NOT NULL,
            price       DOUBLE NOT NULL
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


def _snap(conn, position_id, price, ts=None):
    conn.execute(
        "INSERT INTO price_snapshots (position_id, timestamp, price) "
        "VALUES (?, ?, ?)",
        [position_id, ts or NOW, price],
    )


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
    against). cumulative_delta is the running sum of realized_pnl_usd; with
    no closed positions in this fixture every row's realized is 0, so the
    cumulative stays 0.0 throughout (the equity curve is irrelevant to it).

    ``today`` is pinned to the last reconciled date so no preliminary
    row is synthesized — this test exercises the historical-only path.
    """
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1020.0)
    _daily(e, date(2026, 5, 14), equity=1015.0)

    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14)
    )
    assert list(df.columns) == [
        "date", "equity", "realized_pnl_usd", "unrealized_pnl_usd",
        "daily_delta", "cumulative_delta", "is_preliminary",
    ]
    assert len(df) == 3
    assert df.iloc[0]["date"] == date(2026, 5, 12)
    assert df.iloc[0]["equity"] == 1000.0
    # First-row daily_delta has no prior to diff against — None from the
    # query becomes NaN in the float column, which is what Streamlit
    # renders as an empty cell.
    import pandas as _pd
    assert _pd.isna(df.iloc[0]["daily_delta"])
    # daily_delta still tracks the equity curve.
    assert df.iloc[1]["daily_delta"] == pytest.approx(20.0)
    assert df.iloc[2]["daily_delta"] == pytest.approx(-5.0)
    # cumulative_delta is the realized cumsum — 0 everywhere with no closures.
    assert df.iloc[0]["cumulative_delta"] == 0.0
    assert df.iloc[1]["cumulative_delta"] == 0.0
    assert df.iloc[2]["cumulative_delta"] == 0.0
    # No preliminary row — today (5/14) is already reconciled.
    assert not df["is_preliminary"].any()


def test_daily_balance_excludes_pre_baseline_rows():
    e = _engine_db()
    # Pre-baseline rows that must be filtered out.
    _daily(e, date(2026, 5, 9), equity=950.0)
    _daily(e, date(2026, 5, 11), equity=975.0)
    # Post-baseline rows that count.
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)

    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 13)
    )
    assert list(df["date"]) == [date(2026, 5, 12), date(2026, 5, 13)]
    # Pre-baseline rows (5/9, 5/11) are excluded by the date filter. No
    # closed positions here, so cumulative_delta (realized cumsum) is 0.0.
    assert df.iloc[1]["cumulative_delta"] == pytest.approx(0.0)


def test_daily_balance_empty_table_returns_empty_dataframe():
    """No daily_pnl rows AND today is earlier than ``since`` → no rows
    to render and no preliminary row to synthesize. Returns the
    empty-schema DataFrame.

    The "daily_pnl empty AND today >= since" case is covered separately
    (synthesizes a single preliminary row with NaN equity).
    """
    e = _engine_db()
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 11)
    )
    assert df.empty
    assert list(df.columns) == [
        "date", "equity", "realized_pnl_usd", "unrealized_pnl_usd",
        "daily_delta", "cumulative_delta", "is_preliminary",
    ]


def test_daily_balance_handles_negative_streak():
    """Equity drops — daily_delta (equity-based) goes negative. With no
    closed positions, cumulative_delta (realized cumsum) stays 0.0."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=990.0)
    _daily(e, date(2026, 5, 14), equity=975.0)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14)
    )
    assert df.iloc[1]["daily_delta"] == pytest.approx(-10.0)
    assert df.iloc[2]["daily_delta"] == pytest.approx(-15.0)
    assert df.iloc[2]["cumulative_delta"] == pytest.approx(0.0)


def test_daily_balance_preserves_gaps():
    """If the engine's reconcile timer skipped a day, the resulting row gap
    is preserved as-is — the query doesn't backfill. Daily Δ on the row
    after a gap is the raw difference against the previous *present* row,
    not against the missing day."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    # 5/13 missing (reconcile skipped)
    _daily(e, date(2026, 5, 14), equity=1030.0)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14)
    )
    assert list(df["date"]) == [date(2026, 5, 12), date(2026, 5, 14)]
    assert df.iloc[1]["daily_delta"] == pytest.approx(30.0)


def test_daily_balance_orders_by_date_ascending():
    """Whatever insertion order the engine used, the dashboard reads
    oldest-first — that is the natural reading order for a balance table."""
    e = _engine_db()
    _daily(e, date(2026, 5, 14), equity=1015.0)
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14)
    )
    assert list(df["date"]) == [
        date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 14),
    ]


def test_daily_balance_since_override_takes_precedence():
    """The ``since`` argument is mandatory and authoritative — callers pass
    the baseline they want, the function does not silently default."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 13), today=date(2026, 5, 13)
    )
    assert len(df) == 1
    assert df.iloc[0]["date"] == date(2026, 5, 13)
    assert df.iloc[0]["cumulative_delta"] == 0.0


# ── Daily balance: realized/unrealized columns + today-row synthesis ──
# The dashboard surfaces today's preliminary balance in-day so the operator
# doesn't have to wait until 23:00 UTC reconcile to see progress. Two new
# columns are added regardless of synthesis (sourced from daily_pnl for
# reconciled rows). When today's row is missing from daily_pnl, it is
# synthesized in-process from the engine's positions + price_snapshots
# tables — no Binance API call, per ADR-020 / INTERFACE.md.


def test_daily_balance_includes_realized_and_unrealized_columns():
    """Reconciled rows expose realized_pnl_usd + unrealized_pnl_usd
    recomputed from the positions table, filtered to entry_date >=
    baseline. The fix-daily-balance-baseline-awareness branch moved
    these from daily_pnl pass-through to position-level aggregation so
    pre-baseline positions don't pollute post-baseline strategy
    attribution.
    """
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1020.0)
    # Closure on 5/12 → row 5/12 realized = 50
    _pos(e, "c12", "CUSDT", "exit_filled",
         entry_date=date(2026, 5, 12), entry_price=10.0, qty=5.0,
         peak_price=20.0, exit_price=20.0, realized_pnl_usd=50.0,
         updated_at=datetime(2026, 5, 12, 9, 0))
    # Closure on 5/13 → row 5/13 realized = 70
    _pos(e, "c13", "DUSDT", "exit_filled",
         entry_date=date(2026, 5, 13), entry_price=10.0, qty=7.0,
         peak_price=20.0, exit_price=20.0, realized_pnl_usd=70.0,
         updated_at=datetime(2026, 5, 13, 9, 0))
    # Open position spanning both rows → unrealized at EOD each
    _pos(e, "open1", "OPENUSDT", "entry_filled",
         entry_date=date(2026, 5, 12), entry_price=10.0, qty=1.0,
         peak_price=10.0, updated_at=datetime(2026, 5, 12, 9, 0))
    _snap(e, "open1", 20.0, ts=datetime(2026, 5, 12, 23, 0))  # +$10 at 5/12 EOD
    _snap(e, "open1", 15.0, ts=datetime(2026, 5, 13, 23, 0))  # +$5 at 5/13 EOD

    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 13),
        baseline_date=date(2026, 5, 12),
    )
    assert df.iloc[0]["realized_pnl_usd"] == pytest.approx(50.0)
    assert df.iloc[0]["unrealized_pnl_usd"] == pytest.approx(10.0)
    assert df.iloc[1]["realized_pnl_usd"] == pytest.approx(70.0)
    assert df.iloc[1]["unrealized_pnl_usd"] == pytest.approx(5.0)
    # cumulative_delta is the running realized sum, inclusive of the first
    # row: 5/12 = 50 (its own realized, not 0), 5/13 = 50 + 70 = 120.
    assert df.iloc[0]["cumulative_delta"] == pytest.approx(50.0)
    assert df.iloc[1]["cumulative_delta"] == pytest.approx(120.0)
    assert not df["is_preliminary"].any()


def test_daily_balance_synthesizes_today_when_missing():
    """daily_pnl has 5/12 and 5/13; today is 5/14, missing. A single
    preliminary row is appended with equity = prev + today_realized,
    today_realized from positions exit-filled today (filtered to
    entry_date >= baseline), today_unrealized from open positions'
    latest price snapshots (same filter)."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)
    # Closed today: realized 5 + 3 = 8. Closed yesterday: ignored.
    # All entry_dates >= baseline (5/12) so the filter is a no-op here.
    today_ts = datetime(2026, 5, 14, 9, 0, 0)
    _pos(e, "x", "XUSDT", "exit_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=5.0,
         peak_price=11.0, exit_price=11.0, realized_pnl_usd=5.0,
         updated_at=today_ts)
    _pos(e, "y", "YUSDT", "exit_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=2.0,
         peak_price=12.0, exit_price=11.5, realized_pnl_usd=3.0,
         updated_at=today_ts)
    _pos(e, "yest", "ZUSDT", "exit_filled",
         entry_date=date(2026, 5, 13),
         entry_price=10.0, qty=1.0,
         peak_price=11.0, exit_price=11.0, realized_pnl_usd=1.0,
         updated_at=datetime(2026, 5, 13, 9, 0, 0))
    # One open position: entry 100, latest snapshot 110, qty 2 → +20 unrealized.
    _pos(e, "open1", "OPEN1USDT", "entry_filled",
         entry_date=date(2026, 5, 14),
         entry_price=100.0, qty=2.0,
         peak_price=110.0, updated_at=today_ts)
    _snap(e, "open1", 110.0, ts=today_ts)

    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14),
        baseline_date=date(2026, 5, 12),
    )
    assert len(df) == 3
    today_row = df.iloc[-1]
    assert today_row["date"] == date(2026, 5, 14)
    assert today_row["is_preliminary"] is True or bool(today_row["is_preliminary"]) is True
    assert today_row["equity"] == pytest.approx(1018.0)  # 1010 + 8
    assert today_row["realized_pnl_usd"] == pytest.approx(8.0)
    assert today_row["unrealized_pnl_usd"] == pytest.approx(20.0)
    assert today_row["daily_delta"] == pytest.approx(8.0)  # 1018 - 1010
    # cumulative_delta = realized cumsum: 5/12=0, 5/13=1 (the "yest" closure
    # attributed to its exit date), 5/14=8 (today's two closures) → 9.0.
    assert today_row["cumulative_delta"] == pytest.approx(9.0)


def test_daily_balance_no_synthesis_when_today_already_reconciled():
    """If today is already in daily_pnl, the reconciled row is used —
    no synthesis, is_preliminary stays False. The per-row realized is
    still recomputed from positions filtered to entry_date >= baseline.
    """
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1050.0)
    # Closure on 5/13 entered 5/13 → counted under baseline_date=5/12
    _pos(e, "x", "XUSDT", "exit_filled",
         entry_date=date(2026, 5, 13),
         entry_price=10.0, qty=5.0, peak_price=11.0, exit_price=11.0,
         realized_pnl_usd=50.0,
         updated_at=datetime(2026, 5, 13, 9, 0, 0))
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 13),
        baseline_date=date(2026, 5, 12),
    )
    assert len(df) == 2
    assert not df["is_preliminary"].any()
    # Reconciled equity preserved.
    assert df.iloc[1]["equity"] == pytest.approx(1050.0)
    assert df.iloc[1]["realized_pnl_usd"] == pytest.approx(50.0)


def test_daily_balance_synthesized_today_with_no_closed_positions():
    """No exit_filled positions today → today_realized = 0.0 (not NaN);
    the preliminary row equity equals the previous reconciled equity."""
    e = _engine_db()
    _daily(e, date(2026, 5, 13), equity=1010.0)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14)
    )
    today_row = df.iloc[-1]
    assert today_row["is_preliminary"] is True or bool(today_row["is_preliminary"]) is True
    assert today_row["realized_pnl_usd"] == 0.0
    assert today_row["equity"] == pytest.approx(1010.0)
    assert today_row["unrealized_pnl_usd"] == 0.0


def test_daily_balance_synthesized_today_when_daily_pnl_empty():
    """Bootstrap edge case: no daily_pnl rows, but today >= since.
    A single preliminary row is shown with equity = NaN (no anchor) so
    the operator sees today's realized/unrealized numbers without a
    misleading made-up wallet balance. cumulative_delta is the realized
    cumsum, which is well-defined (5.0) even without an equity anchor."""
    import pandas as _pd
    e = _engine_db()
    today_ts = datetime(2026, 5, 14, 9, 0, 0)
    # Post-baseline closure on today (entry on 5/14, baseline = 5/12)
    _pos(e, "x", "XUSDT", "exit_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=5.0, peak_price=11.0, exit_price=11.0,
         realized_pnl_usd=5.0,
         updated_at=today_ts)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14),
        baseline_date=date(2026, 5, 12),
    )
    assert len(df) == 1
    row = df.iloc[0]
    assert row["date"] == date(2026, 5, 14)
    assert row["is_preliminary"] is True or bool(row["is_preliminary"]) is True
    assert _pd.isna(row["equity"])  # no prior anchor — equity unknown
    assert row["realized_pnl_usd"] == pytest.approx(5.0)
    assert _pd.isna(row["daily_delta"])  # still equity-based → NaN
    # cumulative_delta does not depend on equity: realized cumsum = 5.0.
    assert row["cumulative_delta"] == pytest.approx(5.0)


def test_daily_balance_is_preliminary_flag_correct():
    """Mixed: two reconciled rows + one synthesized row → flag mirrors
    the row's provenance."""
    e = _engine_db()
    _daily(e, date(2026, 5, 12), equity=1000.0)
    _daily(e, date(2026, 5, 13), equity=1010.0)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 12), today=date(2026, 5, 14)
    )
    assert list(df["is_preliminary"]) == [False, False, True]


# ── get_open_positions_unrealized_pnl_usd ────────────────────────────
# Live (~60s freshness) open-exposure read used to synthesize today's
# unrealized_pnl_usd. SUM((latest_price - entry_price) * qty) joined per
# position_id against the most-recent price_snapshots row. Positions
# without a snapshot are skipped (they contribute 0).


def test_unrealized_pnl_zero_when_no_open_positions():
    e = _engine_db()
    assert q.get_open_positions_unrealized_pnl_usd(e) == 0.0


def test_unrealized_pnl_sums_open_positions_with_latest_snapshot():
    e = _engine_db()
    _pos(e, "a", "AUSDT", "entry_filled", entry_price=100.0, qty=2.0, peak_price=110.0)
    _pos(e, "b", "BUSDT", "trailing_active", entry_price=50.0, qty=4.0, peak_price=55.0)
    _snap(e, "a", 110.0)
    _snap(e, "b", 55.0)
    # 'trailing_active' is technically "open" but the helper's filter
    # is documented as entry_filled / entry_pending only (per spec). It
    # should NOT contribute. Adjust below if spec widens.
    val = q.get_open_positions_unrealized_pnl_usd(e)
    assert val == pytest.approx((110 - 100) * 2)  # 20.0


def test_unrealized_pnl_uses_latest_snapshot_per_position():
    e = _engine_db()
    _pos(e, "a", "AUSDT", "entry_filled", entry_price=100.0, qty=2.0, peak_price=120.0)
    _snap(e, "a", 110.0, ts=datetime(2026, 5, 14, 8, 0))
    _snap(e, "a", 120.0, ts=datetime(2026, 5, 14, 9, 0))  # latest
    _snap(e, "a", 115.0, ts=datetime(2026, 5, 14, 8, 30))
    val = q.get_open_positions_unrealized_pnl_usd(e)
    assert val == pytest.approx((120 - 100) * 2)  # 40.0


def test_unrealized_pnl_excludes_closed_positions():
    e = _engine_db()
    _pos(e, "c", "CUSDT", "exit_filled", entry_price=100.0, qty=2.0, peak_price=120.0)
    _snap(e, "c", 120.0)
    assert q.get_open_positions_unrealized_pnl_usd(e) == 0.0


def test_unrealized_pnl_excludes_positions_without_snapshots():
    """Open position with no price_snapshots row → JOIN drops it, the
    function returns 0.0 (not NaN, not a crash)."""
    e = _engine_db()
    _pos(e, "a", "AUSDT", "entry_filled", entry_price=100.0, qty=2.0, peak_price=100.0)
    assert q.get_open_positions_unrealized_pnl_usd(e) == 0.0


def test_unrealized_pnl_handles_negative_drawdown():
    e = _engine_db()
    _pos(e, "a", "AUSDT", "entry_filled", entry_price=100.0, qty=3.0, peak_price=100.0)
    _snap(e, "a", 95.0)
    val = q.get_open_positions_unrealized_pnl_usd(e)
    assert val == pytest.approx((95 - 100) * 3)  # -15.0


def test_unrealized_pnl_includes_entry_pending():
    """entry_pending positions have an intended limit price as entry_price;
    they count toward today's open exposure until the order fills or
    cancels."""
    e = _engine_db()
    _pos(e, "p", "PUSDT", "entry_pending", entry_price=100.0, qty=1.0, peak_price=100.0)
    _snap(e, "p", 105.0)
    val = q.get_open_positions_unrealized_pnl_usd(e)
    assert val == pytest.approx(5.0)


# ──────────────────────────────────────────────────────────────────────
# Baseline-aware position filtering — fix-daily-balance-baseline-awareness
#
# The daily balance table used to filter its DATE ROWS by baseline but
# sum realized/unrealized across ALL positions, polluting post-baseline
# strategy attribution with pre-baseline contributions. This branch
# moves filtering to the position level: realized/unrealized per row
# recomputed from positions filtered by entry_date >= baseline_date.
# Pre-baseline open positions become invisible to the per-row metrics
# but are surfaced via a new helper for the dashboard explainer.
# ──────────────────────────────────────────────────────────────────────


def test_daily_balance_filters_unrealized_to_post_baseline_positions():
    """Two open positions on baseline_date+1: one pre-baseline (entry
    before baseline_date) and one post-baseline. The row's unrealized
    must include only the post-baseline contribution.
    """
    e = _engine_db()
    baseline = date(2026, 5, 14)
    row_date = date(2026, 5, 14)
    _daily(e, row_date, equity=10000.0, realized=0.0, unrealized=999.0)
    # Pre-baseline open: entered 5/12, mark $90 from $100 entry, qty 5 → -$50
    _pos(e, "pre1", "PREUSDT", "entry_filled",
         entry_date=date(2026, 5, 12),
         entry_price=100.0, qty=5.0, peak_price=100.0,
         updated_at=datetime(2026, 5, 12, 9, 0))
    _snap(e, "pre1", 90.0, ts=datetime(2026, 5, 14, 23, 0))
    # Post-baseline open: entered 5/14, mark $11 from $10 entry, qty 3 → +$3
    _pos(e, "post1", "POSTUSDT", "entry_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=3.0, peak_price=11.0,
         updated_at=datetime(2026, 5, 14, 9, 0))
    _snap(e, "post1", 11.0, ts=datetime(2026, 5, 14, 23, 0))

    df = q.get_daily_balance_since_baseline(
        e, since=baseline, today=row_date, baseline_date=baseline,
    )
    assert len(df) == 1
    # Only post-baseline contributes: (11-10)*3 = +3.0. Pre-baseline (-$50) excluded.
    assert df.iloc[0]["unrealized_pnl_usd"] == pytest.approx(3.0)


def test_daily_balance_filters_realized_to_post_baseline_closures():
    """Two closures on the row date: one pre-baseline entry (excluded)
    and one post-baseline entry (included). Realized must reflect only
    the post-baseline closure.
    """
    e = _engine_db()
    baseline = date(2026, 5, 14)
    row_date = date(2026, 5, 14)
    _daily(e, row_date, equity=10000.0, realized=99.0, unrealized=0.0)
    # Pre-baseline closure on row_date: realized +$50 (must be excluded)
    _pos(e, "pre_closed", "PRECLOSED", "exit_filled",
         entry_date=date(2026, 5, 12),
         entry_price=100.0, qty=5.0, peak_price=110.0,
         exit_price=110.0, realized_pnl_usd=50.0,
         updated_at=datetime(2026, 5, 14, 9, 0))
    # Post-baseline closure on row_date: realized +$7 (included)
    _pos(e, "post_closed", "POSTCLOSED", "exit_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=1.0, peak_price=17.0,
         exit_price=17.0, realized_pnl_usd=7.0,
         updated_at=datetime(2026, 5, 14, 10, 0))

    df = q.get_daily_balance_since_baseline(
        e, since=baseline, today=row_date, baseline_date=baseline,
    )
    assert df.iloc[0]["realized_pnl_usd"] == pytest.approx(7.0)


def test_daily_balance_cumulative_delta_excludes_pre_baseline_realized():
    """cumulative_delta is the running sum of post-baseline realized P&L.
    A *pre-baseline* position's realized P&L must NOT leak into it (the
    realized column already filters entry_date >= baseline), while a
    post-baseline closure does count.

    Concretely:
      Pre-baseline position (entry 5/12 < baseline 5/14) closes 5/15 with
      $50 realized — excluded. Post-baseline position (entry 5/14) closes
      5/15 with $30 realized — included. So cumulative_delta = 0.0 on 5/14
      (no post-baseline closures) and 30.0 on 5/15. The pre-baseline $50
      (and the wallet bump it caused) is invisible to the strategy curve.
    """
    e = _engine_db()
    baseline = date(2026, 5, 14)
    _daily(e, date(2026, 5, 14), equity=10000.0)
    _daily(e, date(2026, 5, 15), equity=10080.0)  # +50 pre + 30 post
    # Pre-baseline closure on 5/15 — excluded from realized cumsum.
    _pos(e, "pre1", "PREUSDT", "exit_filled",
         entry_date=date(2026, 5, 12),
         entry_price=100.0, qty=10.0, peak_price=105.0,
         exit_price=105.0, realized_pnl_usd=50.0,
         updated_at=datetime(2026, 5, 15, 9, 0))
    # Post-baseline closure on 5/15 — counted.
    _pos(e, "post1", "POSTUSDT", "exit_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=10.0, peak_price=13.0,
         exit_price=13.0, realized_pnl_usd=30.0,
         updated_at=datetime(2026, 5, 15, 9, 0))

    df = q.get_daily_balance_since_baseline(
        e, since=baseline, today=date(2026, 5, 15), baseline_date=baseline,
    )
    assert df.iloc[0]["cumulative_delta"] == pytest.approx(0.0)
    assert df.iloc[1]["cumulative_delta"] == pytest.approx(30.0)


def test_daily_balance_realized_comes_from_positions_not_daily_pnl():
    """The strip's realized_pnl_usd (and hence cumulative_delta) is
    recomputed from the positions table, NOT passed through from
    daily_pnl.realized_pnl_usd. With no positions seeded, both are 0.0
    even though daily_pnl carries a realized=20.0 value — confirming the
    daily_pnl realized column does not feed the strip.
    """
    e = _engine_db()
    _daily(e, date(2026, 5, 14), equity=1000.0)
    _daily(e, date(2026, 5, 15), equity=1020.0, realized=20.0, unrealized=0.0)
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 14), today=date(2026, 5, 15),
        baseline_date=date(2026, 5, 14),
    )
    # No positions seeded → filtered realized = 0.0 on each row (daily_pnl's
    # realized=20.0 is ignored — the strip reads positions, not daily_pnl).
    assert df.iloc[0]["realized_pnl_usd"] == pytest.approx(0.0)
    assert df.iloc[1]["realized_pnl_usd"] == pytest.approx(0.0)
    # cumulative_delta is the realized cumsum → 0.0 throughout.
    assert df.iloc[0]["cumulative_delta"] == pytest.approx(0.0)
    assert df.iloc[1]["cumulative_delta"] == pytest.approx(0.0)


def test_daily_balance_synthesized_today_filters_open_positions_by_baseline():
    """Today's preliminary row's unrealized must exclude pre-baseline
    open positions, mirroring the historical-row contract.
    """
    e = _engine_db()
    baseline = date(2026, 5, 14)
    _daily(e, date(2026, 5, 14), equity=10000.0)
    # Pre-baseline open: ignored
    _pos(e, "pre1", "PREUSDT", "entry_filled",
         entry_date=date(2026, 5, 12),
         entry_price=100.0, qty=5.0, peak_price=100.0,
         updated_at=datetime(2026, 5, 14, 9, 0))
    _snap(e, "pre1", 90.0, ts=datetime(2026, 5, 15, 9, 0))  # -$50 pre
    # Post-baseline open: counted
    _pos(e, "post1", "POSTUSDT", "entry_filled",
         entry_date=date(2026, 5, 15),
         entry_price=10.0, qty=2.0, peak_price=10.0,
         updated_at=datetime(2026, 5, 15, 9, 0))
    _snap(e, "post1", 13.0, ts=datetime(2026, 5, 15, 9, 0))  # +$6 post

    df = q.get_daily_balance_since_baseline(
        e, since=baseline, today=date(2026, 5, 15), baseline_date=baseline,
    )
    today_row = df.iloc[-1]
    assert bool(today_row["is_preliminary"]) is True
    assert today_row["unrealized_pnl_usd"] == pytest.approx(6.0)


def test_get_pre_baseline_open_summary_returns_scalar_metrics():
    """The new helper exposes the count and unrealized P&L of
    pre-baseline open positions for the dashboard explainer below the
    daily-balance table.
    """
    e = _engine_db()
    baseline = date(2026, 5, 14)
    _pos(e, "pre1", "PREUSDT", "entry_filled",
         entry_date=date(2026, 5, 12),
         entry_price=100.0, qty=5.0, peak_price=100.0,
         updated_at=datetime(2026, 5, 12, 9, 0))
    _snap(e, "pre1", 90.0, ts=datetime(2026, 5, 15, 9, 0))  # -$50
    _pos(e, "pre2", "PRE2USDT", "entry_filled",
         entry_date=date(2026, 5, 13),
         entry_price=20.0, qty=10.0, peak_price=20.0,
         updated_at=datetime(2026, 5, 13, 9, 0))
    _snap(e, "pre2", 18.0, ts=datetime(2026, 5, 15, 9, 0))  # -$20
    # Post-baseline open — must be excluded from the pre-baseline summary
    _pos(e, "post1", "POSTUSDT", "entry_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=1.0, peak_price=10.0,
         updated_at=datetime(2026, 5, 14, 9, 0))
    _snap(e, "post1", 11.0, ts=datetime(2026, 5, 15, 9, 0))

    summary = q.get_pre_baseline_open_summary(e, baseline_date=baseline)
    assert summary["n_pre_baseline_open_positions"] == 2
    assert summary["pre_baseline_unrealized_pnl_usd"] == pytest.approx(-70.0)
    assert summary["pre_baseline_cost_basis_usd"] == pytest.approx(700.0)


def test_get_pre_baseline_open_summary_empty_when_no_pre_baseline_positions():
    """Edge case: no pre-baseline open positions → zero scalars, not None
    or NaN. The dashboard renders without conditional fallback."""
    e = _engine_db()
    baseline = date(2026, 5, 14)
    _pos(e, "post1", "POSTUSDT", "entry_filled",
         entry_date=date(2026, 5, 14),
         entry_price=10.0, qty=1.0, peak_price=10.0,
         updated_at=datetime(2026, 5, 14, 9, 0))
    summary = q.get_pre_baseline_open_summary(e, baseline_date=baseline)
    assert summary["n_pre_baseline_open_positions"] == 0
    assert summary["pre_baseline_unrealized_pnl_usd"] == 0.0
    assert summary["pre_baseline_cost_basis_usd"] == 0.0


def test_daily_balance_baseline_date_defaults_to_since():
    """baseline_date is optional; when omitted it defaults to `since` so
    callers that supply only `since` get baseline-aware filtering
    automatically (the common case: dashboard calls
    paper_baseline_date() once and threads it through)."""
    e = _engine_db()
    _daily(e, date(2026, 5, 14), equity=10000.0)
    _pos(e, "pre", "PRE", "entry_filled",
         entry_date=date(2026, 5, 12), entry_price=100.0, qty=5.0,
         peak_price=100.0, updated_at=datetime(2026, 5, 12, 9, 0))
    _snap(e, "pre", 90.0, ts=datetime(2026, 5, 14, 23, 0))
    _pos(e, "post", "POST", "entry_filled",
         entry_date=date(2026, 5, 14), entry_price=10.0, qty=2.0,
         peak_price=10.0, updated_at=datetime(2026, 5, 14, 9, 0))
    _snap(e, "post", 12.0, ts=datetime(2026, 5, 14, 23, 0))

    # No baseline_date — must default to since=5/14
    df = q.get_daily_balance_since_baseline(
        e, since=date(2026, 5, 14), today=date(2026, 5, 14),
    )
    # Only the post-baseline (2*1.0 * 2 = 4) contributes
    assert df.iloc[0]["unrealized_pnl_usd"] == pytest.approx(4.0)
