# Session Log

Append-only record of what each `HARDENING_PLAN.md` session actually
accomplished, what changed, and what's pending. Most recent entries
are at the top.

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
