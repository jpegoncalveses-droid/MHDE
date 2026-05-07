# Session Log

Append-only record of what each `HARDENING_PLAN.md` session actually
accomplished, what changed, and what's pending. Most recent entries
are at the top.

---

## 2026-05-07 — Session 2: Test Infrastructure

**Branch:** `session-2-test-infra` off `master @ fb744bf`.

Framework only. No production-code tests written this session — that's
Sessions 3-5.

### What was completed

All 9 tasks:

1. Categorized the 71 active tests via AST import analysis
   (`.claude/local_scripts/categorize_tests.py`): equity 60, integration
   8, crypto 2, dashboard 1, fx 0.
2. Created the 6 subdirs: `tests/equity/`, `tests/crypto/` (already
   existed), `tests/fx/`, `tests/dashboard/`, `tests/integration/`,
   `tests/regression/` — each with an `__init__.py`.
3. Reorganized all 71 tests via `git mv` in batches (history
   preserved). Ran offline test subset between batches; one test
   (`test_daily_analysis_script.py`) needed a `..` → `../..` path fix
   after moving into `tests/integration/`. All 540 offline tests still
   pass post-reorg.
4. Extended `tests/conftest.py` with 7 new fixtures: `temp_db`
   (in-memory DuckDB pre-loaded with all 4 schema sources →
   storage/migrations + ml/crypto/fx schema.py),
   `synthetic_prices_equity` / `synthetic_prices_crypto` /
   `synthetic_prices_fx` (deterministic random walks with engine-
   appropriate vol and weekend handling), `synthetic_filings`,
   `synthetic_fundamentals`, `mock_telegram` (intercepts
   `requests.post` to Telegram and any `notifications.telegram` helpers).
5. Added `tests/helpers.py` with `assert_db_state`,
   `assert_pipeline_completed_cleanly`, and a stub
   `assert_dashboard_renders` for Session 4.
6. Wrote `tests/test_session2_infra_smoke.py` — 7 tests validating the
   fixtures themselves. All pass in 0.5s.
7. Added `Makefile` with `test`, `test-unit`, `test-integration`,
   `test-regression`, `coverage`, `install-hooks`, `precommit`, `help`
   targets. Network-touching tests skipped by default; override with
   `make NET_SKIPS= test-unit`.
8. Wrote `scripts/pre-commit.sh` — 3-stage hook (py_compile staged
   .py, curated pytest smoke, forbidden-pattern lint). Wall-clock
   runtime 1.8s. Symlinked into `.git/hooks/pre-commit` via
   `make install-hooks`.
9. Added `pytest-cov` to `requirements.txt` and installed in venv.
   `make coverage` runs the unit subset with coverage and writes an
   HTML report to `htmlcov/`. Coverage and `.coverage` files added to
   `.gitignore`.

### What was changed

- Tests reorganized: 71 files moved (`git mv`).
- New: `tests/equity/__init__.py`, `tests/fx/__init__.py`,
  `tests/dashboard/__init__.py`, `tests/integration/__init__.py`,
  `tests/regression/__init__.py`,
  `tests/test_session2_infra_smoke.py`, `tests/helpers.py`,
  `Makefile`, `scripts/pre-commit.sh`.
- Modified: `tests/conftest.py` (extended), `requirements.txt`
  (pytest-cov), `.gitignore` (coverage outputs).
- Symlink installed: `.git/hooks/pre-commit` → `scripts/pre-commit.sh`.
- Path fix in `tests/integration/test_daily_analysis_script.py`
  (`../`→`../../` for the moved location).

### Bugs caught and fixed during the session

- One real path bug surfaced by the reorg:
  `test_daily_analysis_script.py` used a `dirname(__file__)/..` path
  that broke when the file moved one directory deeper. Caught
  immediately by running pytest after the integration batch.

### New known issues to track

None. Existing KI-003 (manual model promotion) is the only open item.

### Pending for the next session (Session 3)

- Write actual unit tests using the new fixtures: features, labels,
  predict, evaluate, signals — for each of the 3 engines. Target the
  80%+ coverage threshold called out in the plan.
- Decide whether `assert_dashboard_renders` should call the underlying
  query functions in `dashboard/services/queries.py` (likely yes —
  matches what the dashboard consumes without booting Streamlit).

---

## 2026-05-07 — Session 6: Monitoring & Verification

**Branch:** `session-6-monitoring` off `master @ 0808b47`.

