"""Unit tests for the 6 production monitors in monitoring/.

Each test exercises one monitor's pure-logic path with the temp_db
fixture and asserts the MonitorResult shape. mock_telegram captures
any would-be Telegram sends so we never hit the real API.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from monitoring import alert as alert_mod
from monitoring.alert import MonitorResult


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


# ──────────────────────────────────────────────────────────────────────
# alert.py shape + dispatch behavior
# ──────────────────────────────────────────────────────────────────────


def test_monitor_result_to_telegram_text_includes_severity_prefix():
    r = MonitorResult(monitor="x", status="warn", severity="warn",
                       title="t", body="b")
    text = r.to_telegram_text()
    assert "[!] MHDE monitor: x" in text
    assert "t" in text
    assert "b" in text


def test_send_alert_skips_ok_results(mock_telegram):
    r = MonitorResult(monitor="x", status="ok", severity="info", title="t")
    sent = alert_mod.send_alert(r)
    assert sent is False
    assert mock_telegram == []


def test_send_alert_dry_run_does_not_call_telegram(mock_telegram, monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")
    r = MonitorResult(monitor="x", status="fail", severity="critical",
                       title="failure")
    sent = alert_mod.send_alert(r)
    assert sent is False  # dry-run suppresses
    # mock_telegram captures requests.post — none should be invoked
    assert mock_telegram == []


# ──────────────────────────────────────────────────────────────────────
# dashboard_consistency
# ──────────────────────────────────────────────────────────────────────


def test_dashboard_consistency_ok_on_empty_db(temp_db):
    """Empty DB has no candidate_outcomes — dashboard and direct query
    both return 0; no mismatch. Empty engines also produce no
    column-completeness issues (an empty engine is pipeline_execution's
    concern, not dashboard_consistency's)."""
    from monitoring import dashboard_consistency
    result = dashboard_consistency.run(conn=temp_db)
    assert result.monitor == "dashboard_consistency"
    assert result.status == "ok"


def _seed_equity_pending_row(temp_db, ticker, pred_date, horizon, with_maturity_date):
    """Helper: insert one ticker's prices_daily entry and one pending
    ml_predictions row. `with_maturity_date=True` means we also seed
    enough trading rows so the JOIN resolves maturity (matured-style);
    `False` means the JOIN returns NULL (the May 9 bug shape — pending
    with no future rows yet)."""
    from datetime import timedelta
    rows = [(pred_date, 100.0)]
    if with_maturity_date:
        # 5 weekday rows after pred_date so 5d horizon resolves.
        cur = pred_date + timedelta(days=1)
        while len(rows) < 7:
            if cur.weekday() < 5:
                rows.append((cur, 100.0 + len(rows)))
            cur += timedelta(days=1)
    for i, (d, c) in enumerate(rows):
        temp_db.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, close, adjusted_close) "
            "VALUES (?, ?, ?, ?, ?)",
            [f"{ticker}-{i}", ticker, d, c, c],
        )
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, ?, 'm1', ?, 0.65, 0.05)",
        [ticker, pred_date, horizon],
    )


def test_dashboard_consistency_flags_all_null_maturity_date(temp_db, monkeypatch):
    """Regression for the May 9 equity bug: pending equity rows with
    NULL maturity_date are flagged. We bypass the busday-offset
    backfill in queries.py so the column STAYS NULL the way it did
    before the fix landed — and assert dashboard_consistency catches it."""
    from datetime import date

    # Patch out the post-processor that fills the estimate so we can
    # simulate the pre-fix state.
    from dashboard.services import queries as q
    monkeypatch.setattr(
        q, "_fill_estimated_equity_maturity", lambda df, prediction_date_value=None: df
    )

    pred_date = date(2026, 5, 8)
    _seed_equity_pending_row(temp_db, "AAA", pred_date, "5d", with_maturity_date=False)
    _seed_equity_pending_row(temp_db, "BBB", pred_date, "5d", with_maturity_date=False)

    from monitoring import dashboard_consistency
    result = dashboard_consistency.run(conn=temp_db)
    assert result.status == "fail"
    assert "maturity_date" in result.body
    assert "all-NULL" in result.body or "NULL" in result.body
    # Per-horizon labelling is expected.
    assert "equity/5d" in result.body


