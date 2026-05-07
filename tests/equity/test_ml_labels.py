"""Unit tests for ml/labels.py — equity ML forward-return labels."""
from __future__ import annotations

from datetime import date, timedelta

from ml.labels import compute_labels


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


def test_compute_labels_writes_rows(temp_db, synthetic_prices_equity):
    _seed_company(temp_db, "AAPL")
    rows = synthetic_prices_equity("AAPL", num_days=40)
    _insert_prices(temp_db, rows)

    n = compute_labels(temp_db)
    assert n > 0
    db_count = temp_db.execute("SELECT COUNT(*) FROM ml_labels").fetchone()[0]
    assert db_count == n


def test_compute_labels_universe_filter_market_cap(temp_db, synthetic_prices_equity):
    """Tickers with market_cap < 10B are excluded from the ML universe."""
    _seed_company(temp_db, "BIGCAP", market_cap=100e9)
    _seed_company(temp_db, "SMALL", market_cap=1e9)  # below threshold
    _insert_prices(temp_db, synthetic_prices_equity("BIGCAP", num_days=30))
    _insert_prices(temp_db, synthetic_prices_equity("SMALL", num_days=30))

    compute_labels(temp_db)
    tickers = {r[0] for r in temp_db.execute(
        "SELECT DISTINCT ticker FROM ml_labels"
    ).fetchall()}
    assert "BIGCAP" in tickers
    assert "SMALL" not in tickers


def test_compute_labels_universe_filter_excludes_etfs(
    temp_db, synthetic_prices_equity
):
    _seed_company(temp_db, "STOCK", market_cap=50e9)
    temp_db.execute(
        "INSERT INTO companies (ticker, company_name, is_active, is_etf, market_cap, sector) "
        "VALUES ('SPY', 'SPY ETF', true, true, 500e9, 'ETF')"
    )
    _insert_prices(temp_db, synthetic_prices_equity("STOCK", num_days=30))
    _insert_prices(temp_db, synthetic_prices_equity("SPY", num_days=30))

    compute_labels(temp_db)
    tickers = {r[0] for r in temp_db.execute(
        "SELECT DISTINCT ticker FROM ml_labels"
    ).fetchall()}
    assert "STOCK" in tickers
    assert "SPY" not in tickers


def test_binary_labels_match_continuous(temp_db, synthetic_prices_equity):
    _seed_company(temp_db, "AAPL")
    rows = synthetic_prices_equity("AAPL", num_days=50, volatility=0.03)
    _insert_prices(temp_db, rows)

    compute_labels(temp_db)
    rows = temp_db.execute(
        "SELECT fwd_max_return_5d, label_5d_5pct, "
        "       fwd_max_return_20d, label_20d_10pct "
        "FROM ml_labels "
        "WHERE fwd_max_return_5d IS NOT NULL AND fwd_max_return_20d IS NOT NULL"
    ).fetchall()
    for max5, lab5, max20, lab20 in rows:
        assert lab5 == (max5 >= 0.05)
        assert lab20 == (max20 >= 0.10)


def test_compute_labels_idempotent(temp_db, synthetic_prices_equity):
    _seed_company(temp_db, "AAPL")
    _insert_prices(temp_db, synthetic_prices_equity("AAPL", num_days=30))

    n1 = compute_labels(temp_db)
    n2 = compute_labels(temp_db)
    assert n1 == n2


def test_compute_labels_empty_universe(temp_db):
    n = compute_labels(temp_db)
    assert n == 0
