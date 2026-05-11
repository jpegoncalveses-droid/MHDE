# Paper-Trading Drift Monitor (Gap 2) — Design

**Date:** 2026-05-11
**Branch:** `gap2-paper-trading-drift-monitor`
**Status:** Approved by operator (2026-05-11), ready for TDD implementation.
**Supersedes:** the Gap 2 section of `~/.claude/plans/operator-needs-three-interconnected-zazzy-brooks.md` (scope reworked — see below).

## Why

The crypto-trading-engine runs paper trading on the Binance demo against MHDE's daily
predictions. If the realised **trade win rate** (P&L > 0 after costs) or the **label hit
rate** (top-6 picks reaching +10% within 10 days) drifts outside the Phase 1B walkfold band,
nothing alerts — the operator only sees it if they go looking. There is also no automated
check that the engine is actually alive (entry phase ran today, monitor phase ticking) or
that no position is wedged in a `*_pending` state.

## Scope (reworked from the original plan)

Liveness + hit-rate only. **No P&L-band / drawdown / monthly-return checks** — the engine's
`daily_pnl` table is empty because the reconcile timer is disabled pending RECONCILE-001 on
the engine side, so those arms would be inert. They are deferred to a follow-up ("Gap 2.5",
blocked on RECONCILE-001 populating `daily_pnl`), tracked as a new `KNOWN_ISSUES.md` entry.

## Cross-repo data access

The monitor reads the engine's DuckDB **read-only**. Path from env var
`CRYPTO_ENGINE_DB_PATH` (default `/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`)
— not hardcoded. This is a deliberate, scoped exception to `INTERFACE.md`'s "no database
access between systems" rule: it is read-only, monitoring-only, the engine remains the source
of truth, and the monitor never writes to the engine's DB. Documented as ADR-020 in
`DECISIONS.md` with a cross-reference note added to `INTERFACE.md`.

## Module: `monitoring/paper_trading_drift.py`

```python
def run(engine_conn=None, mhde_conn=None, now=None) -> MonitorResult: ...
def main() -> int:  # calls send_alert(run()); returns 0 if status == "ok" else 1
```

Mirrors the existing monitor pattern (`monitoring/phase0_calibration.py`). Connections and
`now` are injectable for testability. The run aggregates four checks into one `MonitorResult`:
overall `status`/`severity` = the worst finding (critical > warn > ok); `body` is a
markdown-ish list of **every** finding including OK lines and "insufficient sample" notes, so
the single Telegram message is self-contained; `metrics` carries the measured values.

### Check A — engine liveness (always active, never sample-gated)

- **A1 — monitor-phase freshness.** Newest `engine_runs` row with `phase='monitor'` and
  `success=true`. Age > `ENGINE_MONITOR_STALE_WARN_MIN` (5) → WARN; age >
  `ENGINE_MONITOR_STALE_CRIT_MIN` (20) → CRITICAL. (Engine's monitor phase runs every minute
  via its own cron.)
- **A2 — entry-phase ran today.** Only checked once the current UTC time is at or past
  `ENTRY_GRACE_CUTOFF_UTC` (`08:30` — the first 15-min cycle by which the engine's daily
  entry phase, scheduled ~08:00 UTC, should have run). If no `engine_runs` row with
  `phase='entry'`, `success=true`, and `started_at >= today 00:00 UTC` → WARN. Before the
  cutoff, this arm is skipped (reports "entry: not due yet").
- **Reconcile arm intentionally omitted.** The engine's reconcile timer is currently disabled
  pending RECONCILE-001, so a "reconcile ran today" check would false-alarm. A
  `CHECK_RECONCILE = False` module flag is left in place to re-enable it later.

### Check B — stuck-position staleness (always active, never sample-gated)

Positions whose `current_state` is `entry_pending` or `exit_pending` with `now - updated_at`
> `PENDING_STALE_WARN_MIN` (10) → WARN; > `PENDING_STALE_CRIT_MIN` (30) → CRITICAL. The body
lists each offender (symbol, state, age in minutes). (Threshold relaxed from the originally
specified 5 min to 10 min to match the 15-min monitor cadence — a stuck position alerts on
the first or second cycle after it gets stuck.)