def test_dashboard_consistency_ok_with_filled_maturity_for_pending(temp_db):
    """Pending equity rows with maturity_date populated (via the
    busday-offset estimator in get_equity_predictions) and
    price_at_maturity correctly NULL — no failures."""
    from datetime import date
    pred_date = date(2026, 5, 8)
    _seed_equity_pending_row(temp_db, "AAA", pred_date, "5d", with_maturity_date=False)

    from monitoring import dashboard_consistency
    result = dashboard_consistency.run(conn=temp_db)
    # Only 1 ticker, 1 horizon — get_equity_predictions's
    # _fill_estimated_equity_maturity puts the May 15 estimate in.
    # The realized columns are NULL but the row is pending so no flag.
    assert result.status == "ok", f"got {result.status}: {result.body}"


def test_dashboard_consistency_pct_move_rendering_for_pending(temp_db):
    """Pending row with current_price populated — pct_move format helper
    renders +/- N% (or "+0.00%" for same-day same-price). Should NOT
    flag — "+0.00%" is a valid render; only an empty result fails."""
    from datetime import date
    pred_date = date(2026, 5, 8)
    _seed_equity_pending_row(temp_db, "AAA", pred_date, "5d", with_maturity_date=False)

    # Sanity: confirm the format helper renders non-empty for this row.
    from dashboard.services.queries import get_equity_predictions
    df = get_equity_predictions(temp_db, pred_date)
    assert not df.empty
    from monitoring.dashboard_consistency import _check_pct_move_string
    assert _check_pct_move_string(df.iloc[0], "equity") is True


# ──────────────────────────────────────────────────────────────────────
# streamlit_freshness
# ──────────────────────────────────────────────────────────────────────


def test_streamlit_freshness_ok_when_process_post_dates_commit():
    """Process started after the latest commit → ok."""
    from datetime import timedelta as _td
    from monitoring import streamlit_freshness
    commit = datetime(2026, 5, 9, 9, 0, 0, tzinfo=timezone.utc)
    process = commit + _td(hours=1)
    result = streamlit_freshness.run(
        process_start=process, latest_commit=commit
    )
    assert result.status == "ok"
    assert result.metrics["lag_hours"] <= 0


def test_streamlit_freshness_ok_when_lag_under_threshold():
    """Lag of 2h with default 4h threshold → ok."""
    from datetime import timedelta as _td
    from monitoring import streamlit_freshness
    process = datetime(2026, 5, 9, 7, 0, 0, tzinfo=timezone.utc)
    commit  = datetime(2026, 5, 9, 9, 0, 0, tzinfo=timezone.utc)
    result = streamlit_freshness.run(
        process_start=process, latest_commit=commit
    )
    assert result.status == "ok"
    assert result.metrics["lag_hours"] == 2.0


def test_streamlit_freshness_warns_when_lag_exceeds_threshold():
    """Lag of 18h (the May 9 incident) → warn."""
    from datetime import timedelta as _td
    from monitoring import streamlit_freshness
    process = datetime(2026, 5, 8, 15, 20, 0, tzinfo=timezone.utc)
    commit  = datetime(2026, 5, 9,  9,  0, 0, tzinfo=timezone.utc)
    result = streamlit_freshness.run(
        process_start=process, latest_commit=commit
    )
    assert result.status == "fail"
    assert result.severity == "warn"
    assert "Restart with" in result.body
    assert "systemctl --user restart" in result.body
    assert result.metrics["lag_hours"] > 4


def test_streamlit_freshness_handles_unreadable_inputs():
    """When pgrep / git are unreadable, the monitor warns instead
    of crashing."""
    from monitoring import streamlit_freshness
    # Both inputs left as None forces the real-subprocess path. We can
    # at least verify the result structure doesn't raise. Whether the
    # subprocess succeeds depends on the host; in CI both are likely to
    # work; on a stripped-down test env they may not. Either way, the
    # call returns a MonitorResult.
    result = streamlit_freshness.run()
    assert result.monitor == "streamlit_freshness"
    assert result.status in ("ok", "warn", "fail")