Six runtime monitors built, each as a Python module under `monitoring/`,
wired to a `main.py monitor <name>` CLI subcommand and a paired
`systemd/mhde-monitor-*.{service,timer}`. The smoke monitor caught a
real production issue (KI-009) on its first dry-run.

### What was completed

All 12 tasks. New code:

- `monitoring/__init__.py` + `monitoring/alert.py`: shared dispatcher
  with `MonitorResult` dataclass, severity prefixing, and a
  `MONITORING_DRY_RUN=true` env switch that suppresses real Telegram
  sends. Bottoms out in `fx.bot.telegram_bot.send_message`.
- `monitoring/dashboard_consistency.py` (6h): dashboard query layer
  vs direct DB count parity.
- `monitoring/pipeline_execution.py` (hourly): per-engine recency +
  row-count vs 14d rolling avg. Floors: 50% warn, 20% fail.
- `monitoring/config_drift.py` (daily): repo `systemd/*` ↔ deployed
  copies in `/etc/systemd/system` + `~/.config/systemd/user`.
- `monitoring/model_performance.py` (daily): rolling 7d precision per
  active model vs walk-forward baseline. 0.8x threshold.
- `monitoring/data_quality.py` (daily): per-engine ticker / symbol /
  bar coverage on latest day vs 14d avg. 0.8x floor.
- `monitoring/smoke_test.py` (hourly): DB opens, every active joblib
  loads, dashboard query layer returns rows.

CLI: new `cli.group monitor` with 6 subcommands in `main.py`.

systemd: 12 unit files in `systemd/mhde-monitor-*.{service,timer}`.
**Not auto-deployed.** Install instructions in OPERATIONS.md.
Schedules staggered to avoid the FX :05 firing window:
  dashboard 03/09/15/21:30 | pipeline :40 | config-drift 12:15 |
  model-perf 13:15 | data-quality 02:00 | smoke :50.

Tests: `tests/equity/test_monitoring.py` — 12 tests covering each
monitor's pure-logic path with `temp_db` and `mock_telegram`.

OPERATIONS.md: new "Monitors" section — catalog table, manual
invocation, deploy steps, threshold tuning constants, alert format,
overlap with existing health-check, alert-suppression playbook.

### Bug found mid-session — KI-009

**Equity active-model joblibs missing on disk.** The smoke test on
its first dry-run reported:

```
[!!] MHDE monitor: smoke_test
End-to-end smoke failed
- equity model: path missing: models/saved/5d_label_5d_3pct_20260505_092040.joblib
```

`ml_model_runs` has 3 rows with `is_active=true` pointing at:
- `models/saved/5d_label_5d_3pct_20260505_092040.joblib`
- `models/saved/10d_label_10d_5pct_20260505_092031.joblib`
- `models/saved/20d_label_20d_5pct_20260505_092022.joblib`

`/home/jpcg/MHDE/models/saved/` currently contains only `crypto/` and
`fx/` subdirectories — the 3 equity joblibs are gone. Likely cause is
some operation between caf77e4 (KI-004 `git rm --cached`) and now
that deleted the on-disk files. Git history cannot recover them since
they were de-tracked.

**Why Session 5's regression test missed it.** `test_models_saved_path_exists`
only asserts the directory exists, not that specific paths in
`*_model_runs.is_active=true` resolve. Recorded as KI-009; Session 7
should harden the test.

**Action required for production.** Re-run `venv/bin/python main.py
ml train --label label_5d_3pct …` (and 10d / 20d) to regenerate, or
wait for the weekly Sun 21:30 retrain.

### Bugs found in monitor code (caught by tests, fixed)

- `monitoring/dashboard_consistency.py`: invalid SQL
  `SELECT COUNT(*) … ORDER BY as_of_date DESC LIMIT 200` — DuckDB
  rejects ORDER BY without aggregation. Dropped the ORDER BY.
- `monitoring/model_performance.py`: queried `target_threshold` for
  all engines, but `fx_ml_model_runs` uses `target_pips`. Per-engine
  column selection now.

### Verification

- All 6 monitors run cleanly under `MONITORING_DRY_RUN=true`. They
  produced a mix of OK and real-alert results — alerts surfaced
  KI-009 plus an FX freshness lag (latest bar 3h old vs 2h threshold)
  and FX-baseline anomalies (training stored 1.0 / 0.99 sentinel
  baselines, real rolling precision is below the 0.8× cutoff).