### Check C — closed-trade win rate, rolling 14 days (sample-gated)

**Qualifying closed trades:** `positions.current_state = 'exit_filled'` AND
`positions.entry_price IS NOT NULL` AND a matching `orders` row with `side='SELL'`,
`order_type='MARKET'`, `status='FILLED'` exists AND the exit timestamp (the SELL order's
`filled_at`, falling back to `positions.updated_at`) is within the last
`ROLLING_WINDOW_DAYS` (14). This filter excludes the known phantom `reconcile_auto_closed`
position (it has `entry_price = NULL` and no SELL order — a RECONCILE-001 artifact).

**Per trade:** `net_pnl_usd = (sell_fill_price - entry_price) * qty - ROUND_TRIP_FEE_PCT *
(entry_price * qty)`. `ROUND_TRIP_FEE_PCT = 0.0009` (≈ 0.045% taker × 2 legs) — the engine
does not record per-trade fees, so this is a flat haircut so "win" means net-of-cost. A trade
is a **winner** iff `net_pnl_usd > 0`.

**Verdict:** `win_rate = winners / N`. Compared to `TRADE_WIN_RATE_BAND`:
- Inside `[0.74, 0.99]` → OK.
- Outside `[0.74, 0.99]` → WARN.
- Below `0.60` → CRITICAL (above 1.0 is impossible).
Centre reference ≈ 0.869 (walkfold median; do **not** read this from
`active_spec.json.expected_hit_rate` — that field *is* this metric, but the comment must make
the distinction explicit so a future reader doesn't confuse it with the label hit rate).

**Sample gate:** if `N < MIN_CLOSED_FOR_HITRATE` (20) → no warn/critical; status stays OK for
this arm; body line: `closed-trade win rate: insufficient sample (N/20)`.

### Check D — label hit rate, rolling 14 days (sample-gated)

For closed positions, join `(positions.symbol, positions.entry_date)` to MHDE
`crypto_ml_labels (symbol, trade_date)` and read `label_10d_10pct` (the canonical "this coin's
max forward close reached ≥ +10% within 10 days" flag). **Window:** a label only settles
`LABEL_SETTLE_DAYS` (10) days after entry, so the "rolling 14-day window" is interpreted as
**"label settled within the last 14 days"** — i.e. positions with
`entry_date + 10d` in `[today - 14d, today]` (positions entered roughly 14–24 days ago).
(Tying the window to entry-date instead would leave only positions entered 10–14 days ago, a
4-day-wide set that would essentially never reach N=20.) Positions whose label has not yet
settled are excluded; the body reports the skipped count.

**Verdict:** `label_hit_rate = Σ label_10d_10pct / N`. Compared to `LABEL_HIT_RATE_BAND`:
- Inside `[0.32, 0.62]` → OK.
- Outside `[0.32, 0.62]` → WARN.
- Outside `[0.20, 0.75]` → CRITICAL.
Centre reference ≈ 0.425 (walkfold median, top-6).

**Sample gate:** same as Check C — `N < MIN_CLOSED_FOR_HITRATE` (20) → OK + "insufficient
sample" body line.

### Config constants (module-level, with a comment citing `docs/strategy_analysis_2026-05-10.md` for the bands)

| Constant | Default |
|---|---|
| `CRYPTO_ENGINE_DB_PATH` (env var) | `/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb` |
| `ROLLING_WINDOW_DAYS` | `14` |
| `MIN_CLOSED_FOR_HITRATE` | `20` |
| `TRADE_WIN_RATE_BAND` | warn outside `(0.74, 0.99)`, critical below `0.60` |
| `LABEL_HIT_RATE_BAND` | warn outside `(0.32, 0.62)`, critical outside `(0.20, 0.75)` |
| `ENGINE_MONITOR_STALE_WARN_MIN` / `_CRIT_MIN` | `5` / `20` |
| `PENDING_STALE_WARN_MIN` / `_CRIT_MIN` | `10` / `30` |
| `ENTRY_GRACE_CUTOFF_UTC` | `08:30` |
| `ROUND_TRIP_FEE_PCT` | `0.0009` |
| `LABEL_SETTLE_DAYS` | `10` |

`MONITORING_DRY_RUN` is honoured transparently via `monitoring.alert.send_alert`.

## CLI wiring — `main.py`

Add under the existing `monitor` group:

```python
@monitor.command("paper-trading-drift")
def monitor_paper_trading_drift():
    from monitoring import paper_trading_drift
    raise SystemExit(paper_trading_drift.main())
```

## systemd (committed; operator deploys)

- `systemd/mhde-monitor-paper-trading-drift.service` — `ExecStart=/home/jpcg/MHDE/venv/bin/python main.py monitor paper-trading-drift`, `User=jpcg`, `WorkingDirectory=/home/jpcg/MHDE`, `Environment=CRYPTO_ENGINE_DB_PATH=/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`, `StandardOutput=append:/home/jpcg/MHDE/data/logs/monitor_paper_trading_drift.log`, `StandardError=` likewise. Mirrors `mhde-monitor-data-quality.service`.
- `systemd/mhde-monitor-paper-trading-drift.timer` — `OnCalendar=*:0/15` (every 15 minutes), `Persistent=true`.
- `OPERATIONS.md` monitors table gains a row; `INFRASTRUCTURE.md` documents the unit + install steps.

## Tests (TDD — written first) — `tests/monitoring/test_paper_trading_drift.py`

Build a synthetic engine DuckDB (`engine_runs`, `positions`, `orders`) in `tmp_path` and a
synthetic MHDE DuckDB (`crypto_ml_labels`) via the existing `temp_db` helper / a local
builder; drive `run(engine_conn=..., mhde_conn=..., now=...)` and assert `status`, `severity`,
and `body` substrings for:

1. All healthy → `ok` / `info`; body includes the OK lines for all four checks.
2. Engine monitor-phase 6 min stale → `warn`; 25 min stale → `fail`/`critical`.
3. No `entry` run today, `now` past 08:30 → `warn`; same but `now` at 08:15 → no entry alert.
4. Position stuck in `entry_pending` 12 min → `warn`; 35 min → `fail`/`critical`.
5. Closed-trade win rate computed from synthetic SELL fills: 0.60 → `warn`; 0.55 →
   `fail`/`critical`; 0.90 → OK for that arm.
6. `N = 12` qualifying closed trades → "insufficient sample (12/20)" + no warn/critical from
   that arm.
7. Phantom `exit_filled` position with `entry_price = NULL` / no SELL order → excluded from
   the closed-trade denominator (N counts only the real ones).
8. Label hit rate: synthetic `crypto_ml_labels` → 0.80 → `warn`; 0.10 → `fail`/`critical`;
   0.45 → OK. Positions younger than 10 days excluded; skipped count reported.
9. `CRYPTO_ENGINE_DB_PATH` env-var override is honoured by the no-arg `run()` path
   (monkeypatch the env var to a temp DB).
10. Severity aggregation: a `warn` arm + a `critical` arm → overall `fail`/`critical`.

Run with `.venv/bin/python -m pytest tests/monitoring/test_paper_trading_drift.py -v`.

## Deliverable order

tests → `monitoring/paper_trading_drift.py` → `main.py` wiring → systemd units →
docs (`OPERATIONS.md`, `ARCHITECTURE.md` mention, `DECISIONS.md` ADR-020, `INTERFACE.md`
note, `KNOWN_ISSUES.md` Gap-2.5 entry, `INFRASTRUCTURE.md`, `SESSION_LOG.md`) →
verification: full module pytest + `MONITORING_DRY_RUN=true venv/bin/python main.py monitor
paper-trading-drift 2>&1` against the real engine DB → **STOP for operator review** → on
approval, push `gap2-paper-trading-drift-monitor` and open the PR via `gh`; operator merges
via the GitHub UI.

## Out of scope / deferred

- P&L-band check (realised rolling-30d P&L vs `active_spec.json.backtest_expectations`,
  ±20% band) — Gap 2.5, blocked on RECONCILE-001 populating engine `daily_pnl`.
- Drawdown-breach check vs `portfolio_max_dd_pct` — same.
- Monthly-portfolio-return arm — same.
- Dashboard surfacing of paper-trading state — Gap 3.