def test_streamlit_freshness_proc_parser_handles_paren_comm(tmp_path, monkeypatch):
    """The /proc/PID/stat comm field (#2) is in parens but may itself
    contain spaces or close-parens. The parser must use rindex to
    pick the LAST `)` to terminate comm reliably."""
    from monitoring import streamlit_freshness

    # Build a synthetic /proc/<pid>/stat line where comm = "weird(name)"
    # and starttime (field 22) is a known value. That's 100 ticks.
    pid = 12345
    proc_dir = tmp_path / "proc" / str(pid)
    proc_dir.mkdir(parents=True)
    # Layout: pid (comm) state ppid pgrp session tty_nr tpgid flags
    #         minflt cminflt majflt cmajflt utime stime cutime cstime
    #         priority nice num_threads itrealvalue starttime ...
    # We need 22 fields; pad with 0 placeholders for #3..#21 and put
    # 100 at #22. Total tokens after the close-paren = 20 (state +
    # 18 zeros + starttime). Plus we need a lot more fields after
    # starttime for proc(5) compatibility, but the parser only reads
    # field 22 so trailing fields don't matter.
    fields_after_comm = ["S"] + ["0"] * 18 + ["100"]  # state + ... + starttime=100
    line = f"{pid} (weird(name)) " + " ".join(fields_after_comm) + "\n"
    (proc_dir / "stat").write_text(line)

    # Also need a /proc/uptime — point both reads through tmp_path.
    (tmp_path / "proc" / "uptime").write_text("1000.0 5000.0\n")

    # Monkey-patch the open() calls inside the parser to redirect
    # /proc/<pid>/stat and /proc/uptime to tmp_path.
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == f"/proc/{pid}/stat":
            return real_open(proc_dir / "stat", *args, **kwargs)
        if path == "/proc/uptime":
            return real_open(tmp_path / "proc" / "uptime", *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    dt, err = streamlit_freshness._read_proc_start_time(pid)
    assert err is None, f"unexpected error: {err}"
    assert dt is not None
    # boot_epoch = now - 1000s; start_epoch = boot_epoch + (100 / clock_hz).
    # We don't pin an exact value (clock_hz varies), but the result
    # must be very close to "1000s ago" (i.e., within a few seconds).
    from datetime import datetime as _dt, timezone as _tz
    age_s = (_dt.now(tz=_tz.utc) - dt).total_seconds()
    assert 990 <= age_s <= 1010, f"age {age_s}s out of expected ~1000s range"


# ──────────────────────────────────────────────────────────────────────
# dashboard_synthetic
# ──────────────────────────────────────────────────────────────────────


def test_dashboard_synthetic_ok_on_empty_db_skip_http(temp_db):
    """Empty DB: helpers return no rows, no failures, http skipped."""
    from monitoring import dashboard_synthetic
    result = dashboard_synthetic.run(conn=temp_db, skip_http=True)
    assert result.status == "ok"
    for engine in ("equity", "crypto", "fx"):
        assert result.metrics[engine]["rows"] == 0


def test_dashboard_synthetic_flags_helper_raise(temp_db, monkeypatch):
    """If a helper raises, the monitor reports it cleanly."""
    from monitoring import dashboard_synthetic
    from dashboard.services import queries as q

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated helper failure")
    monkeypatch.setattr(q, "get_equity_predictions", _boom)

    # Seed enough rows so we DO call get_equity_predictions (it's
    # short-circuited when ml_predictions is empty).
    from datetime import date
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('XYZ', ?, 'm1', '5d', 0.6, 0.05)",
        [date(2026, 5, 8)],
    )

    result = dashboard_synthetic.run(conn=temp_db, skip_http=True)
    assert result.status == "fail"
    assert "equity: helper raised" in result.body
    assert "RuntimeError" in result.body


def test_dashboard_synthetic_flags_all_null_key_column(temp_db, monkeypatch):
    """If a key column is entirely NULL in the helper result, monitor
    flags it. Mirrors the May 9 maturity-date failure."""
    from monitoring import dashboard_synthetic
    from dashboard.services import queries as q
    import pandas as pd

    def _stub_equity(conn, prediction_date):
        return pd.DataFrame({
            "ticker": ["AAA", "BBB"],
            "horizon": ["5d", "5d"],
            "predicted_probability": [0.6, 0.7],
            "price_at_prediction": [100.0, 200.0],
            "maturity_date": [None, None],   # all-NULL — the bug
            "outcome_filled_at": [None, None],
        })
    monkeypatch.setattr(q, "get_equity_predictions", _stub_equity)

    from datetime import date
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [date(2026, 5, 8)],
    )

    result = dashboard_synthetic.run(conn=temp_db, skip_http=True)
    assert result.status == "fail"
    assert "maturity_date" in result.body
    assert "all-NULL" in result.body


