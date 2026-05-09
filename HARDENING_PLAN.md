MHDE Hardening Plan — From AI-Built to Production-Grade

Goal

A system where JP can look at the dashboard, make decisions, and trust that everything underneath is working. Bugs caught automatically, not by inspection. Context preserved across sessions. Three engines (equity, crypto, FX) all production-grade.

The Problem We're Solving

The current system was built fast by AI across many sessions. It works most of the time but bugs accumulate in integration seams. Each session, Claude Code rebuilds mental model from scratch and risks breaking things it doesn't have context on. Production is the test environment. Discovery of bugs is manual via dashboard inspection. Legacy code from the original engine still lives in the repo, adding noise.

This plan replaces that pattern with engineering discipline: cleanup, documentation, automated tests, CI, monitoring, and context preservation.

Lesson from KI-118 (added 2026-05-09)

A `git status` audit on master surfaced 10 production source files
(`fx/bot/*`, `fx/data/refresh.py`, `pipelines/{freshness,health_check}.py`,
five `systemd/mhde-*` units) that had been live on the deployment host
for months without ever being `git add`-ed. The pre-rebuild
"checkpoint" commit had reorganized the layout but the moved files were
never staged, and Sessions 0-7 ran on top of that gap because none of
the exit criteria checked for it. The fix landed in `fc6fc28`; the
process gap was the harder lesson — eight hardening sessions of test
and monitor scaffolding cannot detect a load-bearing module that
isn't visible to `git`. From the discipline session
(2026-05-09) forward, every session's exit criteria include a clean
working tree and a passing `tests/regression/test_no_untracked_production_imports.py`
gate. Process gaps are tracked in `KNOWN_ISSUES.md` the same way code
defects are.

Multi-Session Plan Overview

8 sessions, executed sequentially. Each session has a defined scope, deliverables, and exit criteria. Don't move to the next session until exit criteria are met.


Session
Theme
Outcome

0
Legacy code cleanup
Only ACTIVE code remains in primary directories. Legacy isolated.

1
Documentation as source of truth
Anyone (or Claude Code) can understand the full system from docs alone

2
Test infrastructure
pytest framework, fixtures, in-memory DB, CI runner

3
Unit tests
Every function in features, labels, predict, evaluate has coverage

4
Integration tests
Each pipeline runs end-to-end with synthetic data, validated

5
Regression tests
Every bug ever found becomes a test that fails-then-passes

6
Monitoring & verification
Automated checks that catch drift, inconsistency, degradation

7
Hardening & validation
Full audit using the suite, fix anything failing, lock down



Context Preservation Strategy

To prevent Claude Code from losing context across sessions, the repo root has a documentation layer that every Claude Code session reads first.

Required files at repo root


File
Purpose
Updated when

CLAUDE.md
Entry point, points Claude Code to other docs
Rarely

ARCHITECTURE.md
Full system architecture, all 3 engines
When architecture changes

INFRASTRUCTURE.md
VPS setup, deployment, services, paths (exists)
When infra changes

DATABASE_SCHEMA.md
Every table, columns, relationships, constraints
Every schema change

TESTING.md
Test strategy, how to run, coverage requirements
When test approach changes

OPERATIONS.md
Runbook: how to deploy, debug, recover, monitor
When ops procedures change

KNOWN_ISSUES.md
Active bug tracker with status
Continuously

DECISIONS.md
ADRs (Architecture Decision Records) for major choices
Per major decision

SESSION_LOG.md
Append-only log of what was done in each session
End of every session



Session start protocol

Every Claude Code session begins with:

Read in order: CLAUDE.md, ARCHITECTURE.md, KNOWN_ISSUES.md, the most recent 
3 entries of SESSION_LOG.md. Then ask the user what we're working on this 
session before making any changes.


Session end protocol

Every Claude Code session ends with:

Append a new entry to SESSION_LOG.md with:
- Date and session focus
- What was completed
- What was changed (files modified, tests added, bugs fixed)
- Any new known issues discovered
- What's pending for the next session
- Update KNOWN_ISSUES.md if any bugs were resolved or discovered


Universal exit criteria (every session)