- `make test-unit`: 607 passed in 37s (was 595 — +12 from monitor
  tests).
- `make test-regression`: 20 passed in 7s.

### Pending for next session (Session 7)

- **Resolve KI-009** by retraining or copying joblibs from a backup.
  This is operational, not code.
- Harden `test_models_saved_path_exists` to validate every
  `is_active=true` row's `model_path` resolves.
- Investigate the FX freshness lag (3h old) vs the 2h threshold —
  scheduled hourly run may be drifting.
- Investigate FX baseline = 1.0 sentinel in `fx_ml_model_runs.precision_at_threshold`.
  Either fix the train code or change the monitor's "no real
  baseline" guard.
- Deploy the 6 monitors to production once Session 7 audit completes.

---

## 2026-05-07 — Session 5: Regression Tests

**Branch:** `session-5-regression-tests` off `master @ 8e45129`.

20 dedicated regression tests across 5 files in `tests/regression/`,
plus a coverage map in `tests/regression/__init__.py` linking each
KI in `KNOWN_ISSUES.md` to the test that guards it. Found and fixed
one new production bug (KI-008) in the daily-analysis wrapper.

### What was completed

All 7 tasks. Files added:

- `tests/regression/__init__.py` — KI-→-test mapping table.
- `tests/regression/test_systemd_units.py` (7 tests). Covers KI-101
  (retrain timer staggering), KI-102 (equity predict ExecStart chain),
  KI-106 (no User=/Group= in user-level units), KI-108 (crypto predict
  6-step chain), KI-109 (health-check timer deployed), KI-112 (every
  repo unit validates + matches deployed copy).
- `tests/regression/test_dashboard_structure.py` (3 tests). Covers
  KI-105 (no module-level DB connection — AST scan that walks only
  Module.body, not function bodies, after a v1 false positive),
  KI-113 (outcome columns present in all 3 schemas).
- `tests/regression/test_legacy_isolation.py` (3 tests). Session 0
  hold-the-line: no active code imports legacy/, legacy/ exists with
  ~99 .py files, README explains the dormant status.
- `tests/regression/test_schema_consistency.py` (4 tests). KI-001
  (nginx /review/ 404 block in conf), KI-117 (models/saved/ exists),
  schema migration: every CREATE TABLE has reader+writer in active
  code (engine schemas + storage/schema.sql, with explicit DORMANT
  exclusions for `scorecard_experiments` and `promotion_gate_results`).
- `tests/regression/test_cli_registry.py` (3 tests). Every `main.py`
  command invoked from systemd / shell wrappers responds to `--help`.
  KI-004 trained-model artifacts gitignored.

### Bug found and fixed during this session

**KI-008** — `priority-refresh-queue` invoked at the wrong CLI level.

The daily-analysis wrapper `run_mhde_daily_analysis.sh` ran
`main.py priority-refresh-queue --enriched-csv ...` but the CLI is
actually registered under the `data` group (`main.py data priority-refresh-queue`,
defined at `main.py:581`). The wrapper's `tee`-pipe swallows the click
exit code, so `set -e` couldn't trap it. **Step d has been silently
failing every Mon-Fri 23:15** since the command was moved under
`data`.

Fix: changed the wrapper to invoke `main.py data priority-refresh-queue`.
Recorded as KI-008 with the lesson "set -e doesn't propagate through
tee — either drop the tee or add explicit error checks."

The new test (`test_systemd_main_commands_invokable`) caught it
immediately by parsing every `main.py X Y` invocation in systemd /
shell wrappers and running `--help` on each.

### Coverage map — every KI now has a regression test

See `tests/regression/__init__.py` for the full table. Highlights:

| Layer | Coverage |
|---|---|
| Sessions 3 / 4 unit + integration | KI-103, KI-104, KI-107, KI-110, KI-111 (already in place) |
| Session 5 regression | KI-001, KI-004, KI-005, KI-006, KI-007, KI-008, KI-101, KI-102, KI-105, KI-106, KI-108, KI-109, KI-112, KI-113, KI-116, KI-117 |
| No test (documentation drift, not code) | KI-002 |
| Open (will get tests when fixed) | KI-003 |

### Verification

- `make test-regression`: 20 passed in 5.4s.
- `make test-unit`: 595 passed in 36s (unchanged — regression target is
  separate per plan).
- `make test-integration`: 56 passed + 1 skipped in 66s (unchanged).