# ──────────────────────────────────────────────────────────────────────
# cross_artifact
# ──────────────────────────────────────────────────────────────────────


def _seed_minimal_health_data(temp_db):
    """Insert a minimal set of equity/crypto/fx rows so the
    health_check internals all return ok=True. Returns the
    (equity_date, today, fx_dt) tuple used.

    Equity seed uses expected_equity_prediction_date(now) so that
    _check_equity (which also calls that helper) returns ok=True
    regardless of day-of-week (KI-128).
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pipelines.market_calendar import expected_equity_prediction_date
    now = _dt.now(tz=_tz.utc)
    equity_date = expected_equity_prediction_date(now)
    today = now.date()
    fx_dt = now.replace(
        minute=5, second=0, microsecond=0, tzinfo=None
    )

    # Equity: one prediction for the expected weekday date.
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [equity_date],
    )
    # Crypto: one prediction for today (latest).
    temp_db.execute(
        "INSERT INTO crypto_ml_predictions (symbol, prediction_date, "
        "model_id, horizon, predicted_probability, prediction_threshold) "
        "VALUES ('BTCUSDT', ?, 'c1', '5d', 0.6, 0.05)",
        [today],
    )
    # FX: one fresh prediction within the 2h window the check uses.
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    return equity_date, today, fx_dt


def test_cross_artifact_ok_when_health_strings_agree_with_db(temp_db):
    """Vanilla case: all three engines have rows, the format strings
    match what direct DB queries return → ok."""
    _seed_minimal_health_data(temp_db)
    from monitoring import cross_artifact
    result = cross_artifact.run(conn=temp_db)
    # services check is stubbed inside the monitor; equity/crypto/fx
    # all derive from the seeded rows.
    assert result.status == "ok", f"got {result.status}: {result.body}"


def test_cross_artifact_flags_equity_count_mismatch(temp_db, monkeypatch):
    """Simulate a formatter typo: detail string says 99 predictions
    but the DB has 1. cross_artifact must catch the disagreement."""
    equity_date, _, _ = _seed_minimal_health_data(temp_db)

    from pipelines import health_check as hc
    real_check_equity = hc._check_equity

    def _typo_check_equity(conn):
        result = real_check_equity(conn)
        # Inject a wrong count into the detail string.
        return type(result)(
            name=result.name, ok=result.ok,
            detail=f"99 predictions for {equity_date}",
        )

    monkeypatch.setattr(hc, "_check_equity", _typo_check_equity)

    from monitoring import cross_artifact
    result = cross_artifact.run(conn=temp_db)
    assert result.status == "fail"
    assert "equity" in result.body
    assert "claims 99" in result.body or "99 predictions" in result.body


def test_cross_artifact_handles_format_evolution(temp_db, monkeypatch):
    """If the detail format changes such that the regex no longer
    matches, the monitor must NOT raise — it should pass through
    quietly (or fall back to OK if no other check fails). The point
    of this case: the operator notices format drift via the regex
    failing to match, but the monitor itself stays robust."""
    _seed_minimal_health_data(temp_db)

    from pipelines import health_check as hc
    real_check_crypto = hc._check_crypto

    def _new_format(conn):
        result = real_check_crypto(conn)
        return type(result)(
            name=result.name, ok=result.ok,
            detail="(crypto detail in a future format unrecognized by cross_artifact)",
        )

    monkeypatch.setattr(hc, "_check_crypto", _new_format)

    from monitoring import cross_artifact
    result = cross_artifact.run(conn=temp_db)
    # The crypto detail no longer parses → no crypto-side claims to
    # verify → no crypto-side issue is raised. Equity and FX still
    # ok. But _format_message now includes the new crypto detail, so
    # the message-includes check still passes (we look for the new
    # string in the message, which is what _format_message produces).
    assert result.status == "ok"


# ──────────────────────────────────────────────────────────────────────
# dashboard_consistency: filled row with missing realized
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# phase0_calibration
# ──────────────────────────────────────────────────────────────────────


def _seed_phase0_model(conn, model_id, horizon, **kwargs):
    """Insert a crypto active model with optional metric overrides."""
    defaults = dict(
        target_threshold=0.10, base_rate=0.30,
        precision_at_threshold=0.60, lift_over_base=2.0,
    )
    defaults.update(kwargs)
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs
            (model_id, horizon, target_threshold, base_rate,
             precision_at_threshold, lift_over_base,
             train_start, train_end, test_start, test_end, is_active)
        VALUES (?, ?, ?, ?, ?, ?,
                '2024-01-01', '2025-04-04',
                '2025-04-05', '2025-04-30', true)
        """,
        [model_id, horizon, defaults["target_threshold"],
         defaults["base_rate"], defaults["precision_at_threshold"],
         defaults["lift_over_base"]],
    )


