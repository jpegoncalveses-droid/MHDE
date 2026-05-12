"""Integration tests for monitoring.pipeline_monitor.daily_runner.

The runner aggregates every step's check for one pipeline, applies the
short-circuit cascade, and posts exactly one Telegram message.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from monitoring.pipeline_monitor import daily_runner
from monitoring.pipeline_monitor.core import PipelineResult, Status, StepResult


NOW = datetime(2026, 5, 12, 6, 40, 0, tzinfo=timezone.utc)  # Tuesday
TODAY = NOW.date()
YDAY = TODAY - timedelta(days=1)
EQ_EXPECTED = YDAY  # expected_equity_prediction_date(Tuesday) == Monday


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


# ── crypto seeding ────────────────────────────────────────────────────
def _seed_healthy_crypto(conn):
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        conn.execute(
            "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            [s, YDAY, 1.0, 1.1, 0.9, 1.05, 1e6],
        )
        conn.execute(
            "INSERT INTO crypto_funding_rates (symbol, funding_time, funding_rate, mark_price) VALUES (?,?,?,?)",
            [s, datetime(TODAY.year, TODAY.month, TODAY.day, 0, 0, 0), 0.0001, 1.0],
        )
        conn.execute(
            "INSERT INTO crypto_open_interest (symbol, trade_date, open_interest, open_interest_value) VALUES (?,?,?,?)",
            [s, TODAY, 1000.0, 1000.0],
        )
        conn.execute("INSERT INTO crypto_ml_features (symbol, trade_date, return_1d) VALUES (?,?,?)", [s, YDAY, 0.01])
    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, is_active) VALUES (?,?,?,?)",
        ["m1", "10d", 0.1, True],
    )
    for s in ("S1USDT", "S2USDT", "S3USDT"):
        conn.execute(
            "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, horizon, predicted_probability, prediction_threshold) "
            "VALUES (?,?,?,?,?,?)",
            [s, YDAY, "m1", "10d", 0.8, 0.1],
        )
    conn.execute(
        "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, horizon, predicted_probability, prediction_threshold, actual_hit, outcome_filled_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ["OLDUSDT", TODAY - timedelta(days=30), "m1", "10d", 0.8, 0.1, True,
         datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=18)],
    )


def _healthy_engine():
    eng = duckdb.connect(":memory:")
    eng.execute("CREATE TABLE engine_runs (id VARCHAR, phase VARCHAR, started_at TIMESTAMP, completed_at TIMESTAMP, success BOOLEAN, error_message VARCHAR)")
    eng.execute("CREATE TABLE positions (id VARCHAR, symbol VARCHAR, entry_date DATE, entry_price DOUBLE, qty DOUBLE, current_state VARCHAR)")
    eng.execute("CREATE TABLE events (id VARCHAR, timestamp TIMESTAMP, position_id VARCHAR, event_type VARCHAR, payload JSON)")
    eng.execute("INSERT INTO engine_runs VALUES (?,?,?,?,?,?)", ["r1", "monitor", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 39), datetime(TODAY.year, TODAY.month, TODAY.day, 6, 39, 5), True, None])
    eng.execute("INSERT INTO engine_runs VALUES (?,?,?,?,?,?)", ["r2", "entry", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30), datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30, 8), True, None])
    for i, s in enumerate(("A", "B", "C", "D", "E")):
        eng.execute("INSERT INTO positions VALUES (?,?,?,?,?,?)", [f"p{i}", f"{s}USDT", TODAY, 1.0, 100.0, "entry_filled"])
    return eng


def _write_export(tmp_path):
    d = tmp_path / "exports"
    d.mkdir(exist_ok=True)
    fname = f"predictions_{TODAY.isoformat()}.json"
    (d / fname).write_text(json.dumps({
        "export_date": TODAY.isoformat(), "features_as_of_date": YDAY.isoformat(),
        "n_predictions": 5, "predictions": [{"symbol": f"S{i}USDT", "rank": i + 1} for i in range(5)],
    }))
    link = d / "predictions_latest.json"
    link.symlink_to(fname)
    return d


def _write_spec(tmp_path):
    p = tmp_path / "active_spec.json"
    p.write_text(json.dumps({"sizing": {"max_concurrent": 6}}))
    return p


# ── equity / fx seeding ───────────────────────────────────────────────
def _seed_healthy_equity(conn):
    for t in ("AAPL", "MSFT", "NVDA"):
        conn.execute("INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?,?,?,?)", [f"{t}-x", t, EQ_EXPECTED, 100.0])
        conn.execute("INSERT INTO ml_features (ticker, trade_date, return_5d) VALUES (?,?,?)", [t, EQ_EXPECTED, 0.01])
        conn.execute("INSERT INTO ml_predictions (ticker, prediction_date, model_id, horizon, predicted_probability, prediction_threshold) VALUES (?,?,?,?,?,?)",
                     [t, EQ_EXPECTED, "eq1", "10d", 0.7, 0.05])
    conn.execute("INSERT INTO ml_model_runs (model_id, horizon, target_threshold, is_active) VALUES (?,?,?,?)", ["eq1", "10d", 0.05, True])


def _seed_healthy_fx(conn):
    bar = datetime(TODAY.year, TODAY.month, TODAY.day, 6, 0, 0)  # 40 min before NOW
    conn.execute("INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count) VALUES (?,?,?,?,?,?,?,?,?)",
                 [bar, bar.date(), bar.weekday(), bar.hour, 1.15, 1.16, 1.14, 1.155, 100])
    conn.execute("INSERT INTO fx_signals (datetime_utc, signal_type, gbpeur_price) VALUES (?,?,?)", [bar, "BUY_GBP", 1.155])


# ══════════════════════════════════════════════════════════════════════
# crypto
# ══════════════════════════════════════════════════════════════════════
def test_crypto_all_green(temp_db, tmp_path):
    _seed_healthy_crypto(temp_db)
    res = daily_runner.run_pipeline(
        "crypto", mhde_conn=temp_db, engine_conn=_healthy_engine(), now=NOW,
        exports_dir=_write_export(tmp_path), spec_path=_write_spec(tmp_path),
    )
    assert res.pipeline == "Crypto"
    assert len(res.steps) == 9
    assert [s.status for s in res.steps] == [Status.GREEN] * 9, [(s.name, s.status, s.detail) for s in res.steps]
    assert not res.has_red


def test_crypto_cascade_on_first_red(temp_db, tmp_path):
    # nothing seeded → step 1 (OHLCV) RED, every later step ⚪
    res = daily_runner.run_pipeline(
        "crypto", mhde_conn=temp_db, engine_conn=_healthy_engine(), now=NOW,
        exports_dir=_write_export(tmp_path), spec_path=_write_spec(tmp_path),
    )
    assert res.steps[0].status is Status.RED
    assert all(s.status is Status.SKIPPED for s in res.steps[1:])
    assert res.has_red


def test_crypto_stale_export_is_caught(temp_db, tmp_path):
    # the KI-138 regression: pipeline tables fine, but predictions_latest.json is stale
    _seed_healthy_crypto(temp_db)
    d = tmp_path / "exports"
    d.mkdir()
    stale = d / f"predictions_{(TODAY - timedelta(days=2)).isoformat()}.json"
    stale.write_text(json.dumps({"export_date": (TODAY - timedelta(days=2)).isoformat(), "n_predictions": 5, "predictions": [1] * 5}))
    (d / "predictions_latest.json").symlink_to(stale.name)
    res = daily_runner.run_pipeline(
        "crypto", mhde_conn=temp_db, engine_conn=_healthy_engine(), now=NOW,
        exports_dir=d, spec_path=_write_spec(tmp_path),
    )
    names = {s.name: s for s in res.steps}
    from monitoring.pipeline_monitor.checks import crypto as C
    assert names[C.EXPORT_PREDICTIONS].status is Status.RED
    assert names[C.ENGINE_INGEST].status is Status.SKIPPED
    assert names[C.ENGINE_POSITIONS].status is Status.SKIPPED
    # the steps before the export are all green
    assert all(names[n].status is Status.GREEN for n in
               (C.OHLCV_INGESTION, C.DATA_QUALITY_GUARD, C.FUNDING_OI_INGESTION,
                C.FEATURE_PIPELINE, C.MODEL_PREDICTIONS, C.OUTCOME_TAGGING))


# ══════════════════════════════════════════════════════════════════════
# equity / fx
# ══════════════════════════════════════════════════════════════════════
def test_equity_all_green(temp_db, tmp_path):
    _seed_healthy_equity(temp_db)
    marker = tmp_path / "prediction_vs_actual_rows.csv"
    marker.write_text("x\n")
    res = daily_runner.run_pipeline("equity", mhde_conn=temp_db, now=NOW, dashboard_marker=marker)
    assert res.pipeline == "Equity"
    assert [s.status for s in res.steps] == [Status.GREEN] * 4, [(s.name, s.detail) for s in res.steps]


def test_fx_all_green(temp_db):
    _seed_healthy_fx(temp_db)
    res = daily_runner.run_pipeline("fx", mhde_conn=temp_db, now=NOW)
    assert res.pipeline == "FX"
    assert [s.status for s in res.steps] == [Status.GREEN] * 2, [(s.name, s.detail) for s in res.steps]


def test_unknown_pipeline_raises(temp_db):
    with pytest.raises(ValueError):
        daily_runner.run_pipeline("bogus", mhde_conn=temp_db, now=NOW)


# ══════════════════════════════════════════════════════════════════════
# main() — sends exactly one message, returns 0/1 by status
# ══════════════════════════════════════════════════════════════════════
def test_main_sends_one_message_green(mocker):
    fake = PipelineResult("Crypto", NOW, [StepResult("step a", Status.GREEN, "ok")])
    mocker.patch.object(daily_runner, "run_pipeline", return_value=fake)
    sent = mocker.patch.object(daily_runner.alert, "send_text", return_value=False)
    rc = daily_runner.main("crypto")
    assert rc == 0
    sent.assert_called_once()
    assert sent.call_args[0][0].startswith("🟢 Crypto Pipeline 2026-05-12 06:40 UTC")


def test_main_returns_1_on_red(mocker):
    fake = PipelineResult("FX", NOW, [StepResult("bar", Status.RED, "stale")])
    mocker.patch.object(daily_runner, "run_pipeline", return_value=fake)
    sent = mocker.patch.object(daily_runner.alert, "send_text", return_value=False)
    assert daily_runner.main("fx") == 1
    sent.assert_called_once()
    assert sent.call_args[0][0].startswith("🔴 FX Pipeline")