### Pending for the next session (Session 6)

- Build the 6 monitoring jobs: dashboard-vs-DB consistency, pipeline
  execution, configuration drift, model performance, data quality,
  end-to-end smoke. Each fires a Telegram alert on failure.
- Decide whether to expand pre-commit-hook smoke to include
  `make test-regression` (5.4s — fits the budget).
- Investigate how long Step d of daily-analysis has been silently
  failing in production (KI-008). If priority-refresh-queue.csv
  hasn't been refreshed in months, the root-cause-enrichment chain
  may have stale inputs.

---

## 2026-05-07 — Session 4: Integration Tests

**Branch:** `session-4-integration-tests` off `master @ 985e243`.

End-to-end pipeline tests with synthetic data plus failure-mode
coverage. All major regression cases from `KNOWN_ISSUES.md` are now
covered by at least one integration test.

### What was completed

All 8 tasks. Tests added by file:

- `tests/integration/_helpers.py` — `train_tiny_model` (XGBoost +
  Platt + medians bundle), `register_active_*_model` (3 engines),
  `seed_active_company`, `seed_crypto_universe`, and price-insert
  helpers. Reusable across all engine tests.
- `tests/integration/test_equity_pipeline.py` — 3 tests. 50 tickers ×
  220 days → labels → features → score → fill_outcomes. Covers KI-104
  (trading-day window).
- `tests/integration/test_crypto_pipeline.py` — 3 tests. 5 symbols ×
  80 days. Covers KI-103 (horizon window match).
- `tests/integration/test_fx_pipeline.py` — 4 tests (1 skipped on
  weekend bar). 600 hourly bars × 4 active models. Covers KI-110
  (position-aware alert suppression) end-to-end through the bot
  helper, with `_open_conn` monkeypatched to return temp_db.
- `tests/integration/test_cross_engine_consistency.py` — 6 tests:
  shared prediction columns, distinct entity keys
  (ticker / symbol / time-only), per-engine model_runs tables,
  freshness coverage, health orchestrator parity.
- `tests/integration/test_failure_modes.py` — 8 tests: stale-data skip
  (equity / crypto), stale-but-continue (FX, ADR-010), empty-universe
  graceful handling, no active models → empty predictions, **DuckDB
  lock-retry (KI-111)** with monkeypatched `duckdb.connect` and
  `time.sleep`, non-lock IOException propagation, missing model file
  → FileNotFoundError raised.

Plus the Session 4 deliverable from `tests/helpers.py`:

- `assert_dashboard_renders` — replaced the Session 2 stub with a real
  implementation that calls `dashboard/services/queries.py` directly
  (one of 12 page-query functions) and validates row count + key set.
  Sidesteps Streamlit's runtime entirely.

### Design notes

- **Tiny model factory.** Integration tests need a real joblib bundle
  for `predict.py` to load. `train_tiny_model` fits an XGBClassifier
  on noise with `positive_rate=0.85` so the model produces probabilities
  above the LOW_THRESHOLD=0.50 filter — otherwise predict.py drops all
  predictions before they reach the table.
- **Precision metrics not asserted.** Synthetic random-walk data has
  no predictive signal by construction; an integration test asserting
  precision range against a noise-trained model is meaningless.
  Tests assert structural completeness (predictions written, outcomes
  filled, schema parity, dashboard query returns rows) instead.
- **FX bot connection.** `fx/bot/telegram_bot.py:_open_conn` opens its
  own connection from `storage.config`, not a passed-in conn. Tests
  monkeypatch `_open_conn` to return a wrapper around `temp_db` whose
  `close()` is a no-op (the fixture owns lifetime).

### Bugs caught during this session

None. All four engine pipelines produced expected output on first
correct setup. One test failure was self-inflicted (used
`'long_gbp'` as the position string when production code expects
`'HOLDING_GBP'`); fixed in the test.

### Verification

- `make test-integration`-equivalent: 56 passed + 1 skipped in 66s.
- `make test-unit` unaffected: still passes.
- Integration tests cover regressions for: KI-103, KI-104, KI-110,
  KI-111. Plus the new Session 3 fixes are exercised through pipeline
  runs (KI-005, KI-006, KI-007).

### Pending for the next session (Session 5)

