"""Integration test for run_intraday_replay over synthetic temp DuckDBs.

Exercises the full wiring: prediction load → entry resolution (prediction_date
+ 1 @ 00:45) → 1-minute walk → cost application → traded-subset filter
(post-parabolic top-6) → per-bin aggregation. No network; tiny synthetic data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

from crypto.execution.backtest import intraday_klines as ik
from crypto.execution.backtest.intraday_replay import DeployedEntry, run_intraday_replay


def _mhde_db(tmp_path):
    conn = duckdb.connect(str(tmp_path / "mhde.duckdb"))
    conn.execute("""
        CREATE TABLE crypto_ml_predictions (
            symbol VARCHAR, prediction_date DATE, model_id VARCHAR,
            horizon VARCHAR, predicted_probability DOUBLE
        )""")
    conn.execute("""
        CREATE TABLE crypto_ml_features (
            symbol VARCHAR, trade_date DATE,
            drawdown_from_90d_high DOUBLE, return_60d DOUBLE, return_5d DOUBLE
        )""")
    conn.execute("""
        CREATE TABLE crypto_prices_daily (
            symbol VARCHAR, trade_date DATE, close DOUBLE, volume DOUBLE
        )""")
    conn.execute("""
        CREATE TABLE crypto_funding_rates (
            symbol VARCHAR, funding_time TIMESTAMP, funding_rate DOUBLE
        )""")
    return conn


def _minute_bars(conn, symbol, start, n, *, ohlc):
    """Insert n 1-minute bars all with the same (o,h,l,c)."""
    o, h, lo, c = ohlc
    rows = []
    t = start
    for _ in range(n):
        rows.append({"open_time": t, "open": o, "high": h, "low": lo,
                     "close": c, "volume": 100.0})
        t = t + timedelta(minutes=1)
    ik.upsert_klines(conn, symbol, "1m", rows)


def test_driver_end_to_end(tmp_path):
    mhde = _mhde_db(tmp_path)
    research = ik.connect_research_db(str(tmp_path / "intraday.duckdb"))

    pred_date = date(2026, 2, 6)          # features day
    entry_day = date(2026, 2, 7)          # live entry day (+1)
    entry_anchor = datetime(2026, 2, 7, 0, 45, tzinfo=timezone.utc)

    # Two symbols predicted on the same features day.
    mhde.execute(
        "INSERT INTO crypto_ml_predictions VALUES "
        "('BTCUSDT', ?, 'crypto_10d_walkfold_2026_02', '10d', 0.82),"
        "('ETHUSDT', ?, 'crypto_10d_walkfold_2026_02', '10d', 0.55)",
        [pred_date, pred_date],
    )
    # Features (neither excluded by the post-parabolic filter).
    mhde.execute(
        "INSERT INTO crypto_ml_features VALUES "
        "('BTCUSDT', ?, -0.05, 0.2, 0.01),"
        "('ETHUSDT', ?, -0.05, 0.2, 0.01)",
        [pred_date, pred_date],
    )
    # Volume-rank source.
    for sym in ("BTCUSDT", "ETHUSDT"):
        mhde.execute(
            "INSERT INTO crypto_prices_daily VALUES (?, ?, 100.0, 1000000.0)",
            [sym, pred_date],
        )

    # BTC: a flat ride that hits the 10d time stop at 100.0 (gross 0).
    # We need bars from entry_anchor for the whole 10d horizon. Use a coarse
    # but valid minute series: 11 bars is enough since the time stop fires on
    # the last supplied bar within the horizon window.
    _minute_bars(research, "BTCUSDT", entry_anchor, 20, ohlc=(100.0, 100.5, 99.6, 100.0))
    # ETH: floors at −5% on the 2nd minute.
    _minute_bars(research, "ETHUSDT", entry_anchor, 1, ohlc=(100.0, 100.4, 99.8, 100.0))
    ik.upsert_klines(research, "ETHUSDT", "1m", [{
        "open_time": entry_anchor + timedelta(minutes=1),
        "open": 100.0, "high": 100.2, "low": 94.0, "close": 96.0, "volume": 50.0,
    }])

    report = run_intraday_replay(
        mhde, research, start_date=pred_date, end_date=pred_date,
        entry_rule=DeployedEntry(), top_n=6,
    )

    assert report.n_predictions == 2
    assert report.n_replayed == 2
    assert report.n_skipped == 0

    by_sym = {r.symbol: r for r in report.results}
    # ETH floored at 95 → gross −5%.
    assert by_sym["ETHUSDT"].exit_reason == "hard_floor"
    assert by_sym["ETHUSDT"].gross_return == pytest.approx(-0.05)
    # Net is gross minus costs (fees+slippage), so strictly below gross.
    assert by_sym["ETHUSDT"].net_return < by_sym["ETHUSDT"].gross_return
    # BTC time-stopped flat.
    assert by_sym["BTCUSDT"].exit_reason == "time"

    # Entry anchored to prediction_date + 1 @ 00:45.
    assert by_sym["BTCUSDT"].entry_time == entry_anchor

    # Both survive the filter and are top-6 → traded.
    assert all(r.traded for r in report.results)
    assert report.traded_stats["n"] == 2

    # Bins present for 0.8 (BTC) and 0.5 (ETH).
    bins = {b["bin"]: b for b in report.bins}
    assert set(bins) == {0.5, 0.8}


def test_driver_skips_when_no_entry_bar(tmp_path):
    mhde = _mhde_db(tmp_path)
    research = ik.connect_research_db(str(tmp_path / "intraday.duckdb"))
    pred_date = date(2026, 2, 6)
    mhde.execute(
        "INSERT INTO crypto_ml_predictions VALUES "
        "('BTCUSDT', ?, 'crypto_10d_walkfold_2026_02', '10d', 0.82)",
        [pred_date],
    )
    # Bars exist but NOT at the 00:45 anchor (only 01:00).
    ik.upsert_klines(research, "BTCUSDT", "1m", [{
        "open_time": datetime(2026, 2, 7, 1, 0, tzinfo=timezone.utc),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0,
    }])
    report = run_intraday_replay(
        mhde, research, start_date=pred_date, end_date=pred_date,
    )
    assert report.n_replayed == 0
    assert report.n_skipped == 1
    assert report.skipped[0][2] == "no_entry_bar"


def test_driver_excludes_post_parabolic_from_traded(tmp_path):
    mhde = _mhde_db(tmp_path)
    research = ik.connect_research_db(str(tmp_path / "intraday.duckdb"))
    pred_date = date(2026, 2, 6)
    entry_anchor = datetime(2026, 2, 7, 0, 45, tzinfo=timezone.utc)
    mhde.execute(
        "INSERT INTO crypto_ml_predictions VALUES "
        "('PARABUSDT', ?, 'crypto_10d_walkfold_2026_02', '10d', 0.95)",
        [pred_date],
    )
    # dd90 < -0.20 AND ret60 > 2.0 → post-parabolic excluded.
    mhde.execute(
        "INSERT INTO crypto_ml_features VALUES ('PARABUSDT', ?, -0.30, 3.0, 0.0)",
        [pred_date],
    )
    mhde.execute(
        "INSERT INTO crypto_prices_daily VALUES ('PARABUSDT', ?, 100.0, 1.0)",
        [pred_date],
    )
    _minute_bars(research, "PARABUSDT", entry_anchor, 5, ohlc=(100.0, 100.5, 99.6, 100.0))
    report = run_intraday_replay(
        mhde, research, start_date=pred_date, end_date=pred_date,
    )
    # Replayed (it has bars) but NOT in the traded subset.
    assert report.n_replayed == 1
    assert report.results[0].traded is False
    assert report.traded_stats["n"] == 0
