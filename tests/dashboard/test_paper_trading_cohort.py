"""Unit tests for the rebuilt Paper Trading positions view (paper-tab-overhaul).

Three pure helpers on ``dashboard.services.queries`` back the rebuilt view:

* ``get_paper_today_cohort`` — today's opened cohort (entry_filled+ that
  reached the market), open-first then closed by exit time desc, with
  per-position Opened $, PnL $, PnL % (gross; unrealized for open rows).
* ``get_paper_position_snapshots`` — per-position price series, downsampled
  to <= max_points while preserving the global min & max.
* ``build_position_chart_frame`` / ``position_is_armed`` — the per-row chart
  geometry: entry line, plus an activation line (never-armed) or a stepwise
  trail-stop line (armed), matching engine SPEC §3.2.

A synthetic engine DuckDB is built in memory (mirrors the live schema,
including ``closed_at``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb
import numpy as np
import pandas as pd
import pytest

from dashboard.services import queries as q

TODAY = date(2026, 5, 31)
NOON = datetime(2026, 5, 31, 12, 0, 0)


def _engine_db() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE positions (
            id VARCHAR, symbol VARCHAR, entry_date DATE, entry_price DOUBLE,
            qty DOUBLE, peak_price DOUBLE, current_state VARCHAR,
            created_at TIMESTAMP, updated_at TIMESTAMP, closed_at TIMESTAMP,
            exit_price DOUBLE, realized_pnl_usd DOUBLE
        )""")
    conn.execute("""
        CREATE TABLE price_snapshots (
            position_id VARCHAR NOT NULL,
            timestamp   TIMESTAMP NOT NULL,
            price       DOUBLE NOT NULL
        )""")
    return conn


def _pos(conn, id, symbol, state, *, entry_date=TODAY, entry_price=None, qty=None,
         peak_price=None, created_at=None, closed_at=None, exit_price=None,
         realized_pnl_usd=None):
    conn.execute(
        "INSERT INTO positions (id, symbol, entry_date, entry_price, qty, "
        "peak_price, current_state, created_at, updated_at, closed_at, "
        "exit_price, realized_pnl_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [id, symbol, entry_date, entry_price, qty, peak_price, state,
         created_at or NOON, closed_at or NOON, closed_at, exit_price,
         realized_pnl_usd],
    )


def _snap(conn, position_id, price, ts):
    conn.execute(
        "INSERT INTO price_snapshots (position_id, timestamp, price) VALUES (?,?,?)",
        [position_id, ts, price],
    )


# ── get_paper_today_cohort: membership ───────────────────────────────