- Convert each entry in `KNOWN_ISSUES.md` resolved-section into a
  dedicated regression test that the suite runs going forward. Many
  are already implicitly covered; Session 5 makes that coverage
  explicit by name and adds the structural regression tests called
  out in the plan (schema migration, CLI registry, service files,
  timer schedules, legacy isolation).
- Fix the `datetime.utcnow()` deprecation warnings (Python 3.12+).
  Mostly cosmetic; ~10 call sites across pipelines/daily_radar,
  conftest, and a few others.

---

## 2026-05-07 — Session 3: Unit Tests

**Branch:** `session-3-unit-tests` off `master @ d69837f`.

Wrote ~80 unit tests across the 12 target modules called out in the
plan (features, labels, predict, evaluate / signals — per engine —
plus health checks and pipeline freshness). All target modules now at
or above the 80% coverage target.

### What was completed

All 7 tasks. Tests added by file:

- `tests/fx/test_features.py` — 6 tests
- `tests/fx/test_labels.py` — 7 tests
- `tests/fx/test_predict.py` — 5 tests
- `tests/fx/test_signals.py` — 9 tests (full BUY/SELL/WAIT decision matrix)
- `tests/crypto/test_features.py` — 5 tests
- `tests/crypto/test_labels.py` — 6 tests
- `tests/crypto/test_predict.py` — 7 tests
- `tests/crypto/test_evaluate.py` — 5 tests
- `tests/equity/test_ml_features.py` — 5 tests
- `tests/equity/test_ml_labels.py` — 6 tests
- `tests/equity/test_ml_predict.py` — 7 tests (incl. KI-104 trading-day window regression)
- `tests/equity/test_ml_evaluate.py` — 5 tests
- `tests/equity/test_health_ml_checks.py` — 8 tests
- `tests/equity/test_pipeline_freshness.py` — 12 tests (per-engine freshness reports)

Plus `tests/equity/test_health.py` was rescued: 6 tests that had been
failing pre-Session-0 (CatalogException due to a missing
`ml_predictions` table in the local fixture) now pass — the local
`conn` fixture was rewritten to delegate to the project-wide `temp_db`
fixture, which loads every active schema.

### Coverage on plan-listed modules

All ≥ 80% target met:

| Module | Coverage |
|---|---|
| ml/features.py | 80% |
| ml/labels.py | 100% |
| ml/predict.py | 90% |
| ml/evaluate.py | 98% |
| crypto/ml/features.py | 92% |
| crypto/ml/labels.py | 100% |
| crypto/ml/predict.py | 83% |
| crypto/ml/evaluate.py | 100% |
| fx/ml/features.py | 81% |
| fx/ml/labels.py | 100% |
| fx/ml/predict.py | 98% |
| fx/ml/signals.py | 92% |
| health/checks.py | 89% |
| health/ml_checks.py | 87% |
| pipelines/freshness.py | 97% |

Average across the 15 modules: ~92%.

### Bugs caught during the session and fixed

Three real production bugs surfaced by the new tests:

- **KI-005** `fx/ml/labels.py`: `IndexError` on empty `fx_prices_hourly`.
  Second `for` loop did `range(n - 48, n - 24)` → negative-bounded
  range. Fix: `range(max(0, n - 48), max(0, n - 24))`.
- **KI-006** `ml/features.py`: `Parser Error` on empty equity ML
  universe — `WHERE ticker IN ()` is invalid SQL. Fix: early return
  when `tickers` is empty.
- **KI-007** `ml/evaluate.py`: `ValueError: min() arg is empty` when
  `print_walk_forward_results` is called with zero folds. Fix: guard
  the success-criteria block on `if fold_results:`.

All three documented in `KNOWN_ISSUES.md` with regression-test pointers.

Plus one fixture fix in `tests/conftest.py`: `synthetic_prices_fx`
default `data_quality` was `"good"` (the schema default) but production
writes `"OK"` and the labels/features queries filter for `'OK'`. Synthetic
default now `"OK"` to match.

### Verification

- `make test-unit`: 595 passed in 38.5s. ~38s wall-clock — slightly over
  the plan's 30s target, mostly from the equity ml/features feature
  computation over 600 synthetic bars × 2 symbols.
- All 15 target modules ≥ 80% coverage.
- Pre-commit hook still 1.6s.

### Pending for the next session (Session 4)

- Integration tests: end-to-end pipeline runs with synthetic data
  (already established by the test-infra), plus failure-mode tests
  (missing data, lock retry, model file absent).