In addition to each session's specific exit criteria below, no session
is considered complete until all of:

- `git status` on the session branch is clean. No untracked or unstaged
  load-bearing source under any production directory (the regression
  test below enforces this for `.py` and `systemd/` units; operator-
  decided scratch under `data/processed/` or `docs/` plans may remain in
  the working tree only when it's explicit which class they fall under).
- `tests/regression/test_no_untracked_production_imports.py` passes
  (added 2026-05-09 as the KI-118 gate). It walks every tracked `.py`
  outside `tests/`, `legacy/`, `.claude/local_scripts/`, `venv/`,
  `.venv/` and asserts that every import resolving to a path in the
  repo is in `git ls-files`. Plus: every `.service`/`.timer` under
  `systemd/` is tracked.
- The full `tests/regression/` suite passes.
- `SESSION_LOG.md` has a new entry covering what was done.
- `KNOWN_ISSUES.md` updated if any bugs were resolved or discovered.

These apply on top of the per-session exit criteria below. They were
introduced after KI-118 (see lesson note at the top of this file) so
that the next process gap surfaces inside the discipline rather than
in production.


───

Session 0: Legacy Code Cleanup — EXECUTED 2026-05-07

> **Status:** completed. Branch `session-0-legacy-cleanup` merged as
> `1f9a11e`. The full record is in `SESSION_LOG.md`; the design decisions
> are in `DECISIONS.md` (ADR-001 through ADR-005); a per-file inventory
> with rationale is in `legacy/README.md`. The text below is the original
> plan as written, with corrections applied so Sessions 1-7 reference
> accurate ground truth.

Scope: Remove or isolate every code path that is no longer reachable from a running pipeline, service, or dashboard tab. The three current ML engines (equity / crypto / FX) plus the daily-analysis path are what stays.

What's actually legacy in the codebase (as confirmed by reachability analysis from systemd ExecStart lines, the daily-analysis shell wrapper, and dashboard imports):

• scoring/incomplete_diagnostics.py — dev tool, not invoked by any timer.
  *(scoring/scorecard.py, scoring/ranker.py, scoring/explanations.py, scoring/tiers.py STAY: still imported by pipelines/daily_radar.py, which runs Mon-Fri 23:15 via mhde-daily-analysis.service.)*
• learning/ — feedback loop, never wired in. Whole directory.
• missed/{attribution,catalyst_report,detector,episode_tracker,investigator,labels,llm_policy,report,sector_attribution}.py — 9 dormant pieces.
  *(missed/{catalyst_queue,catalyst_digest,prediction_report,root_cause_enrichment} and several helpers STAY: invoked daily by run_mhde_daily_analysis.sh with --no-mock --provider openai. The "mock mode only" claim was wrong.)*
• models/ — shadow_ranker, dataset_builder, evaluation, promotion_gates, registry, shadow_dataset, xgboost_ranker. Whole directory minus models/saved/.
  *(models/saved/ STAYS at the active path. Every engine reads it via hardcoded "models/saved" strings in ml/train.py, crypto/config.py, fx/config.py, health/ml_checks.py.)*
• backtest/ — stub framework, never used. Whole directory.
• dashboard/pages/_legacy/ — 19 Streamlit pages from the pre-rebuild dashboard.
• review/server.py + review/{importer,packet_builder}.py — Flask catalyst review UI. Service (mhde-review-server) and nginx /review/ route both retired.
• ml/retrain.py — superseded by ml/train.py:train_walk_forward.
• pipelines/weekly_review.py and reports/weekly_review.py — companion to the dead `weekly_review` CLI.
• outcomes/{candidate_lifecycle,labels}.py — only ever called by the legacy review server and a dead `outcomes/__init__.py` re-export (now removed).
• storage/inventory.py — backs the `data inventory` CLI (no timer).
• universe/ticker_details_enricher.py — backs `data enrich-ticker-details` (no timer).
• governance/ — 7 files of dormant registry / feature-flag / governance scaffolding. Whole directory.
• hypotheses/registry.py.
• crypto/ml/hypothesis_tests.py and fx/ml/hypothesis_tests.py — dev research harnesses.
• `daily_radar` orchestration is NOT legacy — it is the active equity ingest path (mhde-daily-analysis.service Mon-Fri 23:15). The earlier draft of this list was wrong on that.
• `/home/jpcg/ATSRP/research/gbpeur_personal_fx/` is NOT a movable artifact — fx/data/refresh.py shells into ATSRP for Dukascopy bi5 hourly bars and notifications/telegram.py reads /home/jpcg/ATSRP/.env for TELEGRAM_BOT_TOKEN. ATSRP is an active dependency. (Plan's "Option B" was correct: leave it alone.)

Deliverables (as executed):

1. Inventory script: Walk every file in the repo. Classify each as:
◦ ACTIVE: imported by current pipelines, services, or dashboard tabs
◦ LEGACY: dormant, no active references
◦ SHARED: utilities used by both (e.g., storage, common config)
*(Implemented as `.claude/local_scripts/inventory_active_legacy.py` — AST-based reachability BFS from systemd ExecStart entry points.)*

1. Confirm legacy is truly unused:
◦ For each LEGACY file, grep the entire codebase for imports
◦ Check systemd unit ExecStart commands
◦ Check dashboard imports
◦ Check CLI commands in main.py
◦ Verify zero references from ACTIVE code
*(All confirmed except: outcomes/__init__.py re-exported `compute_forward_returns` from outcomes/labels.py — re-export removed in commit a3ba5d1.)*

1. Isolate legacy: Move LEGACY files to a legacy/ directory at repo root. Don't delete. Preserve git history.

1. Update imports: If any ACTIVE code accidentally references something now in legacy/, refactor.

1. Verify nothing broke (executed as **safe checks only**, per JP's choice — no live pipeline runs, no test telegram messages, to avoid colliding with scheduled timers and the production DuckDB):
◦ python -m py_compile over every active .py — clean.
◦ Import-resolution smoke on 50 entry-point modules — 50/50 OK.
◦ systemd-analyze verify on every unit in systemd/ — 13/13 OK.
◦ Dashboard query smoke (test_dashboard_queries.py with auth disabled) — 10/10 queries pass.
◦ pytest --collect-only — 743 tests collected, no errors. Offline subset (540 tests) — all pass. The 6 pre-existing test_health.py failures predate this session.
◦ Live pipelines and Telegram bot will be observed at the next natural timer firings rather than triggered ad-hoc.

1. Document the cleanup:
◦ Update INFRASTRUCTURE.md to remove references to legacy
◦ Update CLAUDE.md to note that legacy/ is for reference only
◦ Add a legacy/README.md explaining what's there and why
◦ Note in DECISIONS.md why legacy was preserved (rollback safety) vs deleted

1. External legacy: Decide on /home/jpcg/ATSRP/research/gbpeur_personal_fx/:
◦ Option A: Move to a legacy_external/ directory in MHDE for cleanup
◦ Option B: Leave it where it is — it's an active dependency of the FX engine, not a candidate for relocation. Selected.

Exit criteria (as met):
• ACTIVE code is now distributed across: ml/, crypto/, fx/, dashboard/, pipelines/, storage/, ingestion/, health/ (the plan's "system/" was a typo), adapters/, config/, deploy/, llm/, missed/ (active subset), notifications/, outcomes/ (active subset), reports/ (active subset), runner/, scoring/ (active subset), features/, hypotheses/ (active subset), universe/ (active subset), main.py.
• Nothing in legacy/ is imported by ACTIVE code (verified with grep + 50/50 import smoke).
• All 3 prediction pipelines verified at the static level (import + py_compile + systemd unit syntax). Live runs deferred to natural timer firings.
• Dashboard query layer verified (10/10 queries pass against the production DB read-only). Full UI render unchecked.
• Every systemd unit in `systemd/` validates clean under systemd-analyze. Active timers: mhde-{predict,retrain}, mhde-crypto-{predict,retrain}, mhde-fx-{predict,retrain}, mhde-fx-bot (always-on), plus user-level mhde-daily-analysis, mhde-health-check, mhde-streamlit, mhde-streamlit-relay. (The original "all 6 systemd timers" line under-counted.)
• ~99 .py files of ~347 total moved to legacy/ (≈28%). Slightly under the 30-50% estimate in the original plan because several directories the plan assumed were legacy (features/, scoring/scorecard, the active subset of missed/, daily_radar, etc.) turned out to still be wired in.

Cleanup deletion (deferred):
After 2 weeks of stable operation post-Session 7, the legacy/ directory can be deleted entirely. This gives a safety window in case anything was missed.

Outputs of this session (commit `a3ba5d1`, merged as `1f9a11e`):
• `legacy/` directory with 99 .py + 29 tests + README.md
• `DECISIONS.md` — 5 ADRs (legacy preservation, review-server retirement, ATSRP-stays-external, dead-re-export removal, plan-corrected-by-codebase)
• `SESSION_LOG.md` — append-only session record
• `INFRASTRUCTURE.md` — first-time tracked, with review-server section retired
• `CLAUDE.md` — read-first list expanded to include the new docs and a `legacy/` warning
• `outcomes/__init__.py` — dead `compute_forward_returns` re-export removed
• `/home/jpcg/homeboard/nginx/nginx.conf` — `/review/` route + upstream removed (out-of-repo coordinated change)

Time estimate: 1 session. *(Actual: ~1 session including discovery of plan inaccuracies and recovery of `models/saved/` after it was nearly swept into legacy.)*

───

Session 1: Documentation as Source of Truth

Scope: Establish the documentation foundation. No code changes. Everything that exists in the system gets documented accurately.

Deliverables:

1. ARCHITECTURE.md consolidating the 3 engine architecture docs into one master document
2. DATABASE_SCHEMA.md with every table, column types, relationships, indexes, constraints
3. OPERATIONS.md consolidating systemd timers, services, deployment procedures, recovery procedures
4. KNOWN_ISSUES.md listing every bug we've found and its status
5. DECISIONS.md capturing the 8-10 major architectural decisions made (XGBoost over logistic, walk-forward CV, mean-reversion baseline, Session 0 legacy preservation, etc.)
6. SESSION_LOG.md initialized with everything done to date including Session 0 cleanup
7. CLAUDE.md updated to point to all of the above with the read-first protocol

Exit criteria:
• Every database table is documented with its purpose, columns, and which code reads/writes it
• Every systemd unit is documented with schedule, what it runs, dependencies
• Every major design decision has an ADR explaining why
• Reading the docs alone, a new engineer (or Claude Code) understands the full system
• Documentation reflects the post-cleanup state, not the legacy state

Time estimate: 1 session.

───

Session 2: Test Infrastructure

Scope: Build the testing framework. No tests yet, just the scaffolding.

Deliverables:

1. pytest installed and configured with pytest.ini
2. tests/ directory structure mirroring the source (tests/equity/, tests/crypto/, tests/fx/, tests/dashboard/, tests/integration/, tests/regression/)
3. tests/conftest.py with fixtures: 
◦ temp_db: in-memory DuckDB with all schemas applied
◦ synthetic_prices_equity: realistic OHLCV data generator
◦ synthetic_prices_crypto: same for crypto
◦ synthetic_prices_fx: same for FX hourly
◦ synthetic_filings: filing data generator
◦ synthetic_fundamentals: fundamentals generator
◦ mock_telegram: captures sent messages without hitting API
4. tests/helpers.py with assertion helpers: 
◦ assert_db_state(conn, table, expected_rows)
◦ assert_pipeline_completed_cleanly(conn, engine)
◦ assert_dashboard_renders(page, expected_data)
5. Makefile with targets: test, test-unit, test-integration, test-regression, coverage
6. CI configuration (GitHub Actions or simple pre-commit hook running tests)
7. Coverage reporting set up (pytest-cov, target 80%+ for non-dashboard code)

Exit criteria:
• make test runs and reports "0 tests" without errors
• Synthetic data fixtures produce realistic data that passes manual inspection
• In-memory DB fixture creates all production tables successfully

Time estimate: 1 session.

───

Session 3: Unit Tests

Scope: Cover every pure function with unit tests. No integration, no databases (except in-memory for table-touching code).

Deliverables:

For each engine (equity, crypto, FX), write unit tests covering:

1. Feature computations (features.py): 
◦ Each feature function called with synthetic data
◦ Edge cases: NULL inputs, single-row inputs, all-zero inputs
◦ Lookahead bias check: feature for date T must not change when future data is appended
◦ Numerical stability: extreme values don't produce NaN/Inf

1. Label generation (labels.py): 
◦ Forward returns computed correctly
◦ Max returns/drawdowns in window
◦ Binary labels at each threshold
◦ Edge cases: insufficient forward data, NULL prices in window

1. Outcome filling (predict.py:fill_outcomes): 
◦ Window matches label window (regression test for the bug we found)
◦ Past predictions filled correctly
◦ Recent predictions left as NULL
◦ Equity uses trading rows, crypto/FX use calendar windows

1. Recommendation logic (FX signals.py): 
◦ Each (P_up, P_down, position) combination produces correct signal
◦ Suppression logic for non-actionable signals

1. Health check logic (system/health.py): 
◦ Each engine freshness check
◦ Service state aggregation
◦ Telegram message formatting

Exit criteria:
• Every public function in features, labels, predict, evaluate, signals has at least one test
• Coverage report shows 80%+ for these modules
• All tests pass
• Tests run in under 30 seconds total

Time estimate: 2 sessions if thorough.

───

Session 4: Integration Tests

Scope: Each pipeline runs end-to-end with synthetic data. Verify outputs match expectations.

Deliverables:

1. Equity pipeline integration test:
◦ Setup: synthetic prices for 50 tickers across 60 days
◦ Run: ml backfill-features then ml predict
◦ Verify: features table populated, predictions table populated, outcomes table populated for old predictions
◦ Verify: precision metrics within expected range
◦ Verify: dashboard query returns expected rows

1. Crypto pipeline integration test:
◦ Same pattern with synthetic crypto data including funding rates and OI

1. FX pipeline integration test:
◦ Same pattern with synthetic hourly FX data and macro
◦ Test position-aware signal suppression

1. Cross-engine consistency tests:
◦ All three engines use compatible date conventions
◦ All three write to ml_predictions table with consistent schema
◦ Health check correctly detects each engine's state

1. Failure mode tests:
◦ Pipeline handles missing data gracefully
◦ Pipeline handles stale data with proper warnings
◦ Pipeline handles DB lock with retry
◦ Pipeline handles model file missing

Exit criteria:
• Each pipeline can be run from clean state with synthetic data and produces expected output
• Tests catch the bugs we found in this session (window mismatches, etc.)
• All tests pass on a clean checkout

Time estimate: 1-2 sessions.

───

Session 5: Regression Tests

Scope: Every bug we found becomes a test. The bug must be reproducible by removing the fix and the test must fail; with the fix, it passes.

Deliverables:

Tests for each documented bug in KNOWN_ISSUES.md:

1. Equity timer schedule (00:15 not 21:00)
2. Equity service includes feature step
3. Crypto outcome window matches label window
4. Equity outcome window uses trading rows
5. Dashboard connection per page (no module-level cache)
6. User=/Group= forbidden in user-level systemd units
7. FX data refresh in pipeline (not stale-scoring)
8. Crypto auto-ingest in pipeline
9. Health check timer deployed and scheduled correctly
10. Position-aware signal suppression
11. DuckDB lock retry with backoff
12. Repo-vs-deployed config consistency check
13. Dashboard outcome rendering for all 3 engines

Plus structural regression tests:

1. Schema migration test: every table in schema.py has corresponding code that reads/writes it
2. CLI registry test: every documented command in main.py is invokable
3. Service file test: every systemd unit in repo is valid and matches deployed copy
4. Timer schedule test: every timer has the schedule documented in OPERATIONS.md
5. Legacy isolation test: nothing in legacy/ is imported by ACTIVE code (Session 0 hold-the-line)

Exit criteria:
• One test per documented bug
• Each test would have caught the bug if run before deployment
• All tests pass on current code

Time estimate: 1 session.

───

Session 6: Monitoring & Verification

Scope: Automated production checks that detect when something is wrong. Goes beyond the existing health check.

Deliverables:

1. Dashboard-vs-database consistency monitor:
◦ For each engine, query the dashboard rendering function and compare to direct database query
◦ Alert if any displayed value doesn't match underlying data
◦ Runs every 6 hours, sends Telegram alert on mismatch

1. Pipeline execution monitor:
◦ For each pipeline, verify it ran successfully and produced expected row count
◦ Alert if row count is significantly below historical average (anomaly)
◦ Runs after each pipeline schedule

1. Configuration drift monitor:
◦ Compare repo systemd files to deployed copies
◦ Compare repo config files to running service environment
◦ Alert if any drift detected
◦ Runs daily

1. Model performance monitor:
◦ Track rolling 7-day precision per engine
◦ Alert if precision drops below 0.8x of walk-forward baseline
◦ Runs daily

1. Data quality monitor:
◦ For each engine: ratio of expected vs actual ticker/coin coverage
◦ Alert if coverage drops significantly (Yahoo data thinning, Binance outage, etc.)
◦ Runs after each ingestion

1. End-to-end smoke test:
◦ Synthetic prediction request through full stack
◦ Verifies: ingestion endpoint reachable, model loads, prediction generated, dashboard query returns it
◦ Runs hourly

Exit criteria:
• Six monitors deployed as systemd timers
• Each monitor sends Telegram alert on failure
• Documented in OPERATIONS.md

Time estimate: 1 session.

───

Session 7: Hardening & Validation — EXECUTED 2026-05-07

> **Status:** completed. The structural exit criteria (tests pass,
> monitors functional, zero open KIs, docs match reality) are met.
> The "7 consecutive days green" criteria require post-session
> observation discipline. See `SESSION_LOG.md` for the full record
> and the post-session homework list.

Scope: Full audit using the new test suite. Fix anything failing. Document final state.

Deliverables:

1. Run full test suite, fix any failures
2. Run full audit using monitoring stack, fix any issues
3. Performance optimization if needed (slow queries, redundant computations)
4. Final documentation refresh: ARCHITECTURE.md, DATABASE_SCHEMA.md, OPERATIONS.md updated to reflect current state
5. SESSION_LOG.md fully updated
6. KNOWN_ISSUES.md cleared of resolved items
7. Decision point: delete legacy/ directory? Only if 2+ weeks of stability post-Session 0.

Exit criteria:
• All tests pass
• All monitors green for 7 consecutive days
• Documentation matches reality (verified by reading docs and comparing to system)
• Zero items in KNOWN_ISSUES.md
• Health check passes 7 days running

Time estimate: 1 session plus 1 week of monitoring.

───

Estimated Total Effort

8-10 sessions plus ~1 week of monitoring. After that, the system is in a state where:

• Legacy noise is removed from the codebase
• Every bug we've found has a test that prevents regression
• Every pipeline is verified end-to-end
• Configuration drift is detected automatically
• Dashboard-vs-database mismatches are detected automatically
• Performance degradation is detected automatically
• Documentation is canonical and Claude Code reads it before changes
• New features can be added with confidence because tests catch breakage

What This Doesn't Promise

This won't make the models better. The walk-forward AUC/Lift numbers are what they are. Live performance may still differ from backtest. Markets may shift.

What it promises is that when the dashboard shows you a number, you can trust that number. When the system says "20 predictions today, 14 hits", you don't have to verify the math by hand. The plumbing is sound. The decisions you make are based on accurate information.

That's the goal.

How to Execute

Treat this document as the master plan. At the start of each session, tell Claude Code which session number to execute. Don't deviate from the deliverables. Don't skip exit criteria.

If something blocks progress, document it in KNOWN_ISSUES.md and continue. The plan adapts.

After Session 7

Maintenance mode. New features go through the same discipline:
1. Update ARCHITECTURE.md if design changes
2. Write tests before implementation (TDD)
3. Update DATABASE_SCHEMA.md if schema changes
4. Run full test suite before deploying
5. Add monitor if new failure mode possible
6. Update SESSION_LOG.md after every session

This is the engineering discipline that prevents the chaos we've been fighting.
