"""ADR-039 §D layer-2 — peer-asymmetry dead-shard detector (pure logic).

With symbol sharding, one HUNG (not crashed) shard = a permanent gap for its ~1/N symbols,
invisible to systemd Restart=. The tell is asymmetry: its partitions (rows written) stop
advancing while peers flow. ``evaluate`` decides alerts from the shard heartbeats + systemd
unit states; it is pure (all I/O injected) so every branch is unit-tested here. Rows is the
"advance" signal (monotonic across a connection-manager rebuild), not the mgr-scoped dispatched.
"""
from __future__ import annotations

from crypto.research.capture_core.stall_detector import (
    evaluate, read_heartbeats, run_check)

SEC = 1_000_000_000
NOW = 1_000 * SEC
SHARDS = [str(i) for i in range(3)]          # a 3-shard universe for brevity


def _hb(ts_ns, rows, dispatched=100, bytes_in=0):
    return {"ts_ns": ts_ns, "dispatched": dispatched, "bytes_in": bytes_in, "rows": rows}


def _common(**over):
    base = dict(
        heartbeats={s: _hb(NOW, rows=100 + int(s)) for s in SHARDS},     # all rows advanced
        failed_units=[],
        prev={s: {"dispatched": 50, "rows": 50 + int(s)} for s in SHARDS},
        now_ns=NOW,
        expected_shards=SHARDS,
        interval_s=10.0,
        stale_factor=3,
    )
    base.update(over)
    return base


def test_healthy_all_fresh_and_advancing_yields_no_alerts():
    alerts, state = evaluate(**_common())
    assert alerts == []
    assert state["0"]["rows"] == 100             # new baseline returned for next run


def test_failed_unit_alerts():
    alerts, _ = evaluate(**_common(failed_units=["mhde-capture-core@1.service"]))
    assert any("mhde-capture-core@1.service" in a and "failed" in a for a in alerts)


def test_missing_heartbeat_alerts():
    hb = {s: _hb(NOW, rows=100) for s in SHARDS if s != "2"}     # shard 2 never reported
    alerts, _ = evaluate(**_common(heartbeats=hb))
    assert any("2" in a and "heartbeat" in a.lower() for a in alerts)


def test_stale_heartbeat_alerts():
    hb = {s: _hb(NOW, rows=100) for s in SHARDS}
    hb["1"] = _hb(NOW - 40 * SEC, rows=100)                     # 40s old > 3*10s
    alerts, _ = evaluate(**_common(heartbeats=hb))
    assert any("1" in a and "stale" in a.lower() for a in alerts)


def test_peer_asymmetry_one_stalled_while_peers_flow_alerts():
    # shards 0,2 advanced (rows up); shard 1 did NOT (rows == prev) though fresh -> stalled.
    hb = {"0": _hb(NOW, rows=200), "1": _hb(NOW, rows=51), "2": _hb(NOW, rows=202)}
    prev = {"0": {"dispatched": 0, "rows": 100}, "1": {"dispatched": 0, "rows": 51},
            "2": {"dispatched": 0, "rows": 100}}
    alerts, _ = evaluate(**_common(heartbeats=hb, prev=prev))
    assert any("1" in a and "stall" in a.lower() for a in alerts)
    assert not any(("0" in a or "2" in a) and "stall" in a.lower() for a in alerts)


def test_global_lull_no_peer_advances_is_not_asymmetry():
    # NONE advanced (e.g. a market-wide socket reconnect) -> NOT a per-shard stall.
    hb = {s: _hb(NOW, rows=100) for s in SHARDS}
    prev = {s: {"dispatched": 0, "rows": 100} for s in SHARDS}
    alerts, _ = evaluate(**_common(heartbeats=hb, prev=prev))
    assert not any("stall" in a.lower() for a in alerts)