- Replace the stub `assert_dashboard_renders` in `tests/helpers.py` —
  the suggested implementation is to call
  `dashboard/services/queries.py` directly without booting Streamlit.
- Decide whether to widen `make test-unit` time budget from 30s to 45s
  given Session 3 made it ~38s; or split slow tests into `test-slow`.

---

## 2026-05-07 — Pre-Session-2 follow-ups (KI-001, KI-004)

**Branch:** `pre-session-2-fixes` off `master @ 1050eab`.

Two outstanding issues from earlier sessions resolved before starting
Session 2 (test infrastructure).

### KI-001 — `/review/` returns 502 → 404

The nginx conf at `/home/jpcg/homeboard/nginx/nginx.conf` already had
the `location /review/ { return 404; }` block from Session 0's
follow-up edit, but `nginx -s reload` was leaving the response at 502.

Diagnosis: the host file is a **single-file bind mount** into the
nginx container. The Edit tool writes via atomic rename, which changes
the host file's inode. Docker single-file bind mounts pin to the
original inode and don't follow rename-replace, so nginx kept reading
the old config inside the container even after a reload.

Fix: `docker compose restart nginx` to force the container to re-mount
and re-read the file. `/review/` now returns 404 cleanly.

Lesson recorded in `KNOWN_ISSUES.md` KI-001: future host-file edits
that feed bind-mounted single files need either a full container
restart or an inode-preserving editor (`sed -i`, `cat > file << EOF`).
Plain `nginx -s reload` will silently serve stale config.

### KI-004 — `models/saved/**` gitignored

Added four patterns to `.gitignore`:
```
models/saved/**/*.joblib
models/saved/**/*.pkl
models/saved/**/*.bin
models/saved/**/*.model
```

Removed the 3 previously-tracked equity joblibs from the index with
`git rm --cached` (files preserved on disk). All 9 model binaries (3
equity + 2 crypto + 4 FX) on disk are now ignored. Verified by
`git ls-files models/saved/` returning empty.

### Pending for Session 2

Test infrastructure: pytest fixtures (in-memory DuckDB with all
schemas, synthetic data per engine, mock Telegram), helpers, Makefile
targets, CI runner, coverage reporting.

---

## 2026-05-07 — Session 1: Documentation as Source of Truth

**Branch:** `session-1-documentation` off `master @ f59baf9`.

### What was completed

All 9 tasks from the Session 1 task list:

1. Mapped every database table from `ml/schema.py`, `crypto/schema.py`,
   `fx/schema.py`, `storage/schema.sql`, and `storage/migrations.py`,
   plus enumerated the 52 tables in the live DB to confirm complete
   coverage.
2. Wrote `DATABASE_SCHEMA.md` — purpose + columns + reader/writer
   modules per table, grouped by engine. Cross-cutting notes on time
   conventions, outcome filling, active-model resolution, single-row
   tables.
3. Traced each engine's pipeline end-to-end by reading
   `pipelines/{ml,crypto,fx}_prediction_pipeline.py` and
   `pipelines/freshness.py`. Captured the chained ExecStart structure,
   freshness policies, fill_outcomes behavior.
4. Wrote `ARCHITECTURE.md` — system overview with ASCII data flow,
   per-engine sections (equity ML, crypto ML, FX ML), the
   daily-analysis path, dashboard, health check, cross-cutting infra,
   and the ATSRP external dependency. Plus a "what's not in production"
   pointer at `legacy/`.
5. Wrote `OPERATIONS.md` — runbook layer: daily smoke checks, manual
   pipeline invocations per engine, recovery procedures (DuckDB lock,
   stale data, missing model file, Telegram, dashboard 502, nginx),
   deploy procedures, dashboard auth rotation, prediction history
   queries, source-specific ingestion debugging, escalation matrix.
6. Wrote `KNOWN_ISSUES.md` — bug tracker with naming convention
   (KI-0XX open, KI-1XX resolved). 4 open issues (the /review/ 502,
   plan-vs-codebase drift now resolved, manual model promotion, and
   `models/saved/` not gitignored) plus 17 resolved entries with
   Session 5 regression-test pointers.
7. Expanded `DECISIONS.md` from 5 to 12 ADRs. Added ADR-006 (XGBoost
   choice), ADR-007 (walk-forward CV), ADR-008 (DuckDB single-file),
   ADR-009 (service chaining in ExecStart), ADR-010 (freshness guards),
   ADR-011 (position-aware FX alerts), ADR-012 (per-engine
   `schema.py`). Verified each claim against active code before
   recording.
