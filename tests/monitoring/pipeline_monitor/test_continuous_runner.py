"""Integration tests for monitoring.pipeline_monitor.continuous_runner.

Silent when all checks green; sends one Telegram message when any is red.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from monitoring.pipeline_monitor import continuous_runner as CR
from monitoring.pipeline_monitor.core import Status


NOW = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)  # Tuesday, past the 08:00 entry cutoff, forex open
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


def _mhde_with_fresh_fx(temp_db, bar_dt=None):
    bar_dt = bar_dt or (datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0, 0))  # 1h before NOW
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [bar_dt, bar_dt.date(), bar_dt.weekday(), bar_dt.hour, 1.15, 1.16, 1.14, 1.155, 100],
    )
    return temp_db


def _engine(monitor_age_min=1.0, entry_today=True):
    eng = duckdb.connect(":memory:")
    eng.execute("CREATE TABLE engine_runs (id VARCHAR, phase VARCHAR, started_at TIMESTAMP, completed_at TIMESTAMP, success BOOLEAN, error_message VARCHAR)")
    nn = NOW.replace(tzinfo=None)
    eng.execute("INSERT INTO engine_runs VALUES (?,?,?,?,?,?)", ["m1", "monitor", nn - timedelta(minutes=monitor_age_min), nn, True, None])
    if entry_today:
        eng.execute("INSERT INTO engine_runs VALUES (?,?,?,?,?,?)", ["e1", "entry", datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30), datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30, 8), True, None])
    return eng


# ── all green → silent ────────────────────────────────────────────────
def test_all_green_is_silent(temp_db, mocker):
    _mhde_with_fresh_fx(temp_db)
    sent = mocker.patch.object(CR.alert, "send_text", return_value=False)
    res = CR.run_continuous(mhde_conn=temp_db, engine_conn=_engine(), now=NOW)
    assert not res.has_red
    assert [s.status for s in res.steps] == [Status.GREEN, Status.GREEN, Status.GREEN]
    # main() must not send
    mocker.patch.object(CR, "run_continuous", return_value=res)
    rc = CR.main()
    assert rc == 0
    sent.assert_not_called()


# ── any red → one alert ───────────────────────────────────────────────
def test_stale_fx_bar_triggers_alert(temp_db, mocker):
    _mhde_with_fresh_fx(temp_db, bar_dt=datetime(TODAY.year, TODAY.month, TODAY.day, 4, 0, 0))  # 6h stale
    res = CR.run_continuous(mhde_conn=temp_db, engine_conn=_engine(), now=NOW)
    assert res.has_red
    assert res.steps[0].status is Status.RED
    # engine checks still evaluated (no cascade) and green
    assert res.steps[1].status is Status.GREEN and res.steps[2].status is Status.GREEN

    mocker.patch.object(CR, "run_continuous", return_value=res)
    sent = mocker.patch.object(CR.alert, "send_text", return_value=False)
    assert CR.main() == 1
    sent.assert_called_once()
    assert sent.call_args[0][0].startswith("🔴 Continuous Pipeline")


def test_engine_monitor_stale_triggers_alert(temp_db):
    _mhde_with_fresh_fx(temp_db)
    res = CR.run_continuous(mhde_conn=temp_db, engine_conn=_engine(monitor_age_min=30), now=NOW)
    assert res.has_red
    assert res.steps[0].status is Status.GREEN
    assert res.steps[1].status is Status.RED and "looks down" in res.steps[1].detail


def test_engine_entry_missing_triggers_alert(temp_db):
    _mhde_with_fresh_fx(temp_db)
    res = CR.run_continuous(mhde_conn=temp_db, engine_conn=_engine(entry_today=False), now=NOW)
    assert res.has_red
    assert res.steps[2].status is Status.RED and "no successful engine 'entry' run today" in res.steps[2].detail


def test_engine_entry_not_due_yet_is_green(temp_db):
    _mhde_with_fresh_fx(temp_db, bar_dt=datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30, 0))
    early = datetime(2026, 5, 12, 7, 0, 0, tzinfo=timezone.utc)  # before 08:00 cutoff
    res = CR.run_continuous(mhde_conn=temp_db, engine_conn=_engine(entry_today=False), now=early)
    assert res.steps[2].status is Status.GREEN and "not due yet" in res.steps[2].detail
    assert not res.has_red


def test_engine_db_unreachable_is_red(temp_db, mocker):
    _mhde_with_fresh_fx(temp_db)
    mocker.patch.object(CR.C, "open_engine_db", side_effect=RuntimeError("no such file"))
    res = CR.run_continuous(mhde_conn=temp_db, now=NOW)
    assert res.has_red
    assert res.steps[1].status is Status.RED and "unreadable after retries" in res.steps[1].detail
    assert res.steps[2].status is Status.RED
