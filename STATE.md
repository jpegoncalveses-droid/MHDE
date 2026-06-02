# STATE.md: Current System State

> Overwrite this file IN PLACE only when the operator says
> "refresh STATE". This is NOT append-only.
> It answers one question: what is true right now?
>
> Precedence on any conflict: the live host wins, then SESSION_LOG.md,
> then this file. A conflict between docs is a bug to fix.
>
> Machine fields come from scripts/snapshot_state.py. Judgement fields
> (blockers, next action, divergences) are filled by CC from the
> session, or by asking the operator.

Last updated: 2026-06-02 (snapshot ~17:47 UTC).

## Orientation
- Phase (per docs/PATH_TO_LIVE_PLAN.md): Phase 0 (calibration validation,
  parallel) — Active (pass/fail not re-verified this session). The execution
  layer (Phase 2) is built and live as the separate `crypto-trading-engine`
  repo, which consumes MHDE's daily `active_spec.json` + `predictions_*.json`
  and runs **paper trading** (ENGINE_MODE=paper, Binance testnet) — so **Phase 3
  (paper) is underway** on the engine side; Phase 4 (live) not started.
- Live branch: `master` @ `074c859` (the ADR-034 leverage-revert merge commit).
- Working tree: **dirty** (12 uncommitted), `master == origin/master` (ahead 0 /
  behind 0). The dirty files are **not in-flight code** — they are local
  data/analysis artifacts: `STATE.md` (this refresh), regenerated
  `data/processed/*` (prediction_vs_actual_*, missed_spike, priority_refresh,
  root_cause_enrichment), and this session's untracked regime-filter analysis
  outputs (`data/processed/backtest_regime_filter_{,_bullish_only,_causal,_dual_leverage,_walkfold}_20260602.md`).
  3 stashes parked (see below).

## In-flight work
- Open PRs: **none merged-pending on MHDE**. This session shipped **PR #16**
  (execution leverage revert 2x→1x, ADR-034 supersedes ADR-033) — **merged**,
  merge commit `074c859`. Last merged PRs: #16, #15 (intraday faithful replay of
  Phase 1B predictions), #14 (`entry_time_utc` 06:30→00:45), #13 (leverage
  1x→2x, since reverted), #12 (monitor engine-DB read-only retry), #11 (paper net
  PnL columns).