8. Updated `CLAUDE.md` read-first list to point at the new docs in the
   right reading order. Appended this Session 1 entry to
   `SESSION_LOG.md`.
9. Verified Session 1 exit criteria — every database table documented,
   every systemd unit referenced via `INFRASTRUCTURE.md` from
   `ARCHITECTURE.md` and `OPERATIONS.md`, every major decision has an
   ADR, the new docs are internally cross-linked.

### What was changed

- New: `DATABASE_SCHEMA.md`, `ARCHITECTURE.md`, `OPERATIONS.md`,
  `KNOWN_ISSUES.md`.
- `DECISIONS.md`: appended 7 new ADRs.
- `CLAUDE.md`: read-first list expanded from 5 entries to 8, ordered
  by what's needed first.
- `SESSION_LOG.md`: this entry.

No code changes. Session 1 was a pure documentation pass.

### Bugs caught and fixed during the session

- One spec drift caught while writing `DATABASE_SCHEMA.md`: the dead
  `outcomes/labels.py` file was supposedly resolved in Session 0, but
  the per-table reader/writer audit confirmed `outcomes/__init__.py`
  no longer references it. Accurate.

### New known issues to track

None new. All issues recorded as KI entries already existed.

### Pending for the next session (Session 2)

- Build pytest scaffolding: `tests/conftest.py` fixtures for in-memory
  DuckDB with all schemas applied, synthetic data generators per
  engine, mock Telegram. CI runner. Coverage reporting.
- Decide on `models/saved/` gitignore policy (KI-004) before the next
  retrain otherwise the binaries will grow the repo.
- Decide on auto-promotion for `*_train_cmd` (KI-003) so the weekly
  retrain actually changes the live model.

---

## 2026-05-07 — Session 0: Legacy code cleanup

**Branch:** `session-0-legacy-cleanup` off `master @ 7b46c50`.

### What was completed

All 11 tasks from the Session 0 task list:

1. Pre-flight checkpoint commit (`7b46c50`) capturing in-flight FX /
   pipeline / systemd work that was in the dirty tree at session start.
2. Inventory of all 250+ project .py files via reachability analysis
   (`.claude/local_scripts/inventory_active_legacy.py`). Entry points
   were derived from systemd unit `ExecStart` lines, the
   `mhde-daily-analysis.service` shell wrapper, and dashboard imports.
3. Confirmed every LEGACY candidate is unreachable from ACTIVE code via
   grep + import-graph BFS.
4. Moved 70 dormant code files into `legacy/` via `git mv` (history
   preserved). 5 whole directories: `backtest/`, `governance/`,
   `learning/`, `models/`, `review/`. Plus targeted moves under
   `crypto/ml/`, `fx/ml/`, `ml/`, `missed/`, `outcomes/`, `pipelines/`,
   `reports/`, `scoring/`, `storage/`, `universe/`, `hypotheses/`, and
   the entirety of `dashboard/pages/_legacy/` (19 pages).
5. Moved 29 legacy-targeting tests to `legacy/tests/`
   (`.claude/local_scripts/find_legacy_targeting_tests.py` derived the
   list).
6. Disabled `mhde-review-server.service` and `mhde-bridge-relay.service`
   (`systemctl --user disable --now`).
7. Removed the `upstream mhde_review` block and the `location /review/`
   block from `/home/jpcg/homeboard/nginx/nginx.conf`. JP ran the
   `nginx -t` and reload (config valid; `/` still 200; `/review/` now
   returns 502).
8. Fixed two import breakages caused by the move:
   - `outcomes/__init__.py` re-exported a function from the
     now-legacy `outcomes/labels.py`. Re-export deleted (no callers).
   - `reports/weekly_review.py` was an orphan tied to the dead
     `weekly_review` CLI; moved to `legacy/reports/weekly_review.py`.
9. Verified safe-checks per JP's choice (no live pipelines, no test
   telegram):
   - `python -m py_compile` over every active .py: clean (exit 0).
   - Import-resolution smoke on 50 entry-point modules: 50/50 OK.
   - `systemd-analyze verify` on every unit in `systemd/`: 13/13 clean.
   - Dashboard query smoke (`MHDE_DASHBOARD_AUTH_ENABLED=false …
     test_dashboard_queries.py`): 10/10 queries pass.
   - `pytest --collect-only`: 743 tests collected, no errors.
