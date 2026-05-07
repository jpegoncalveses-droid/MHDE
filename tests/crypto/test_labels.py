"""Unit tests for crypto/ml/labels.py."""
from __future__ import annotations

from datetime import date, timedelta

from crypto.ml.labels import compute_labels


def _insert_universe(conn, symbols):
    for i, sym in enumerate(symbols):
        conn.execute(
            "INSERT INTO crypto_universe (symbol, base_asset, avg_daily_volume_30d, "
            "rank_by_volume, is_active) VALUES (?, ?, ?, ?, ?)",
            [sym, sym.replace("USDT", ""), 1e9 - i * 1e6, i + 1, True],
        )


def _insert_prices(conn, rows):
    conn.executemany(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, close, "
        "volume, trades, taker_buy_volume, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["symbol"], r["trade_date"], r["open"], r["high"], r["low"], r["close"],
          r["volume"], r["trades"], r["taker_buy_volume"], r["source"]) for r in rows],
    )


def test_compute_labels_writes_rows(temp_db, synthetic_prices_crypto):
    _insert_universe(temp_db, ["BTCUSDT"])
    rows = synthetic_prices_crypto("BTCUSDT", num_days=30)
    _insert_prices(temp_db, rows)

    n = compute_labels(temp_db)
    assert n > 0
    db_count = temp_db.execute("SELECT COUNT(*) FROM crypto_ml_labels").fetchone()[0]
    assert db_count == n


def test_compute_labels_empty_universe(temp_db):
    """No symbols in crypto_universe → 0 labels."""
    n = compute_labels(temp_db)
    assert n == 0


def test_compute_labels_skips_rows_without_forward_data(
    temp_db, synthetic_prices_crypto
):
    """The last 1-10 days lack enough forward data; query filters
    `WHERE close_1d IS NOT NULL` so the most recent day is dropped."""
    _insert_universe(temp_db, ["BTCUSDT"])
    rows = synthetic_prices_crypto("BTCUSDT", num_days=20)
    _insert_prices(temp_db, rows)

    compute_labels(temp_db)
    label_dates = {r[0] for r in temp_db.execute(
        "SELECT trade_date FROM crypto_ml_labels"
    ).fetchall()}
    price_dates = {r["trade_date"] for r in rows}
    # Latest price date should NOT have a label (no forward data).
    assert max(price_dates) not in label_dates


def test_binary_labels_match_continuous_returns(
    temp_db, synthetic_prices_crypto
):
    """label_5d_10pct must equal (fwd_max_return_5d >= 0.10)."""
    _insert_universe(temp_db, ["BTCUSDT"])
    rows = synthetic_prices_crypto("BTCUSDT", num_days=30, volatility=0.06)
    _insert_prices(temp_db, rows)

    compute_labels(temp_db)
    rows = temp_db.execute(
        "SELECT fwd_max_return_5d, label_5d_10pct, "
        "       fwd_max_return_10d, label_10d_20pct "
        "FROM crypto_ml_labels WHERE fwd_max_return_5d IS NOT NULL"
    ).fetchall()
    for max5, lab5, max10, lab10 in rows:
        assert lab5 == (max5 >= 0.10)
        if max10 is not None:
            assert lab10 == (max10 >= 0.20)


def test_compute_labels_idempotent(temp_db, synthetic_prices_crypto):
    """compute_labels does DELETE+INSERT — re-running gives same row count."""
    _insert_universe(temp_db, ["ETHUSDT"])
    rows = synthetic_prices_crypto("ETHUSDT", num_days=20)
    _insert_prices(temp_db, rows)

    n1 = compute_labels(temp_db)
    n2 = compute_labels(temp_db)
    assert n1 == n2


def test_compute_labels_skips_inactive_symbols(temp_db, synthetic_prices_crypto):
    """is_active=FALSE rows in crypto_universe are excluded."""
    temp_db.execute(
        "INSERT INTO crypto_universe (symbol, base_asset, rank_by_volume, is_active) "
        "VALUES ('DELISTED', 'DELISTED', 999, false)"
    )
    rows = synthetic_prices_crypto("DELISTED", num_days=20)
    _insert_prices(temp_db, rows)

    n = compute_labels(temp_db)
    # No active symbols — no labels.
    assert n == 0