- The branch `chore/spec-leverage-revert-1x` (PR #16) is retained on origin.
- Stale local branches (retained, not in flight): `chore/spec-entry-time-align`,
  `feat-intraday-replay-eval`, `feat/paper-trading-net-columns`,
  `feat/paper-trading-tab-overhaul`, `fix/pipeline-monitor-opened-vs-failed`,
  `chore/ki-postparabolic-filter-drift`, `chore/state-md-snapshot-tooling`,
  `exp/trailstop-sweep`, `exp/parabolic-filter-ab`,
  `chore/spec-remove-phantom-polling-mhde`, `feat-btc-regime-classifier`,
  `feat-universe-correction`, `gap2-paper-trading-drift-monitor`,
  `gap3-paper-trading-dashboard-tab`.
- Stashes: 3 — `stash@{0}` data-artifacts fingerprint before paper-net-cols (on
  master), `stash@{1}` cumulative_delta redef pre-rebaseline (on
  fix/dashboard-pnl-cumulative-delta), `stash@{2}` parabolic-filter WIP (on
  exp/parabolic-filter-ab).
- Built but not deployed: none known.

## Active config
- Strategy spec: `data/exports/active_spec.json` v1.0.0, hash
  `sha256:f3c21810d98edfc6c53a14ef2c115c3c7597da8693b79d84b699f4de4e2a6cfe`,
  generated_at 2026-06-02T17:08:57Z by MHDE commit `074c859`. phase_1b_winner:
  exit_policy D, horizon 10d, top_n selection_n=6, run_id
  `backtest_10d_D_top_n_a02e15a0`, activation_pct 0.01, trail_pct 0.3.
  **Sizing: leverage 1.0** (reverted from 2.0 — PR #16 / ADR-034, supersedes
  ADR-033), deploy 0.8 / reserve 0.2, isolated, max_concurrent 6 / min 5. Risk:
  daily_loss_limit $100, max_account_dd 30%, position_size_max 20% / min $5.
  Runtime: entry_time_utc 00:45 (descriptive-only), reconciliation 23:00 UTC,
  24h monitoring window.
- Universe: `crypto_universe` 69 rows, 57 active. Last added_date 2026-05-31,
  last removed_date 2026-05-29. Source `binance_usdtm_perp_top_50`.
  **DOGSUSDT is NOT excluded** (`universe.excluded: []`) — kept per the engine's
  VENUE-001 disposition (the `-2019` was DOGSUSDT-specific transient venue noise,
  not margin; see Blockers).
- Active crypto model runs: `crypto_5d_306d3f1a`, `crypto_10d_46bd01ee`.
- Latest crypto predictions export: 2026-06-02 (mhde-crypto-export-predictions.timer
  last fired 2026-06-02 00:40 UTC).
- Paper-trading status: engine in paper mode, **0 open positions** at snapshot.
  The engine adopts the reverted 1x spec (`f3c21810…`) at the next 00:45 UTC
  entry (no restart needed — the entry oneshot re-reads the spec from disk each
  cycle). MHDE-side drift-monitor observability is scheduled
  (`mhde-monitor-paper-trading-drift.timer` active); P&L-band / drawdown /
  monthly arms still deferred (KI-136).

## Deltas from baseline
- DB schema: 67 live tables vs 30 declared (crypto/ml/fx `schema.py` cover only
  those three engines by design); the rest are equity-side + legacy +
  experimental orphans (backtest_*, crypto_backtest_*, crypto_regime_daily,
  events, features, fundamentals_*, missed_opportunity_*, model_runs,
  pipeline_runs, prices_daily, etc.). No declared table is absent.
- The hard-floor backtest run `backtest_10d_D_top_n_27e28ee7` (Policy D, 10d,
  top_n=6, hard_floor_pct=-0.05, post-parabolic ON) is **retained in the shared
  `crypto_backtest_{runs,trades}` tables by operator decision** (created during
  the regime-filter analysis; the spec's pinned winner is the separate
  `…_a02e15a0`).
- Timers: 25 mhde-* timers (23 system + 2 user), all enabled and firing on
  schedule; none flagged. Crypto daily chain: rank-universe 22:00 →
  build-universe 23:30 → predict 00:30 → export-predictions 00:40 → (engine entry
  00:45) → crypto-pipeline-monitor 00:50 UTC.

## Blockers and next action
- Open blockers: none hard. Live-path gates remain Phase 0 calibration passing
  and the Phase 1B sensitivity grid. Standing KIs: KI-132 (Streamlit not
  auto-restarted after dashboard merges), KI-136 (paper-trading drift arms
  deferred), KI-137 (post-parabolic-crash buy re-emission, mitigated), KI-141
  (crypto_ml_predictions lacks a run-time stamp).
- **BTC-7d-return regime export gate: INVESTIGATED AND SHELVED.** The backtested
  drawdown benefit was a one-day **look-ahead artifact** — the prior label used
  the entry day's own BTC close (close(D)), which the live 00:45 entry cannot
  know. Under the causal D-1 label, the filter is **worse than baseline**
  (portfolio max DD −39.98% vs −34.61%, lower Sharpe, breaches the −25% floor)
  and **fails the ADR-032 walk-fold gate (3/6)**. See
  `data/processed/backtest_regime_filter_causal_20260602.md`. **ADR-035 was
  intentionally not written** (operator chose to skip). Regime gating remains an
  open research direction, to be pursued **causal-first** (a fresh signal must
  clear the gate under causal labels before any implementation).
- **DOGSUSDT `-2019`: RESOLVED.** The prior cross-repo tension (engine VENUE-001
  "venue noise" vs ADR-033 "margin insufficiency") is closed in favor of
  VENUE-001: host evidence (two `-2019` events ever, both DOGSUSDT; mid-rank
  rejected while later same-cycle placements succeeded; ~5000 equity vs ~4000
  needed) refutes margin pressure. Leverage reverted to 1x (ADR-034). DOGSUSDT
  kept in universe; revisit before live.
- Next agreed action / focus: **regime-gating research** (fresh signal, causal
  labels, walk-forward OOS discipline). Secondary: confirm the next 00:45 UTC
  engine entry adopts the 1x spec (`f3c21810…`) cleanly.
- Deferred queue (max 3): (1) KI-136 — paper-trading drift P&L-band / drawdown /
  monthly arms; (2) regime-gating research (causal-first); (3) KI-141 — run-time
  stamp on crypto_ml_predictions.

## Repo / host divergences
- None confirmed on the MHDE side this session. Machine fields above are read
  directly from the live host (DuckDB read-only + systemctl). Repo-vs-host drift
  has its own monitor (`mhde-monitor-config-drift.timer`).
- Cross-repo (engine): engine HEAD `a087ed5` (no engine code change this
  session); 0 open positions. The engine's latest-position spec_hash (`40ae8ac…`,
  the old 2x stamp) lags MHDE's live 1x spec (`f3c21810…`) — EXPECTED; the engine
  adopts the 1x spec at the next 00:45 UTC entry. Engine `ENGINE_STATE.md` was
  refreshed this session on branch `chore/engine-state-refresh-20260602` (pushed,
  awaiting operator merge).
- This STATE.md refresh is delivered on branch `chore/state-refresh-20260602`
  (pushed, awaiting operator merge — not auto-merged).