10. Wrote new docs: `legacy/README.md`, `DECISIONS.md` (5 ADRs),
    updated `INFRASTRUCTURE.md` (review server section + bridge-relay
    + nginx route), updated `CLAUDE.md` (read-first list + legacy
    pointer), initialized this `SESSION_LOG.md`.

### Plan corrections (recorded in DECISIONS.md ADR-005)

`HARDENING_PLAN.md` Session 0 listed several items as legacy that
turned out to be ACTIVE:

- `scoring/scorecard.py` is still imported by `pipelines/daily_radar.py`
  via `mhde-daily-analysis.service` (Mon-Fri 23:15). Only
  `scoring/incomplete_diagnostics.py` was movable from `scoring/`.
- `features/feature_builder.py` is still imported transitively by the
  same path. `features/` stays.
- The "missed" CLI is partly active: `missed.catalyst_queue`,
  `missed.catalyst_digest`, `missed.prediction_report`,
  `missed.root_cause_enrichment` are all invoked by the daily-analysis
  shell script with `--no-mock --provider openai`. Only the dormant
  subset (9 files) moved.
- `daily_radar` orchestration is fully active.
- `mhde-health-check.service` exists and runs `main.py system
  health-check`; the plan didn't mention it.

### What was changed

- 18 prior in-flight tracked files committed as `7b46c50` (FX
  position-aware alerts, pipeline freshness guards, service chaining).
- ~100 .py files moved into `legacy/` plus 29 tests.
- `outcomes/__init__.py`: dead `compute_forward_returns` re-export
  removed.
- `INFRASTRUCTURE.md`: review server / bridge-relay sections retired;
  user-services table updated (added `mhde-health-check`); restart
  cheat sheet pruned; reverse-proxy routes pruned.
- `CLAUDE.md`: read-first list now points at `HARDENING_PLAN.md`,
  `DECISIONS.md`, `SESSION_LOG.md`, plus a `legacy/` warning.
- `/home/jpcg/homeboard/nginx/nginx.conf`: review upstream + location
  removed.
- New: `DECISIONS.md`, `legacy/README.md`, this `SESSION_LOG.md`.

### Bugs found and fixed during the session

- **`models/saved/` was almost lost.** `git mv models/ legacy/models/`
  swept the trained-artifact directory into `legacy/`. Caught when
  active config grep showed `ml/train.py:26`, `crypto/config.py:26`,
  `fx/config.py:31`, `health/ml_checks.py:17` all hardcode
  `models/saved`. Restored with `git mv legacy/models/saved
  models/saved` before any pipeline could miss the artifacts.
- **Dead `outcomes.compute_forward_returns` re-export.** First active
  module to fail the import smoke test. Removed the line from
  `outcomes/__init__.py` (ADR-004).

### New known issues to track

- `https://mhde.duckdns.org/review/` returns 502 instead of 404. The
  Streamlit catch-all matches the path and the relay errors. Add an
  explicit `location /review/ { return 404; }` block in a follow-up.
- `HARDENING_PLAN.md` Session 0 description is partially wrong about
  what's legacy (see ADR-005). Update the plan in Session 1 so
  Sessions 2-7 don't re-derive the same misclassifications.
- 8 tests under `legacy/tests/` (and the 29 total) won't run from
  there — they import top-level `governance.*`, `learning.*`, etc.,
  which now live under `legacy.governance.*`. Acceptable for
  reference-only state. Session 5 (regression tests) will replace
  them with new active tests where appropriate.

### Pending for the next session (Session 1)

- Update `HARDENING_PLAN.md` with the corrected legacy / active
  classification before doing the full ARCHITECTURE.md /
  DATABASE_SCHEMA.md / OPERATIONS.md / KNOWN_ISSUES.md write-up.
- Initialize `KNOWN_ISSUES.md` (the 502 issue and the plan-vs-code
  drift go in there).
- Decide whether to delete or archive the empty `dashboard/pages/`
  directory (currently has no content but is still tracked).
- Decide whether `data` CLI subcommands (`data inventory`,
  `data enrich-ticker-details`, `data sector-diagnostics`,
  `data peer-cluster-diagnostics`) are worth keeping in `main.py`
  given their underlying modules moved to `legacy/storage/inventory.py`
  and `legacy/universe/ticker_details_enricher.py`. Currently the CLI
  registers but the commands ImportError when invoked.