def _seed_phase0_predictions(conn, model_id, horizon, items):
    """items: list of (predicted_probability, hit_bool, days_ago_filled)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from datetime import date as _date
    now = _dt.now(tz=_tz.utc)
    for i, (prob, hit, days_ago) in enumerate(items):
        filled_at = now - _td(days=days_ago)
        conn.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 actual_hit, outcome_filled_at, market_cap_bucket)
            VALUES (?, ?, ?, ?, ?, 0.10, ?, ?, 'unknown')
            """,
            [f"PH0COIN{i:04d}USDT",
             _date.today() - _td(days=i % 21),
             model_id, horizon, prob, hit, filled_at],
        )


def test_phase0_monitor_ok_when_no_active_models(temp_db):
    from monitoring import phase0_calibration
    result = phase0_calibration.run(conn=temp_db)
    assert result.status == "ok"
    assert result.metrics["n_active_models"] == 0


def test_phase0_monitor_ok_when_no_drift_no_threshold(temp_db):
    """Healthy active model below 200 samples, no drift triggers."""
    _seed_phase0_model(temp_db, "crypto_5d_test", "5d",
                       precision_at_threshold=0.60, base_rate=0.30)
    # 100 filled, hit rate 0.60 → ratio 1.00 (no drift), lift 2.0× (no drift)
    items = [(0.62, True, 3)] * 60 + [(0.62, False, 3)] * 40
    _seed_phase0_predictions(temp_db, "crypto_5d_test", "5d", items)
    from monitoring import phase0_calibration
    result = phase0_calibration.run(conn=temp_db)
    assert result.status == "ok", f"got {result.status}: {result.body}"


def test_phase0_monitor_warns_on_lift_drift(temp_db):
    """Lift in last 30d below 1.5 → drift alert."""
    _seed_phase0_model(temp_db, "crypto_5d_test", "5d",
                       precision_at_threshold=0.60, base_rate=0.30)
    # Lift = 0.40 / 0.30 = 1.33 — below 1.5 threshold
    items = [(0.62, True, 3)] * 40 + [(0.62, False, 3)] * 60
    _seed_phase0_predictions(temp_db, "crypto_5d_test", "5d", items)
    from monitoring import phase0_calibration
    result = phase0_calibration.run(conn=temp_db)
    assert result.status == "warn"
    assert result.severity == "warn"
    assert "lift" in result.body.lower()
    assert "1.33" in result.body


def test_phase0_monitor_warns_on_precision_ratio_drift(temp_db):
    """Rolling precision / baseline below 0.85 → drift alert."""
    _seed_phase0_model(temp_db, "crypto_5d_test", "5d",
                       precision_at_threshold=0.80, base_rate=0.30)
    # 60 hits / 100 = 0.60 — ratio 0.75 against baseline 0.80
    items = [(0.62, True, 3)] * 60 + [(0.62, False, 3)] * 40
    _seed_phase0_predictions(temp_db, "crypto_5d_test", "5d", items)
    from monitoring import phase0_calibration
    result = phase0_calibration.run(conn=temp_db)
    assert result.status == "warn"
    assert "rolling precision" in result.body.lower()
    assert "ratio" in result.body.lower()


