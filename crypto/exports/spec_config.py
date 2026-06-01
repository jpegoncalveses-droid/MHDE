"""Static spec fields for active_spec.json.

Phase 1B-derived fields (run_id, trail_pct, n, horizon, expectations)
are read from DB at spec-generation time; everything else lives here.
Phase 1B re-runs require an explicit edit of PHASE1B_WINNER_RUN_ID
plus a commit.

Risk envelope values are adopted from INTERFACE.md §2 example for
$1k Phase 2 paper trading. Revisit at the Phase 3 → 4 transition.
See DECISIONS.md.
"""
from __future__ import annotations

SPEC_VERSION = "1.0.0"

PHASE1B_WINNER_RUN_ID = "backtest_10d_D_top_n_a02e15a0"

SIZING = {
    "deploy_pct": 0.80,
    "reserve_pct": 0.20,
    "max_concurrent": 6,
    "min_concurrent": 5,
    "leverage": 2.0,
    "margin_mode": "isolated",
}

RISK = {
    "max_account_drawdown_pct": 0.30,
    "daily_loss_limit_usd": 100.0,
    "position_size_min_usd": 5.0,
    "position_size_max_pct": 0.20,
}

UNIVERSE = {
    "source": "binance_usdtm_perp_top_50",
    "excluded": [],
}

RUNTIME = {
    "monitoring_window_hours": 24,
    "reconciliation_time_utc": "23:00",
    "entry_time_utc": "06:30",
}

DIVERGENCE_ALERT_THRESHOLD_PCT = 0.20
