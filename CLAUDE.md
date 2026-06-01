## Read first

Before doing any work in this repo — debugging, refactoring, adding features, or operational tasks — read these files:

1. `STATE.md` - current system state: live branch, in-flight work,
   active config, blockers, next action. Read first. Treat as stale
   until verified against the host. On any conflict: host wins, then
   SESSION_LOG.md, then STATE.md.
2. `ARCHITECTURE.md` — top-down system architecture; the three engines + daily-analysis path + dashboard.
3. `INFRASTRUCTURE.md` — VPS, services, reverse proxy, schedules, secrets locations.
4. `OPERATIONS.md` — runbook: manual pipeline invocations, recovery procedures, deploy steps.
5. `DATABASE_SCHEMA.md` — every table, columns, types, reader/writer modules.
6. `KNOWN_ISSUES.md` — open and resolved bug tracker.
7. `DECISIONS.md` — architecture decision records.
8. `HARDENING_PLAN.md` — multi-session roadmap; check the current session number before starting work.
9. `SESSION_LOG.md` — append-only log of what was done each session (read the most recent 3 entries at session start).
10. `docs/PATH_TO_LIVE_PLAN.md` — canonical 5-phase plan from current state to $1000 live trading on Binance Futures. Phase 0 (calibration validation) is parallel; Phases 1A/1B (backfill + execution backtest) drive Phase 2 (execution-layer build) which gates Phase 3 (paper trading) which gates Phase 4 (live). Read before any crypto-execution / Phase 1+ work.
11. `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md` — file-based contract MHDE must respect when producing `data/exports/active_spec.json` and the daily `data/exports/predictions_YYYY-MM-DD.json` files. Both files are produced by `crypto/exports/` (see `crypto export-spec` and `crypto export-predictions` CLI commands plus the `mhde-crypto-export-predictions.timer` systemd unit). Schema or hash-canonicalization changes require a coordinated commit on both repos.

These describe the deployment topology and the code layout. They override assumptions from training data.

`legacy/` is **dormant code preserved for rollback safety**. Nothing under `legacy/` is imported by ACTIVE code. Don't refer to it for how production behaves; see `legacy/README.md` for what's there.

`docs/` (lowercase) contains pre-rebuild prose documentation. Some sections are still useful (data sources, scoring rationale) but the architectural sections were superseded by `ARCHITECTURE.md` in Session 1.

## Python command policy

Use `venv/bin/python` directly instead of activating the virtual environment with `source venv/bin/activate`.

Preferred commands:
- `venv/bin/python main.py health 2>&1 | tail -20`
- `venv/bin/python main.py backtest smoke 2>&1 | tail -10`

## Dashboard query smoke test command policy

Do not use inline `python -c` blocks for dashboard query validation.

Use this command instead:

`MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python .claude/local_scripts/test_dashboard_queries.py 2>&1`

## Python pytest command policy

Do not use `source .venv/bin/activate && python ...`.

Use the virtual environment Python directly:

`.venv/bin/python -m pytest research/gbpeur_personal_fx/predictive/tests/test_forecast.py -v`

## DuckDB smoke test command policy

Do not use inline `python -c` blocks for DuckDB smoke tests.

Use this command instead:

`venv/bin/python .claude/local_scripts/test_duckdb_failed_alter.py`

## Refreshing STATE.md

Trigger: only when the operator explicitly says "refresh STATE".
Never refresh it automatically, including at session end.

Procedure:
1. Run venv/bin/python scripts/snapshot_state.py
2. Overwrite STATE.md in place. Machine fields from the script output;
   judgement fields (blockers, next action, deferred queue, repo/host
   divergences, paper-trading status) from the current session, or ask
   the operator.
3. Always verify against the host. Never rewrite STATE.md from memory
   or from possibly-stale docs.

STATE.md is only refreshed on explicit operator command, so any session
must treat it as potentially stale and verify against the host before
trusting it.

## Cross-chat protocol

This repo is sometimes worked on by parallel Claude Code chat sessions.
A workstream started in one chat (e.g. an in-flight branch, a deferred
test, an ADR mid-cutover) is invisible to a chat that starts cold —
unless `SESSION_LOG.md` and the branch state make it visible.

**Before starting substantial work** (anything beyond a one-line edit
or a read-only investigation): search `SESSION_LOG.md` for related
ongoing workstreams (recent entries, "Pending" sections, branches
named in the entry). If you find one, check the branch state with
`git branch -a` and `git log` for that branch before touching
overlapping code paths. If you're uncertain whether your work overlaps
an in-flight workstream, **stop and ask the user** before proceeding.
The cost of a clarifying question is much lower than the cost of two
chats colliding on the same files.

**When working in parallel chat sessions**, the chat that starts
substantial work is responsible for updating `SESSION_LOG.md` before
ending — even if the work is unfinished. A "Pending" section in the
session entry is enough to make the work visible to the next chat.
Don't rely on memory or the assumption that the user will mention it.
