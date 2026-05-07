# Session Log

Append-only record of what each `HARDENING_PLAN.md` session actually
accomplished, what changed, and what's pending. Most recent entries
are at the top.

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
