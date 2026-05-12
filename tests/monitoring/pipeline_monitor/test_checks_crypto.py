"""Unit tests for monitoring.pipeline_monitor.checks.crypto.

The MHDE side uses the project ``temp_db`` fixture (all production tables).
The engine side is a synthetic DuckDB with just the columns the checks read.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from monitoring.pipeline_monitor.core import Status
from monitoring.pipeline_monitor.checks import crypto as C


NOW = datetime(2026, 5, 12, 6, 40, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
YDAY = TODAY - timedelta(days=1)


# ── builders ──────────────────────────────────────────────────────────
def _seed_prices(conn, trade_date, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT")):
    for s in symbols:
        conn.execute(
            "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?)",
            [s, trade_date, 100.0, 101.0, 99.0, 100.5, 1e6],
        )


def _seed_funding_oi(conn, funding_ts, oi_date, symbols=("BTCUSDT", "ETHUSDT")):
    for s in symbols:
        conn.execute(
            "INSERT INTO crypto_funding_rates (symbol, funding_time, funding_rate, mark_price) VALUES (?,?,?,?)",
            [s, funding_ts, 0.0001, 100.0],
        )
        conn.execute(
            "INSERT INTO crypto_open_interest (symbol, trade_date, open_interest, open_interest_value) VALUES (?,?,?,?)",
            [s, oi_date, 1000.0, 100000.0],
        )


def _seed_active_model(conn, model_id="m1"):
    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, is_active) VALUES (?,?,?,?)",
        [model_id, "10d", 0.10, True],
    )


def _seed_features(conn, trade_date, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT")):
    for s in symbols:
        conn.execute(
            "INSERT INTO crypto_ml_features (symbol, trade_date, return_1d) VALUES (?,?,?)",
            [s, trade_date, 0.01],
        )


def _seed_prediction(conn, symbol, prediction_date, model_id="m1", horizon="10d",
                     actual_hit=None, outcome_filled_at=None):
    conn.execute(
        "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, horizon, "
        "predicted_probability, prediction_threshold, actual_hit, outcome_filled_at) VALUES (?,?,?,?,?,?,?,?)",
        [symbol, prediction_date, model_id, horizon, 0.8, 0.1, actual_hit, outcome_filled_at],
    )


def _write_export(tmp_path, export_date=None, features_as_of=None, n_predictions=5, write_file=True):
    d = tmp_path / "exports"
    d.mkdir(exist_ok=True)
    export_date = export_date if export_date is not None else TODAY.isoformat()
    features_as_of = features_as_of if features_as_of is not None else YDAY.isoformat()
    fname = f"predictions_{export_date}.json"
    if write_file:
        (d / fname).write_text(json.dumps({
            "export_date": export_date,
            "features_as_of_date": features_as_of,
            "model_id": "m1",
            "n_predictions": n_predictions,
            "predictions": [{"symbol": f"S{i}USDT", "rank": i + 1, "probability": 0.9 - i * 0.01}
                            for i in range(n_predictions)],
        }))
    link = d / "predictions_latest.json"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(fname)
    return d


def _new_engine_db():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE engine_runs (id VARCHAR, phase VARCHAR, started_at TIMESTAMP, "
        "completed_at TIMESTAMP, success BOOLEAN, error_message VARCHAR)"
    )
    conn.execute(
        "CREATE TABLE positions (id VARCHAR, symbol VARCHAR, entry_date DATE, entry_price DOUBLE, "
        "qty DOUBLE, peak_price DOUBLE, current_state VARCHAR, horizon_expiry_date DATE, "
        "spec_version VARCHAR, spec_hash VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, "
        "exit_price DOUBLE, realized_pnl_usd DOUBLE)"
    )
    conn.execute(
        "CREATE TABLE events (id VARCHAR, timestamp TIMESTAMP, position_id VARCHAR, "
        "event_type VARCHAR, payload JSON)"
    )
    return conn


def _add_run(eng, phase, started_at, success=True, err=None):
    eng.execute(
        "INSERT INTO engine_runs (id, phase, started_at, completed_at, success, error_message) VALUES (?,?,?,?,?,?)",
        [f"r-{phase}-{started_at.isoformat()}", phase, started_at, started_at + timedelta(seconds=5), success, err],
    )


def _add_position(eng, pid, symbol, entry_date, state="entry_filled"):
    eng.execute(
        "INSERT INTO positions (id, symbol, entry_date, entry_price, qty, current_state) VALUES (?,?,?,?,?,?)",
        [pid, symbol, entry_date, 1.0, 100.0, state],
    )


@pytest.fixture
def seeded_db(temp_db):
    """temp_db with a fully-healthy crypto pipeline state at NOW."""
    _seed_prices(temp_db, YDAY)
    _seed_funding_oi(temp_db, datetime(TODAY.year, TODAY.month, TODAY.day, 0, 0, 0), TODAY)
    _seed_active_model(temp_db)
    _seed_features(temp_db, YDAY)
    # today's fresh predictions (not matured)
    for s in ("S1USDT", "S2USDT", "S3USDT"):
        _seed_prediction(temp_db, s, YDAY)
    # an old, matured, tagged prediction (10d window long closed) — no backlog
    _seed_prediction(temp_db, "OLD1USDT", TODAY - timedelta(days=30),
                     actual_hit=True, outcome_filled_at=datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=18))
    return temp_db


@pytest.fixture
def healthy_engine():
    eng = _new_engine_db()
    _add_run(eng, "monitor", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 39, 0))
    _add_run(eng, "entry", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30, 0))
    for i, s in enumerate(("S1USDT", "S2USDT", "S3USDT", "S4USDT", "S5USDT")):
        _add_position(eng, f"p{i}", s, TODAY)
    return eng


# ══════════════════════════════════════════════════════════════════════
# 1. OHLCV ingestion
# ══════════════════════════════════════════════════════════════════════
def test_ohlcv_green(seeded_db):
    r = C.check_ohlcv_ingestion(seeded_db, NOW)
    assert r.status is Status.GREEN
    assert "2026-05-11" in r.detail


def test_ohlcv_green_when_advanced_to_today(temp_db):
    _seed_prices(temp_db, TODAY)
    assert C.check_ohlcv_ingestion(temp_db, NOW).status is Status.GREEN


def test_ohlcv_red_when_empty(temp_db):
    r = C.check_ohlcv_ingestion(temp_db, NOW)
    assert r.status is Status.RED
    assert "empty" in r.detail


def test_ohlcv_red_when_stale(temp_db):
    _seed_prices(temp_db, TODAY - timedelta(days=3))
    r = C.check_ohlcv_ingestion(temp_db, NOW)
    assert r.status is Status.RED
    assert "did not advance" in r.detail


# ══════════════════════════════════════════════════════════════════════
# 2. Data-quality guard
# ══════════════════════════════════════════════════════════════════════
def test_dq_green_when_no_rows(temp_db):
    assert C.check_data_quality_guard(temp_db, NOW).status is Status.GREEN


def test_dq_green_with_per_symbol_warnings_only(temp_db):
    temp_db.execute(
        "INSERT INTO crypto_data_quality_reports (date, symbol, check_name, expected, observed, flagged, severity) "
        "VALUES (?,?,?,?,?,?,?)",
        [YDAY, "DOGEUSDT", "volume_cliff", 1000.0, 100.0, True, "warn"],
    )
    r = C.check_data_quality_guard(temp_db, NOW)
    assert r.status is Status.GREEN
    assert "per-symbol warning" in r.detail


def test_dq_red_when_systemic_flag_recent(temp_db):
    temp_db.execute(
        "INSERT INTO crypto_data_quality_reports (date, symbol, check_name, expected, observed, flagged, severity) "
        "VALUES (?,?,?,?,?,?,?)",
        [YDAY, "__systemic__", "systemic_corruption", 0.5, 0.8, True, "critical"],
    )
    r = C.check_data_quality_guard(temp_db, NOW)
    assert r.status is Status.RED
    assert "SYSTEMIC" in r.detail


def test_dq_green_when_systemic_flag_is_old(temp_db):
    temp_db.execute(
        "INSERT INTO crypto_data_quality_reports (date, symbol, check_name, expected, observed, flagged, severity) "
        "VALUES (?,?,?,?,?,?,?)",
        [TODAY - timedelta(days=10), "__systemic__", "systemic_corruption", 0.5, 0.8, True, "critical"],
    )
    assert C.check_data_quality_guard(temp_db, NOW).status is Status.GREEN


# ══════════════════════════════════════════════════════════════════════
# 3. Funding / OI ingestion
# ══════════════════════════════════════════════════════════════════════
def test_funding_oi_green(seeded_db):
    assert C.check_funding_oi_ingestion(seeded_db, NOW).status is Status.GREEN


def test_funding_oi_red_when_funding_stale(temp_db):
    _seed_funding_oi(temp_db, datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=3), TODAY)
    r = C.check_funding_oi_ingestion(temp_db, NOW)
    assert r.status is Status.RED
    assert "funding stale" in r.detail


def test_funding_oi_red_when_oi_empty(temp_db):
    temp_db.execute(
        "INSERT INTO crypto_funding_rates (symbol, funding_time, funding_rate, mark_price) VALUES (?,?,?,?)",
        ["BTCUSDT", datetime(TODAY.year, TODAY.month, TODAY.day), 0.0001, 100.0],
    )
    r = C.check_funding_oi_ingestion(temp_db, NOW)
    assert r.status is Status.RED
    assert "open_interest is empty" in r.detail


def test_funding_oi_red_when_both_empty(temp_db):
    r = C.check_funding_oi_ingestion(temp_db, NOW)
    assert r.status is Status.RED
    assert "funding_rates is empty" in r.detail and "open_interest is empty" in r.detail


# ══════════════════════════════════════════════════════════════════════
# 4. Feature pipeline
# ══════════════════════════════════════════════════════════════════════
def test_features_green(seeded_db):
    r = C.check_feature_pipeline(seeded_db, NOW)
    assert r.status is Status.GREEN
    assert "2026-05-11" in r.detail


def test_features_red_when_empty(temp_db):
    r = C.check_feature_pipeline(temp_db, NOW)
    assert r.status is Status.RED and "empty" in r.detail


def test_features_red_when_stale(temp_db):
    _seed_features(temp_db, TODAY - timedelta(days=3))
    r = C.check_feature_pipeline(temp_db, NOW)
    assert r.status is Status.RED and "expected features for" in r.detail


# ══════════════════════════════════════════════════════════════════════
# 5. Model predictions
# ══════════════════════════════════════════════════════════════════════
def test_predictions_green(seeded_db):
    r = C.check_model_predictions(seeded_db, NOW)
    assert r.status is Status.GREEN
    assert "3 predictions" in r.detail


def test_predictions_red_when_no_active_model(temp_db):
    _seed_prediction(temp_db, "S1USDT", YDAY)  # has rows but no active model row
    r = C.check_model_predictions(temp_db, NOW)
    assert r.status is Status.RED and "no active model" in r.detail


def test_predictions_red_when_no_rows_from_active_model(temp_db):
    _seed_active_model(temp_db)
    r = C.check_model_predictions(temp_db, NOW)
    assert r.status is Status.RED and "no crypto predictions" in r.detail


def test_predictions_red_when_stale(temp_db):
    _seed_active_model(temp_db)
    _seed_prediction(temp_db, "S1USDT", TODAY - timedelta(days=4))
    r = C.check_model_predictions(temp_db, NOW)
    assert r.status is Status.RED and "expected" in r.detail


def test_predictions_ignores_inactive_model_rows(temp_db):
    _seed_active_model(temp_db, "m1")
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, is_active) VALUES (?,?,?,?)",
        ["backtest_x", "10d", 0.1, False],
    )
    _seed_prediction(temp_db, "S1USDT", YDAY, model_id="m1")
    _seed_prediction(temp_db, "S2USDT", TODAY - timedelta(days=99), model_id="backtest_x")
    r = C.check_model_predictions(temp_db, NOW)
    assert r.status is Status.GREEN
    assert "@ prediction_date=2026-05-11" in r.detail


# ══════════════════════════════════════════════════════════════════════
# 6. Outcome tagging
# ══════════════════════════════════════════════════════════════════════
def test_outcome_tagging_green(seeded_db):
    r = C.check_outcome_tagging(seeded_db, NOW)
    assert r.status is Status.GREEN
    assert "last fill" in r.detail


def test_outcome_tagging_green_when_nothing_matured(temp_db):
    _seed_active_model(temp_db)
    _seed_prediction(temp_db, "S1USDT", YDAY)  # fresh, not matured
    r = C.check_outcome_tagging(temp_db, NOW)
    assert r.status is Status.GREEN


def test_outcome_tagging_red_when_matured_prediction_untagged(temp_db):
    _seed_active_model(temp_db)
    _seed_prediction(temp_db, "S1USDT", TODAY - timedelta(days=20), horizon="10d", actual_hit=None)
    r = C.check_outcome_tagging(temp_db, NOW)
    assert r.status is Status.RED
    assert "actual_hit NULL" in r.detail


def test_outcome_tagging_ignores_untagged_but_unmatured(temp_db):
    _seed_active_model(temp_db)
    # 10d horizon, prediction_date = today-9 → window closes today-9+10 = today+1, +2 margin → not yet due
    _seed_prediction(temp_db, "S1USDT", TODAY - timedelta(days=9), horizon="10d", actual_hit=None)
    assert C.check_outcome_tagging(temp_db, NOW).status is Status.GREEN


# ══════════════════════════════════════════════════════════════════════
# 7. Export predictions
# ══════════════════════════════════════════════════════════════════════
def test_export_green(tmp_path):
    d = _write_export(tmp_path)
    r = C.check_export_predictions(NOW, exports_dir=d)
    assert r.status is Status.GREEN
    assert "export_date=2026-05-12" in r.detail
    assert "features_as_of=2026-05-11" in r.detail


def test_export_red_when_missing(tmp_path):
    r = C.check_export_predictions(NOW, exports_dir=tmp_path / "nope")
    assert r.status is Status.RED and "does not exist" in r.detail


def test_export_red_when_stale_export_date(tmp_path):
    # this is exactly the KI-138 regression: the symlink points to an old file
    d = _write_export(tmp_path, export_date=(TODAY - timedelta(days=2)).isoformat())
    r = C.check_export_predictions(NOW, exports_dir=d)
    assert r.status is Status.RED
    assert "stale" in r.detail and (TODAY - timedelta(days=2)).isoformat() in r.detail


def test_export_red_when_zero_predictions(tmp_path):
    d = _write_export(tmp_path, n_predictions=0)
    r = C.check_export_predictions(NOW, exports_dir=d)
    assert r.status is Status.RED and "0 predictions" in r.detail


# ══════════════════════════════════════════════════════════════════════
# 8. Engine ingest (entry run today)
# ══════════════════════════════════════════════════════════════════════
def test_engine_ingest_green(healthy_engine):
    r = C.check_engine_ingest(healthy_engine, NOW)
    assert r.status is Status.GREEN and "06:30 UTC" in r.detail


def test_engine_ingest_red_when_no_entry_today():
    eng = _new_engine_db()
    _add_run(eng, "entry", datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=1, hours=-6))
    r = C.check_engine_ingest(eng, NOW)
    assert r.status is Status.RED and "no 'entry' phase today" in r.detail


def test_engine_ingest_red_when_last_entry_failed():
    eng = _new_engine_db()
    _add_run(eng, "entry", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30), success=False, err="binance 500")
    r = C.check_engine_ingest(eng, NOW)
    assert r.status is Status.RED and "binance 500" in r.detail


def test_engine_ingest_green_when_retry_succeeded():
    eng = _new_engine_db()
    _add_run(eng, "entry", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30), success=False, err="x")
    _add_run(eng, "entry", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 50), success=True)
    assert C.check_engine_ingest(eng, NOW).status is Status.GREEN


def test_engine_ingest_red_when_db_unreachable():
    assert C.check_engine_ingest(None, NOW).status is Status.RED


# ══════════════════════════════════════════════════════════════════════
# 9. Engine entry / positions
# ══════════════════════════════════════════════════════════════════════
def test_positions_green(healthy_engine):
    r = C.check_engine_positions(healthy_engine, NOW, spec_path=None)
    assert r.status is Status.GREEN and "5 position(s) opened today" in r.detail


def test_positions_red_when_zero_and_not_at_max(tmp_path):
    eng = _new_engine_db()
    _add_position(eng, "p1", "AAAUSDT", TODAY - timedelta(days=2), "entry_filled")  # 1 open, not at max
    spec = tmp_path / "active_spec.json"
    spec.write_text(json.dumps({"sizing": {"max_concurrent": 6}}))
    r = C.check_engine_positions(eng, NOW, spec_path=spec)
    assert r.status is Status.RED
    assert "0 positions opened today" in r.detail and "1/6 open" in r.detail


def test_positions_green_when_zero_but_book_at_max(tmp_path):
    eng = _new_engine_db()
    for i in range(6):
        _add_position(eng, f"p{i}", f"S{i}USDT", TODAY - timedelta(days=3), "entry_filled")
    spec = tmp_path / "active_spec.json"
    spec.write_text(json.dumps({"sizing": {"max_concurrent": 6}}))
    r = C.check_engine_positions(eng, NOW, spec_path=spec)
    assert r.status is Status.GREEN and "max_concurrent" in r.detail


def test_positions_red_when_zero_and_no_spec(tmp_path):
    eng = _new_engine_db()
    r = C.check_engine_positions(eng, NOW, spec_path=tmp_path / "missing.json")
    assert r.status is Status.RED
    assert "0 positions opened today and only 0 open" in r.detail


def test_positions_red_when_db_unreachable():
    assert C.check_engine_positions(None, NOW).status is Status.RED
