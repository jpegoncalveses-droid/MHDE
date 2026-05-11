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


# ──────────────────────────────────────────────────────────────────────
# Knockout (triple-barrier) label — populated by a forward-walk pass in
# compute_labels. See crypto/ml/KNOCKOUT_LABEL_SPEC.md.
# ──────────────────────────────────────────────────────────────────────


def _flat_rows(symbol, n_days, *, start=date(2025, 1, 1), close=100.0,
               high=102.0, low=98.0, mods=None):
    """n_days of flat OHLCV (close/high/low constant) starting at `start`.
    `mods` maps day-index -> {col: value} overrides (e.g. {1: {'high': 120}})."""
    mods = mods or {}
    rows = []
    for i in range(n_days):
        r = {"symbol": symbol, "trade_date": start + timedelta(days=i),
             "open": close, "high": high, "low": low, "close": close,
             "volume": 1e6, "trades": 1000, "taker_buy_volume": 5e5, "source": "test"}
        r.update(mods.get(i, {}))
        rows.append(r)
    return rows


def _label_row(conn, symbol, trade_date):
    return conn.execute(
        "SELECT label_5d_knockout, knockout_outcome_5d, knockout_resolve_day_5d, "
        "label_10d_knockout, knockout_outcome_10d, knockout_resolve_day_10d "
        "FROM crypto_ml_labels WHERE symbol = ? AND trade_date = ?",
        [symbol, trade_date],
    ).fetchone()


def test_compute_labels_knockout_tp_win(temp_db):
    """Entry on day 0; the next bar's high (120) pierces the +10% barrier
    (110) before any -5% (95) touch → outcome 'tp', resolve_day 1, label True."""
    _insert_universe(temp_db, ["BTCUSDT"])
    start = date(2025, 1, 1)
    _insert_prices(temp_db, _flat_rows("BTCUSDT", 15, start=start, mods={1: {"high": 120.0}}))
    compute_labels(temp_db)
    l5k, o5, d5, l10k, o10, d10 = _label_row(temp_db, "BTCUSDT", start)
    assert (l5k, o5, d5) == (True, "tp", 1)
    assert (l10k, o10, d10) == (True, "tp", 1)


def test_compute_labels_knockout_sl_loss(temp_db):
    """Entry on day 0; the next bar's low (85) pierces the -5% barrier (95)
    before any +10% touch → outcome 'sl', resolve_day 1, label False."""
    _insert_universe(temp_db, ["BTCUSDT"])
    start = date(2025, 1, 1)
    _insert_prices(temp_db, _flat_rows("BTCUSDT", 15, start=start, mods={1: {"low": 85.0}}))
    compute_labels(temp_db)
    l5k, o5, d5, l10k, o10, d10 = _label_row(temp_db, "BTCUSDT", start)
    assert (l5k, o5, d5) == (False, "sl", 1)
    assert (l10k, o10, d10) == (False, "sl", 1)


def test_compute_labels_knockout_sl_first_on_same_bar(temp_db):
    """Day-1 bar touches BOTH barriers (high 120 >= 110, low 80 <= 95) →
    pessimistic SL-first → outcome 'sl' on day 1, label False."""
    _insert_universe(temp_db, ["BTCUSDT"])
    start = date(2025, 1, 1)
    _insert_prices(temp_db, _flat_rows("BTCUSDT", 15, start=start, mods={1: {"high": 120.0, "low": 80.0}}))
    compute_labels(temp_db)
    l5k, o5, d5, *_ = _label_row(temp_db, "BTCUSDT", start)
    assert (l5k, o5, d5) == (False, "sl", 1)


def test_compute_labels_knockout_neither_is_loss(temp_db):
    """No bar touches either barrier within the horizon → 'neither',
    resolve_day NULL, label False (neither-is-loss)."""
    _insert_universe(temp_db, ["BTCUSDT"])
    start = date(2025, 1, 1)
    _insert_prices(temp_db, _flat_rows("BTCUSDT", 20, start=start))  # all flat: high 102, low 98
    compute_labels(temp_db)
    l5k, o5, d5, l10k, o10, d10 = _label_row(temp_db, "BTCUSDT", start)
    assert (l5k, o5, d5) == (False, "neither", None)
    assert (l10k, o10, d10) == (False, "neither", None)


def test_compute_labels_knockout_horizon_difference(temp_db):
    """A TP touch on forward-day 8 is a 10d win but a 5d 'neither' (loss)."""
    _insert_universe(temp_db, ["BTCUSDT"])
    start = date(2025, 1, 1)
    _insert_prices(temp_db, _flat_rows("BTCUSDT", 20, start=start, mods={8: {"high": 130.0}}))
    compute_labels(temp_db)
    l5k, o5, d5, l10k, o10, d10 = _label_row(temp_db, "BTCUSDT", start)
    assert (l5k, o5, d5) == (False, "neither", None)
    assert (l10k, o10, d10) == (True, "tp", 8)


def test_compute_labels_knockout_legacy_columns_untouched(temp_db, synthetic_prices_crypto):
    """The additive knockout pass must not disturb the existing close-based
    labels — label_*_10pct etc. are still populated."""
    _insert_universe(temp_db, ["BTCUSDT"])
    _insert_prices(temp_db, synthetic_prices_crypto("BTCUSDT", num_days=30))
    compute_labels(temp_db)
    # at least one legacy label column is non-NULL across the rows
    n_legacy = temp_db.execute(
        "SELECT COUNT(*) FROM crypto_ml_labels WHERE label_10d_10pct IS NOT NULL"
    ).fetchone()[0]
    n_total = temp_db.execute("SELECT COUNT(*) FROM crypto_ml_labels").fetchone()[0]
    assert n_total > 0 and n_legacy == n_total
    # and the knockout columns are all populated too
    n_ko = temp_db.execute(
        "SELECT COUNT(*) FROM crypto_ml_labels WHERE knockout_outcome_10d IS NOT NULL"
    ).fetchone()[0]
    assert n_ko == n_total
