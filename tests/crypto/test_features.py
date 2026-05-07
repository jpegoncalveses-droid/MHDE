"""Unit tests for crypto/ml/features.py."""
from __future__ import annotations

from datetime import date, timedelta

import math

from crypto.ml.features import compute_features


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


def test_compute_features_writes_warmed_up_rows(temp_db, synthetic_prices_crypto):
    _insert_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    btc = synthetic_prices_crypto("BTCUSDT", num_days=80, start_price=50000)
    eth = synthetic_prices_crypto("ETHUSDT", num_days=80, start_price=3000)
    _insert_prices(temp_db, btc + eth)

    n = compute_features(temp_db)
    assert n > 0


def test_compute_features_empty_universe(temp_db):
    n = compute_features(temp_db)
    assert n == 0


def test_compute_features_finite_values(temp_db, synthetic_prices_crypto):
    """No NaN/Inf in core features once the warmup window is satisfied."""
    _insert_universe(temp_db, ["BTCUSDT"])
    rows = synthetic_prices_crypto("BTCUSDT", num_days=80)
    _insert_prices(temp_db, rows)
    compute_features(temp_db)

    cols_to_check = ["return_5d", "rsi_14d", "realized_vol_30d"]
    rows = temp_db.execute(
        f"SELECT {', '.join(cols_to_check)} FROM crypto_ml_features "
        f"ORDER BY trade_date DESC LIMIT 30"
    ).fetchall()
    for row in rows:
        for v in row:
            if v is not None:
                assert math.isfinite(v), f"non-finite value {v}"


def test_lookahead_bias_features_dont_change(temp_db, synthetic_prices_crypto):
    """Feature for date T must not change when later prices are appended."""
    _insert_universe(temp_db, ["BTCUSDT"])
    rows = synthetic_prices_crypto("BTCUSDT", num_days=80)
    _insert_prices(temp_db, rows)
    compute_features(temp_db)

    target = rows[60]["trade_date"]
    before = temp_db.execute(
        "SELECT return_5d, rsi_14d FROM crypto_ml_features "
        "WHERE symbol = 'BTCUSDT' AND trade_date = ?", [target]
    ).fetchone()

    extra = []
    last_date = rows[-1]["trade_date"]
    for i in range(1, 11):
        d = last_date + timedelta(days=i)
        extra.append({
            "symbol": "BTCUSDT", "trade_date": d,
            "open": 50000, "high": 51000, "low": 49000,
            "close": 50500, "volume": 1e8, "trades": 1000,
            "taker_buy_volume": 5e7, "source": "synth",
        })
    _insert_prices(temp_db, extra)
    compute_features(temp_db)

    after = temp_db.execute(
        "SELECT return_5d, rsi_14d FROM crypto_ml_features "
        "WHERE symbol = 'BTCUSDT' AND trade_date = ?", [target]
    ).fetchone()

    assert before == after, (
        f"crypto feature(s) at {target} changed when later data was added "
        f"— lookahead bias. before={before}, after={after}"
    )


def test_compute_features_skips_inactive_symbols(temp_db, synthetic_prices_crypto):
    temp_db.execute(
        "INSERT INTO crypto_universe (symbol, base_asset, rank_by_volume, is_active) "
        "VALUES ('DELISTED', 'DELISTED', 999, false)"
    )
    rows = synthetic_prices_crypto("DELISTED", num_days=60)
    _insert_prices(temp_db, rows)

    n = compute_features(temp_db)
    assert n == 0