def test_first_run_empty_prev_skips_asymmetry():
    hb = {s: _hb(NOW, rows=100) for s in SHARDS}
    alerts, state = evaluate(**_common(heartbeats=hb, prev={}))
    assert not any("stall" in a.lower() for a in alerts)        # no baseline yet
    assert set(state) == set(SHARDS)                            # but baseline IS recorded


# -- read_heartbeats (real file I/O) + run_check (injectable orchestration) ----

def test_read_heartbeats_parses_shard_files_and_skips_junk(tmp_path):
    (tmp_path / "shard-0.json").write_text('{"ts_ns": 1, "dispatched": 2, "rows": 3}')
    (tmp_path / "shard-7.json").write_text('{"ts_ns": 9, "dispatched": 8, "rows": 7}')
    (tmp_path / "shard-bad.json").write_text("{ not json")        # ignored, not fatal
    (tmp_path / "unrelated.txt").write_text("x")                  # ignored
    hbs = read_heartbeats(str(tmp_path))
    assert set(hbs) == {"0", "7"}
    assert hbs["7"]["rows"] == 7


def test_read_heartbeats_missing_dir_is_empty(tmp_path):
    assert read_heartbeats(str(tmp_path / "nope")) == {}


def test_run_check_alerts_on_failed_unit_and_persists_baseline():
    sent, saved = [], {}
    alerts = run_check(
        heartbeat_dir="/x", expected_shards=["0", "1"],
        unit_names=["mhde-capture-core@0.service", "mhde-capture-core@1.service"],
        interval_s=10.0, stale_factor=3, state_path="/s", now_ns=NOW,
        read_heartbeats_fn=lambda d: {"0": _hb(NOW, rows=10), "1": _hb(NOW, rows=10)},
        unit_failed_fn=lambda u: u.endswith("@1.service"),
        send_fn=lambda t: sent.append(t),
        load_state_fn=lambda p: {},
        save_state_fn=lambda p, s: saved.update(s),
    )
    assert any("@1.service" in a and "failed" in a for a in alerts)
    assert sent and "@1.service" in sent[0]            # one combined Telegram message
    assert "0" in saved and "1" in saved               # baseline persisted for next run


def test_run_check_healthy_sends_nothing_but_persists_baseline():
    sent, saved = [], {}
    alerts = run_check(
        heartbeat_dir="/x", expected_shards=["0", "1"],
        unit_names=["a", "b"], interval_s=10.0, stale_factor=3, state_path="/s", now_ns=NOW,
        read_heartbeats_fn=lambda d: {"0": _hb(NOW, rows=10), "1": _hb(NOW, rows=11)},
        unit_failed_fn=lambda u: False,
        send_fn=lambda t: sent.append(t),
        load_state_fn=lambda p: {"0": {"rows": 5, "dispatched": 0},
                                 "1": {"rows": 5, "dispatched": 0}},
        save_state_fn=lambda p, s: saved.update(s),
    )
    assert alerts == []
    assert sent == []                                  # no Telegram on a healthy run
    assert saved["0"]["rows"] == 10


def test_capture_stall_check_cli_wires_run_check(monkeypatch):
    import main
    from click.testing import CliRunner
    from crypto.research.capture_core import stall_detector as sd

    captured = {}

    def fake_run_check(**kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(sd, "run_check", fake_run_check)
    r = CliRunner().invoke(main.cli, ["crypto", "capture-stall-check", "--of", "8"])
    assert r.exit_code == 0, r.output
    assert captured["expected_shards"] == [str(i) for i in range(8)]
    assert "mhde-capture-owner.service" in captured["unit_names"]
    assert "mhde-capture-core@7.service" in captured["unit_names"]
    assert len(captured["unit_names"]) == 9            # owner + 8 shards


def test_capture_stall_check_cli_rejects_bad_of(monkeypatch):
    import main
    from click.testing import CliRunner
    r = CliRunner().invoke(main.cli, ["crypto", "capture-stall-check", "--of", "0"])
    assert r.exit_code != 0