def test_cohort_excludes_failed_and_cancelled():
    e = _engine_db()
    _pos(e, "ok", "OKUSDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=1.0)
    _snap(e, "ok", 1.0, NOON)
    _pos(e, "f", "FUSDT", "failed")
    _pos(e, "c", "CXUSDT", "cancelled")
    df = q.get_paper_today_cohort(e, today=TODAY)
    assert set(df["symbol"]) == {"OKUSDT"}


def test_cohort_only_todays_entry_date():
    e = _engine_db()
    _pos(e, "t", "TODAYUSDT", "exit_filled", entry_price=1.0, qty=1.0, peak_price=1.0,
         exit_price=1.1, realized_pnl_usd=0.1, closed_at=NOON)
    _pos(e, "y", "YESTUSDT", "exit_filled", entry_date=TODAY - timedelta(days=1),
         entry_price=1.0, qty=1.0, peak_price=1.0, exit_price=1.1,
         realized_pnl_usd=0.1, closed_at=NOON - timedelta(days=1))
    df = q.get_paper_today_cohort(e, today=TODAY)
    assert set(df["symbol"]) == {"TODAYUSDT"}


def test_cohort_includes_open_and_closed_states():
    e = _engine_db()
    _pos(e, "o1", "O1USDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=1.0)
    _snap(e, "o1", 1.0, NOON)
    _pos(e, "o2", "O2USDT", "trailing_active", entry_price=1.0, qty=1.0, peak_price=1.2)
    _snap(e, "o2", 1.2, NOON)
    _pos(e, "cl", "CLUSDT", "exit_filled", entry_price=1.0, qty=1.0, peak_price=1.1,
         exit_price=1.1, realized_pnl_usd=0.1, closed_at=NOON)
    df = q.get_paper_today_cohort(e, today=TODAY)
    assert set(df["symbol"]) == {"O1USDT", "O2USDT", "CLUSDT"}


# ── get_paper_today_cohort: ordering ─────────────────────────────────

def test_cohort_orders_open_first_then_closed_by_exit_desc():
    e = _engine_db()
    # two closed at different exit times, one open
    _pos(e, "cl_early", "EARLYUSDT", "exit_filled", entry_price=1.0, qty=1.0,
         peak_price=1.1, exit_price=1.1, realized_pnl_usd=0.1,
         closed_at=NOON - timedelta(hours=3))
    _pos(e, "cl_late", "LATEUSDT", "exit_filled", entry_price=1.0, qty=1.0,
         peak_price=1.1, exit_price=1.1, realized_pnl_usd=0.1,
         closed_at=NOON - timedelta(hours=1))
    _pos(e, "open", "OPENUSDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=1.0)
    _snap(e, "open", 1.0, NOON)
    df = q.get_paper_today_cohort(e, today=TODAY)
    # open first, then closed newest-first
    assert list(df["symbol"]) == ["OPENUSDT", "LATEUSDT", "EARLYUSDT"]
    assert list(df["is_open"]) == [True, False, False]


# ── get_paper_today_cohort: dollar / pnl columns ─────────────────────

def test_cohort_opened_usd_is_entry_price_times_qty():
    e = _engine_db()
    _pos(e, "cl", "CLUSDT", "exit_filled", entry_price=0.2454, qty=2716.0,
         peak_price=0.2534, exit_price=0.2509, realized_pnl_usd=14.938, closed_at=NOON)
    df = q.get_paper_today_cohort(e, today=TODAY)
    row = df.iloc[0]
    assert row["opened_usd"] == pytest.approx(0.2454 * 2716.0)


def test_cohort_closed_pnl_from_realized_column_and_pct():
    e = _engine_db()
    _pos(e, "cl", "CLUSDT", "exit_filled", entry_price=0.2454, qty=2716.0,
         peak_price=0.2534, exit_price=0.2509, realized_pnl_usd=14.938, closed_at=NOON)
    df = q.get_paper_today_cohort(e, today=TODAY)
    row = df.iloc[0]
    opened = 0.2454 * 2716.0
    assert row["pnl_usd"] == pytest.approx(14.938)
    assert row["pnl_pct"] == pytest.approx(14.938 / opened * 100.0)
    assert row["exit_price"] == pytest.approx(0.2509)


def test_cohort_open_pnl_is_unrealized_from_latest_snapshot():
    e = _engine_db()
    _pos(e, "o", "OUSDT", "entry_filled", entry_price=0.4637, qty=1437.0, peak_price=0.4675)
    _snap(e, "o", 0.4600, NOON - timedelta(hours=2))
    _snap(e, "o", 0.4660, NOON)  # latest mark
    df = q.get_paper_today_cohort(e, today=TODAY)
    row = df.iloc[0]
    assert row["is_open"] is True or bool(row["is_open"]) is True
    expected = (0.4660 - 0.4637) * 1437.0
    assert row["pnl_usd"] == pytest.approx(expected)
    opened = 0.4637 * 1437.0
    assert row["pnl_pct"] == pytest.approx(expected / opened * 100.0)


def test_cohort_open_without_snapshot_has_nan_pnl():
    e = _engine_db()
    _pos(e, "o", "OUSDT", "entry_filled", entry_price=0.5, qty=100.0, peak_price=0.5)
    df = q.get_paper_today_cohort(e, today=TODAY)
    row = df.iloc[0]
    assert np.isnan(row["pnl_usd"])
    assert np.isnan(row["pnl_pct"])
    # opened_usd is still well-defined
    assert row["opened_usd"] == pytest.approx(50.0)


def test_cohort_empty_returns_empty_frame_with_columns():
    e = _engine_db()
    df = q.get_paper_today_cohort(e, today=TODAY)
    assert df.empty
    for col in ("id", "symbol", "is_open", "entry_price", "exit_price",
                "opened_usd", "pnl_usd", "pnl_pct"):
        assert col in df.columns


# ── get_paper_position_snapshots: downsampling ───────────────────────

def test_snapshots_returns_all_when_under_limit():
    e = _engine_db()
    _pos(e, "p", "PUSDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=1.0)
    base = NOON
    for i in range(10):
        _snap(e, "p", 1.0 + i * 0.01, base + timedelta(minutes=i))
    df = q.get_paper_position_snapshots(e, "p", max_points=400)
    assert len(df) == 10
    assert list(df.columns) == ["timestamp", "price"]
    # ordered ascending by timestamp
    assert df["timestamp"].is_monotonic_increasing


def test_snapshots_downsamples_and_preserves_min_max():
    e = _engine_db()
    _pos(e, "p", "PUSDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=2.0)
    base = NOON
    n = 5000
    # sawtooth-ish with a unique global min at i=1234 and global max at i=4321
    for i in range(n):
        price = 1.0 + (i % 7) * 0.001
        if i == 1234:
            price = 0.10  # global min
        if i == 4321:
            price = 9.99  # global max
        _snap(e, "p", price, base + timedelta(seconds=i))
    df = q.get_paper_position_snapshots(e, "p", max_points=400)
    assert len(df) <= 400
    assert df["price"].min() == pytest.approx(0.10)
    assert df["price"].max() == pytest.approx(9.99)
    # endpoints preserved
    assert df["timestamp"].is_monotonic_increasing


def test_snapshots_empty_when_none():
    e = _engine_db()
    _pos(e, "p", "PUSDT", "entry_filled", entry_price=1.0, qty=1.0, peak_price=1.0)
    df = q.get_paper_position_snapshots(e, "p")
    assert df.empty
    assert list(df.columns) == ["timestamp", "price"]


# ── position_is_armed ────────────────────────────────────────────────

def test_is_armed_true_when_peak_clears_activation():
    # peak 1.2 vs entry 1.0, activation 0.01 → threshold 1.01 → armed
    assert q.position_is_armed(entry_price=1.0, peak_price=1.2, activation_pct=0.01) is True


def test_is_armed_false_when_peak_below_activation():
    assert q.position_is_armed(entry_price=1.0, peak_price=1.005, activation_pct=0.01) is False


def test_is_armed_false_on_missing_prices():
    assert q.position_is_armed(entry_price=None, peak_price=1.2, activation_pct=0.01) is False
    assert q.position_is_armed(entry_price=1.0, peak_price=None, activation_pct=0.01) is False


# ── build_position_chart_frame ───────────────────────────────────────

def _snap_df(prices, base=NOON):
    return pd.DataFrame({
        "timestamp": [base + timedelta(minutes=i) for i in range(len(prices))],
        "price": prices,
    })


def test_chart_frame_not_armed_shows_activation_line():
    snaps = _snap_df([1.0, 1.005, 1.004])  # peak 1.005 < 1.01 → not armed
    df = q.build_position_chart_frame(
        snaps, entry_price=1.0, peak_price=1.005, trail_pct=0.3, activation_pct=0.01
    )
    assert list(df.columns) == ["timestamp", "price", "entry", "exit_ref"]
    assert (df["entry"] == 1.0).all()
    # activation line is the flat entry*(1+activation_pct)
    assert df["exit_ref"].nunique() == 1
    assert df["exit_ref"].iloc[0] == pytest.approx(1.0 * 1.01)


def test_chart_frame_armed_shows_stepwise_trail_line():
    # prices rise to a peak then fall → armed; trail = cummax - 0.3*(cummax-entry)
    snaps = _snap_df([10.0, 12.0, 11.0, 13.0])
    df = q.build_position_chart_frame(
        snaps, entry_price=10.0, peak_price=13.0, trail_pct=0.3, activation_pct=0.01
    )
    cummax = pd.Series([10.0, 12.0, 12.0, 13.0])
    expected = cummax - 0.3 * (cummax - 10.0)
    assert list(df["exit_ref"]) == pytest.approx(list(expected))
    assert (df["entry"] == 10.0).all()


def test_chart_frame_empty_snapshots_returns_empty_with_columns():
    df = q.build_position_chart_frame(
        _snap_df([]), entry_price=1.0, peak_price=1.0, trail_pct=0.3, activation_pct=0.01
    )
    assert df.empty
    assert list(df.columns) == ["timestamp", "price", "entry", "exit_ref"]
