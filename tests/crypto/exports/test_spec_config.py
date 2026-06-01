"""Tests for crypto.exports.spec_config — shape and value invariants
of the static spec fields.

The Phase 1B winner run_id is hardcoded here; updates require an
explicit code edit + commit. Risk envelope values come from
INTERFACE.md §2 example (see DECISIONS.md ADR for justification).
"""
from __future__ import annotations

from crypto.exports import spec_config as sc


def test_spec_version_is_semver_string():
    assert sc.SPEC_VERSION == "1.0.0"


def test_phase1b_winner_run_id_pinned():
    assert sc.PHASE1B_WINNER_RUN_ID == "backtest_10d_D_top_n_a02e15a0"


def test_sizing_invariants():
    s = sc.SIZING
    assert s["deploy_pct"] + s["reserve_pct"] == 1.0
    assert s["leverage"] in (1.0, 2.0)
    assert s["max_concurrent"] >= s["min_concurrent"]
    assert s["margin_mode"] == "isolated"


def test_risk_values_match_interface_example():
    r = sc.RISK
    assert r["max_account_drawdown_pct"] == 0.30
    assert r["daily_loss_limit_usd"] == 100.0
    assert r["position_size_min_usd"] == 5.0
    assert r["position_size_max_pct"] == 0.20


def test_runtime_values():
    rt = sc.RUNTIME
    assert "polling_interval_seconds" not in rt
    assert rt["entry_time_utc"] == "00:45"
    assert rt["reconciliation_time_utc"] == "23:00"


def test_universe_source_label():
    u = sc.UNIVERSE
    assert u["source"] == "binance_usdtm_perp_top_50"
    assert u["excluded"] == []


def test_divergence_alert_threshold():
    assert sc.DIVERGENCE_ALERT_THRESHOLD_PCT == 0.20
