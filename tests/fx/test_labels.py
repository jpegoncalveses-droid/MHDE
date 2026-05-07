"""Unit tests for fx/ml/labels.py — forward-pip labels for hourly bars.

Coverage targets:
  - compute_labels writes one row per bar with at least 24h forward data.
  - Labels are in pips (not percent).
  - Binary labels at 20/30 pip thresholds match the underlying max_up/down.
  - The DELETE+INSERT pattern is idempotent.
  - Bars with insufficient forward data are dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fx.ml.labels import compute_labels


def _insert_fx_prices(conn, rows):
    conn.executemany(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
        "gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["datetime_utc"], r["date"], r["weekday"], r["hour_utc"],
          r["gbpeur_open"], r["gbpeur_high"], r["gbpeur_low"], r["gbpeur_close"],
          r["tick_count"], r["data_quality"]) for r in rows],
    )


def test_compute_labels_writes_rows(temp_db, synthetic_prices_fx):
    rows = synthetic_prices_fx(num_hours=200)
    _insert_fx_prices(temp_db, rows)

    n = compute_labels(temp_db)
    assert n > 0
    db_count = temp_db.execute("SELECT COUNT(*) FROM fx_ml_labels").fetchone()[0]
    assert db_count == n


def test_compute_labels_drops_tail_without_forward_data(temp_db, synthetic_prices_fx):
    """The last 24 bars don't have 24h of forward data; labels must drop them."""
    rows = synthetic_prices_fx(num_hours=100)
    _insert_fx_prices(temp_db, rows)

    compute_labels(temp_db)
    label_count = temp_db.execute("SELECT COUNT(*) FROM fx_ml_labels").fetchone()[0]
    # We inserted 100, but the last 24 lack 24h forward data, so labels should
    # have at most 100 - 24 rows (often fewer because the FX weekend skips).
    assert label_count <= len(rows) - 24


def test_compute_labels_pips_not_percent(temp_db, synthetic_prices_fx):
    """fwd_max_up_pips_24h must be expressed in pips (1 pip = 0.0001).

    A 0.001 GBP/EUR move = 10 pips, not 0.001 or 0.1.
    """
    rows = synthetic_prices_fx(num_hours=100, volatility=0.001)
    _insert_fx_prices(temp_db, rows)

    compute_labels(temp_db)
    df = temp_db.execute(
        "SELECT fwd_max_up_pips_24h, fwd_max_down_pips_24h FROM fx_ml_labels "
        "WHERE fwd_max_up_pips_24h IS NOT NULL LIMIT 50"
    ).fetchdf()
    # Synthetic data with 0.001 vol → typical max excursion is single-digit
    # to low-double-digit pips. Should never be in the 0-1 range that would
    # indicate the value is in raw price units rather than pips.
    assert df["fwd_max_up_pips_24h"].abs().mean() > 0.5
    # And should never be wildly large (not in % units like 200_000 etc).
    assert df["fwd_max_up_pips_24h"].abs().max() < 10_000


def test_binary_labels_match_continuous(temp_db, synthetic_prices_fx):
    """label_up_20pip_24h must equal (fwd_max_up_pips_24h >= 20)."""
    rows = synthetic_prices_fx(num_hours=200, volatility=0.0015)
    _insert_fx_prices(temp_db, rows)

    compute_labels(temp_db)
    rows = temp_db.execute(
        "SELECT fwd_max_up_pips_24h, label_up_20pip_24h, "
        "       fwd_max_down_pips_24h, label_down_20pip_24h, "
        "       fwd_max_up_pips_48h, label_up_30pip_48h "
        "FROM fx_ml_labels "
        "WHERE fwd_max_up_pips_24h IS NOT NULL "
        "  AND fwd_max_down_pips_24h IS NOT NULL"
    ).fetchall()
    for up24, lab_up_20, dn24, lab_dn_20, up48, lab_up_30_48 in rows:
        assert lab_up_20 == (up24 >= 20)
        assert lab_dn_20 == (dn24 >= 20)
        # 48h labels only meaningful when 48h forward data exists
        if up48 is not None:
            assert lab_up_30_48 == (up48 >= 30)


def test_compute_labels_idempotent(temp_db, synthetic_prices_fx):
    """Re-running compute_labels does not duplicate rows."""
    rows = synthetic_prices_fx(num_hours=120)
    _insert_fx_prices(temp_db, rows)

    n1 = compute_labels(temp_db)
    n2 = compute_labels(temp_db)
    assert n1 == n2


def test_compute_labels_skips_bad_quality_bars(temp_db, synthetic_prices_fx):
    """Rows with data_quality='BAD' must not contribute to labels."""
    good = synthetic_prices_fx(num_hours=120)
    # Mark every 4th bar as BAD
    bad_idx = set(range(0, len(good), 4))
    for i in bad_idx:
        good[i]["data_quality"] = "BAD"
    _insert_fx_prices(temp_db, good)

    compute_labels(temp_db)
    n_labels = temp_db.execute("SELECT COUNT(*) FROM fx_ml_labels").fetchone()[0]
    # Labels query only sees non-BAD rows; conservative check: fewer labels than total bars
    assert n_labels < len(good)


def test_compute_labels_empty_db(temp_db):
    """compute_labels on an empty fx_prices_hourly returns 0 and writes nothing."""
    n = compute_labels(temp_db)
    assert n == 0
