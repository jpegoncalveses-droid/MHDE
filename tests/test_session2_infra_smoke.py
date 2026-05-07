"""Smoke tests for the Session 2 test infrastructure itself.

These verify the fixtures load and produce sensible output. They do not
test production code — that's the work of Sessions 3-5. Keep them fast
and independent of any external service.
"""
from __future__ import annotations

import requests


# ──────────────────────────────────────────────────────────────────────
# temp_db: every active schema loads cleanly into an in-memory DuckDB
# ──────────────────────────────────────────────────────────────────────


EXPECTED_TABLES_AT_LEAST = {
    # equity / shared
    "schema_version", "companies", "prices_daily", "filings",
    "fundamentals_features", "scores", "hypotheses",
    "candidate_outcomes", "model_runs", "llm_runs", "alerts",
    "pipeline_runs", "health_checks", "earnings_estimates",
    "move_episodes",
    # equity ML
    "ml_features", "ml_labels", "ml_predictions", "ml_model_runs",
    # crypto ML
    "crypto_prices_daily", "crypto_funding_rates", "crypto_open_interest",
    "crypto_universe", "crypto_ml_features", "crypto_ml_labels",
    "crypto_ml_predictions", "crypto_ml_model_runs",
    # FX ML
    "fx_prices_hourly", "fx_macro", "fx_ml_features", "fx_ml_labels",
    "fx_ml_predictions", "fx_ml_model_runs", "fx_signals",
    "fx_position", "fx_alert_state",
}


def test_temp_db_creates_all_tables(temp_db):
    rows = temp_db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'"
    ).fetchall()
    actual = {r[0] for r in rows}
    missing = EXPECTED_TABLES_AT_LEAST - actual
    assert not missing, f"temp_db is missing tables: {sorted(missing)}"


# ──────────────────────────────────────────────────────────────────────
# synthetic_prices_*: generators produce shape-correct, plausible data
# ──────────────────────────────────────────────────────────────────────


def test_synthetic_prices_equity_inserts_into_temp_db(
    temp_db, synthetic_prices_equity
):
    rows = synthetic_prices_equity("AAPL", num_days=10, start_price=150.0)
    assert len(rows) == 10
    for r in rows:
        # OHLC ordering: low <= open <= high and low <= close <= high
        assert r["low"] <= r["open"] <= r["high"]
        assert r["low"] <= r["close"] <= r["high"]
        # Trading days only (Mon-Fri)
        assert r["trade_date"].weekday() < 5

    temp_db.executemany(
        "INSERT INTO prices_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["id"], r["ticker"], r["trade_date"], r["open"], r["high"],
          r["low"], r["close"], r["volume"], r["adjusted_close"],
          r["source"], r["run_id"], None) for r in rows]
    )
    n = temp_db.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker = 'AAPL'"
    ).fetchone()[0]
    assert n == 10


def test_synthetic_prices_crypto_shape(synthetic_prices_crypto):
    rows = synthetic_prices_crypto("ETHUSDT", num_days=14, start_price=3000.0)
    assert len(rows) == 14
    for r in rows:
        assert r["low"] <= r["open"] <= r["high"]
        assert r["low"] <= r["close"] <= r["high"]
    # Crypto generates 7 days/week (no weekend skipping)
    weekdays = {r["trade_date"].weekday() for r in rows}
    assert weekdays == set(range(7)) or len(rows) < 7  # full week if 14 days


def test_synthetic_prices_fx_skips_weekend(synthetic_prices_fx):
    rows = synthetic_prices_fx(num_hours=72)
    assert len(rows) == 72
    for r in rows:
        wd, hr = r["datetime_utc"].weekday(), r["datetime_utc"].hour
        in_weekend = (wd == 5 and hr >= 21) or (wd == 6 and hr < 21)
        assert not in_weekend, f"weekend bar leaked: {r['datetime_utc']}"


def test_synthetic_filings_default(synthetic_filings):
    rows = synthetic_filings("NVDA")
    assert len(rows) == 5
    assert {r["form_type"] for r in rows} <= {"8-K", "10-Q", "10-K"}
    assert all(r["ticker"] == "NVDA" for r in rows)


def test_synthetic_fundamentals_default(synthetic_fundamentals):
    rows = synthetic_fundamentals("NVDA")
    assert len(rows) == 4
    revs = [r["revenue"] for r in rows]
    # Revenue grows monotonically (5% QoQ)
    assert all(b > a for a, b in zip(revs, revs[1:]))


# ──────────────────────────────────────────────────────────────────────
# mock_telegram: real network calls are blocked, attempts are captured
# ──────────────────────────────────────────────────────────────────────


def test_mock_telegram_captures_post(mock_telegram):
    resp = requests.post(
        "https://api.telegram.org/bot123/sendMessage",
        json={"chat_id": "456", "text": "hello"},
    )
    assert resp.status_code == 200
    assert len(mock_telegram) == 1
    assert mock_telegram[0]["url"].startswith("https://api.telegram.org/")
    assert mock_telegram[0]["json"]["text"] == "hello"
