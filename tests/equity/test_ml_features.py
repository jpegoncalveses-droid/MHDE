"""Unit tests for ml/features.py — equity ML features."""
from __future__ import annotations

from datetime import date, timedelta

import math

from ml.features import compute_features


def _seed_company(conn, ticker, sector="Information Technology", market_cap=100e9):
    conn.execute(
        "INSERT INTO companies (ticker, company_name, sector, is_active, is_etf, "
        "market_cap) VALUES (?, ?, ?, ?, ?, ?)",
        [ticker, f"{ticker} Inc", sector, True, False, market_cap],
    )


def _insert_prices(conn, rows):
    conn.executemany(
        "INSERT INTO prices_daily (id, ticker, trade_date, open, high, low, close, "
        "volume, adjusted_close, source, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["id"], r["ticker"], r["trade_date"], r["open"], r["high"], r["low"],
          r["close"], r["volume"], r["adjusted_close"], r["source"], r["run_id"])
         for r in rows],
    )


def test_compute_features_writes_warmed_up_rows(temp_db, synthetic_prices_equity):
    _seed_company(temp_db, "AAPL")
    rows = synthetic_prices_equity("AAPL", num_days=250, start_price=150)
    _insert_prices(temp_db, rows)

    n = compute_features(temp_db)
    assert n > 0
    # 200d MA needs 200 lookback so warmed-up tail < total bars
    assert n <= len(rows)


def test_compute_features_empty_universe(temp_db):
    n = compute_features(temp_db)
    assert n == 0


def test_compute_features_skips_non_universe_tickers(
    temp_db, synthetic_prices_equity
):
    """small-cap tickers shouldn't be in ml_features."""
    _seed_company(temp_db, "BIGCAP", market_cap=100e9)
    _seed_company(temp_db, "SMALL", market_cap=1e9)
    _insert_prices(temp_db, synthetic_prices_equity("BIGCAP", num_days=210))
    _insert_prices(temp_db, synthetic_prices_equity("SMALL", num_days=210))

    compute_features(temp_db)
    tickers = {r[0] for r in temp_db.execute(
        "SELECT DISTINCT ticker FROM ml_features"
    ).fetchall()}
    assert "BIGCAP" in tickers
    assert "SMALL" not in tickers


def test_lookahead_bias_features_dont_change(temp_db, synthetic_prices_equity):
    _seed_company(temp_db, "AAPL")
    rows = synthetic_prices_equity("AAPL", num_days=220)
    _insert_prices(temp_db, rows)
    compute_features(temp_db)

    target = rows[210]["trade_date"]
    before = temp_db.execute(
        "SELECT return_5d, rsi_14d, return_20d FROM ml_features "
        "WHERE ticker = 'AAPL' AND trade_date = ?", [target]
    ).fetchone()

    extra = synthetic_prices_equity(
        "AAPL", num_days=10, start_date=rows[-1]["trade_date"] + timedelta(days=1),
        seed=99,
    )
    _insert_prices(temp_db, extra)
    compute_features(temp_db)

    after = temp_db.execute(
        "SELECT return_5d, rsi_14d, return_20d FROM ml_features "
        "WHERE ticker = 'AAPL' AND trade_date = ?", [target]
    ).fetchone()

    assert before == after, (
        f"feature(s) at {target} changed when later data was added "
        f"— lookahead bias. before={before}, after={after}"
    )


def test_compute_features_finite_values(temp_db, synthetic_prices_equity):
    _seed_company(temp_db, "AAPL")
    rows = synthetic_prices_equity("AAPL", num_days=220)
    _insert_prices(temp_db, rows)
    compute_features(temp_db)

    cols = ["return_5d", "rsi_14d", "realized_vol_20d", "drawdown_from_52w_high"]
    rows = temp_db.execute(
        f"SELECT {', '.join(cols)} FROM ml_features ORDER BY trade_date DESC LIMIT 30"
    ).fetchall()
    for row in rows:
        for v in row:
            if v is not None:
                assert math.isfinite(v), f"non-finite value {v}"