def test_phase0_monitor_threshold_reached_one_shot(temp_db):
    """When n_filled crosses 200 for a model that hasn't fired the
    milestone, monitor emits info alert. Subsequent runs do NOT
    re-fire the same alert (idempotency via phase0_milestones)."""
    _seed_phase0_model(temp_db, "crypto_5d_test", "5d",
                       precision_at_threshold=0.60, base_rate=0.30)
    # 200 filled, healthy hit rate so no drift signals fire
    items = [(0.62, True, 3)] * 120 + [(0.62, False, 3)] * 80
    _seed_phase0_predictions(temp_db, "crypto_5d_test", "5d", items)

    from monitoring import phase0_calibration
    first = phase0_calibration.run(conn=temp_db)
    assert first.status == "warn"
    assert first.severity == "info"
    assert "Phase 0 sample threshold reached" in first.title
    assert "phase0-report" in first.body

    # Second run — should NOT re-fire the threshold-reached alert
    second = phase0_calibration.run(conn=temp_db)
    assert second.status == "ok", f"got {second.status}: {second.body}"


def test_phase0_monitor_records_eta_for_slip_detection(temp_db):
    """ETA from the projection is persisted into phase0_milestones
    every run; week-over-week slip detection compares against it."""
    _seed_phase0_model(temp_db, "crypto_5d_test", "5d",
                       precision_at_threshold=0.60, base_rate=0.30)
    # 50 filled spread across recent week so a projection ETA exists
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from datetime import date as _date
    now = _dt.now(tz=_tz.utc)
    for i in range(50):
        temp_db.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 actual_hit, outcome_filled_at, market_cap_bucket)
            VALUES (?, ?, 'crypto_5d_test', '5d', 0.62, 0.10,
                    ?, ?, 'unknown')
            """,
            [f"PH0ETACOIN{i:04d}USDT",
             _date.today() - _td(days=i % 21),
             i % 2 == 0,
             now - _td(days=i % 7)],
        )
    from monitoring import phase0_calibration
    phase0_calibration.run(conn=temp_db)
    row = temp_db.execute(
        "SELECT detail FROM phase0_milestones "
        "WHERE engine = 'crypto' AND model_id = 'crypto_5d_test' "
        "AND milestone = 'last_eta_projection'"
    ).fetchone()
    assert row is not None
    assert row[0] is not None  # an ISO date string


def test_dashboard_consistency_flags_filled_row_missing_realized(temp_db):
    """A filled row (outcome_filled_at set) without actual_max_return
    should be flagged — fill_outcomes is the writer that should have
    populated it."""
    from datetime import date, timedelta
    pred_date = date(2026, 4, 1)
    _seed_equity_pending_row(temp_db, "AAA", pred_date, "5d", with_maturity_date=True)
    # Mark filled but DON'T set actual_max_return.
    temp_db.execute(
        "UPDATE ml_predictions SET outcome_filled_at = CURRENT_TIMESTAMP "
        "WHERE ticker = 'AAA'"
    )

    from monitoring import dashboard_consistency
    result = dashboard_consistency.run(conn=temp_db)
    assert result.status == "fail"
    assert "actual_max_return" in result.body


# ──────────────────────────────────────────────────────────────────────
# pipeline_execution
# ──────────────────────────────────────────────────────────────────────


def test_pipeline_execution_flags_empty_engines(temp_db):
    """All three prediction tables empty → flagged."""
    from monitoring import pipeline_execution
    result = pipeline_execution.run(conn=temp_db)
    assert result.status in ("warn", "fail")
    body = result.body.lower()
    for engine in ("equity", "crypto", "fx"):
        assert engine in body


def test_pipeline_execution_ok_when_fresh(temp_db):
    from monitoring import pipeline_execution
    # Seed an is_active=true model in each engine's *_model_runs table.
    # The monitor JOINs predictions to model_runs WHERE is_active=true so
    # the baseline reflects production scoring only (KI-118 lesson +
    # monitor false-positive fix 2026-05-09).
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '20d', 0.10, '/tmp/x.joblib', true)"
    )
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '5d', 0.10, '/tmp/x.joblib', true)"
    )
    temp_db.execute(
        "INSERT INTO fx_ml_model_runs (model_id, direction, horizon, "
        "target_pips, model_path, is_active) "
        "VALUES ('fx_m1', 'up', '24h', 20, '/tmp/x.joblib', true)"
    )

    today = date.today()
    # 30 rows for the last 14 days each → high baseline
    for d_offset in range(15):
        d = today - timedelta(days=d_offset)
        for i in range(30):
            temp_db.execute(
                "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
                "horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [f"T{i}", d, "m1", "20d", 0.6 + 0.01 * i, 0.10],
            )
            temp_db.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, "
                "horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [f"S{i}USDT", d, "m1", "5d", 0.6, 0.10],
            )
    # FX: hourly bars over the past 27 hours
    now = datetime.utcnow().replace(minute=5, second=0, microsecond=0)
    for h_offset in range(27):
        bar = now - timedelta(hours=h_offset)
        temp_db.execute(
            "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, "
            "horizon, predicted_probability, prediction_threshold) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [bar, "fx_m1", "up", "24h", 0.6, 20],
        )

    result = pipeline_execution.run(conn=temp_db, now=datetime.utcnow().replace(tzinfo=timezone.utc))
    # All three engines have recent rows above threshold — ok.
    assert result.status == "ok", f"got {result.status}: {result.body}"


# ──────────────────────────────────────────────────────────────────────
# config_drift
# ──────────────────────────────────────────────────────────────────────


def test_config_drift_runs_without_crashing():
    """In a CI environment the deployed dirs may not exist — monitor
    must still return a structured result."""
    from monitoring import config_drift
    result = config_drift.run()
    assert result.monitor == "config_drift"
    assert result.status in ("ok", "warn")


# ──────────────────────────────────────────────────────────────────────
# model_performance
# ──────────────────────────────────────────────────────────────────────


def test_model_performance_skips_when_no_active_models(temp_db):
    from monitoring import model_performance
    result = model_performance.run(conn=temp_db)
    assert result.status == "ok"  # no models to fail


def test_model_performance_flags_degradation(temp_db):
    """High baseline + low rolling = ratio < 0.8 → warn."""
    from monitoring import model_performance
    # Active model with baseline 0.40 precision
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active, precision_at_threshold) "
        "VALUES ('m1', '20d', 0.10, '/tmp/x.joblib', true, 0.40)"
    )
    # Last 7 days: 10 predictions filled, 1 hit → precision 0.10 (well below 0.32 threshold).
    today = date.today()
    for i in range(10):
        d = today - timedelta(days=i + 1)
        temp_db.execute(
            "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
            "horizon, predicted_probability, prediction_threshold, "
            "actual_hit, outcome_filled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [f"T{i}", d, "m1", "20d", 0.7, 0.10,
             (i == 0), datetime.now(timezone.utc) - timedelta(days=i)],
        )
    result = model_performance.run(conn=temp_db)
    assert result.status == "warn"
    assert "ratio" in result.body or "baseline" in result.body


# ──────────────────────────────────────────────────────────────────────
# data_quality
# ──────────────────────────────────────────────────────────────────────


def test_data_quality_flags_empty_tables(temp_db):
    from monitoring import data_quality
    result = data_quality.run(conn=temp_db)
    assert result.status == "warn"
    # All three engines flagged as empty
    body_lower = result.body.lower()
    for engine in ("equity", "crypto", "fx"):
        assert engine in body_lower


# ──────────────────────────────────────────────────────────────────────
# smoke_test
# ──────────────────────────────────────────────────────────────────────


def test_smoke_test_fails_without_active_models(temp_db):
    """No active model rows → smoke fails."""
    from monitoring import smoke_test
    result = smoke_test.run(conn=temp_db)
    # DB opens fine, dashboard query works, but no active models for any
    # engine → fail.
    assert result.status == "fail"
    assert "no active model" in result.body.lower()


def test_smoke_test_flags_missing_joblib(temp_db, tmp_path):
    """Active model row pointing at a nonexistent joblib → fail."""
    from monitoring import smoke_test
    fake_path = tmp_path / "does_not_exist.joblib"
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '20d', 0.10, ?, true)",
        [str(fake_path)],
    )
    result = smoke_test.run(conn=temp_db)
    assert result.status == "fail"
    assert "missing" in result.body.lower() or "path" in result.body.lower()
