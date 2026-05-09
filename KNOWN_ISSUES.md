# Known Issues

**2 open observations** (KI-119 and KI-120, both surfaced 2026-05-09
during the discipline session). Neither is a code defect requiring a
hot fix — they are tracked here so a future session triages them
deliberately rather than letting them rot in the working tree.

The historical record of resolved bugs lives in
[`legacy/RESOLVED_ISSUES_ARCHIVE.md`](legacy/RESOLVED_ISSUES_ARCHIVE.md).

## Open

### KI-119 — Phase 1A/1B walkfold backfill writes to a production table without setting model active state

**Symptom.** The Phase 1A/1B crypto backtest workstream (preserved
on branch `crypto-phase-1a-1b-backtest`) populated 36 walk-forward
model_ids' worth of rows into `crypto_ml_predictions` covering
prediction_dates 2024-12-04 → 2026-05-07, without inserting the
matching `crypto_ml_model_runs` rows. The model_runs entries for
those walkfold IDs only got registered on 2026-05-08 19:30 UTC —
all with `is_active=false`. Production scoring uses the same
`crypto_ml_predictions` table, and the pipeline_execution monitor
read both kinds of rows when computing its 14-day rolling baseline.
That contamination is the proximate cause of today's monitor false
positive (resolved by the 2026-05-09 monitor patch).

**Root cause (provisional).** The Phase 1A/1B backfill writer
isolation was incomplete: writing into a production table is fine
in principle, but only if every row is paired with a model_runs
entry whose `is_active` flag is set deliberately at write time.
Without that pairing, downstream consumers (this monitor, the
dashboard's "predictions today" metrics, anything else aggregating
the table) cannot distinguish backtest from production.

**Detection / fix path.** When `crypto-phase-1a-1b-backtest` is next
reviewed for merge-back: verify every writer that touches
`crypto_ml_predictions` (or any `*_ml_predictions` table) registers
its model_id in the matching `*_ml_model_runs` table with
`is_active=false` BEFORE inserting predictions. Consider adding
a regression test in `tests/regression/` that asserts
"every distinct model_id in `*_ml_predictions` has a row in
`*_ml_model_runs`" so the gap surfaces immediately if it recurs.

**Out of scope for the discipline session 2026-05-09.** The
monitor patch landed in this session unblocks the alert; the Phase
1A/1B isolation reinforcement is a separate workstream and lives
on its own branch.

### KI-120 — Equity pipeline_execution flag (10 rows vs 27.5 14d baseline)

**Symptom.** After the monitor fix in this session correctly
filtered baseline counts to active-model rows only, the equity
engine surfaced as `warn`: latest `prediction_date=2026-05-08`,
`n_latest=10` rows, `n_avg=27.5`, `ratio=0.36` (below the 50%
threshold). The crypto fix made this visible by removing the
training-row inflation that previously masked it.

**Possible interpretations** (not yet investigated):

1. The active equity model genuinely scored 10 tickers on
   2026-05-08 — universe regime change or threshold-induced
   thinning analogous to today's crypto count.
2. The 14-day baseline still contains some non-production rows
   (a different writer pattern than crypto's walkfold).
3. A training/walk-forward path on the equity engine is also
   contaminating the predictions table the same way crypto
   was, in which case the same KI-119 fix-pattern applies.

**Detection / fix path.** Run the equity-side equivalent of the
2026-05-09 crypto diagnostic
(`.claude/local_scripts/crypto_volume_diagnostic.py` is the
template): per-day row counts last 21 days for `ml_predictions`
broken down by `model_id`, plus the `ml_model_runs` rows actually
registered. Decide between (1)-(3) based on the data, then either
file the count as expected, add a tighter filter, or open a fix
ticket.

**Out of scope for the discipline session 2026-05-09.** This
finding is a side-effect of the monitor fix verification and is
not part of the listed scope. Tracked here so the next session
triages deliberately.

## Recently resolved (post-Session-7)

- **KI-118** (resolved 2026-05-08, commit `fc6fc28`; regression
  test landed 2026-05-09 on `discipline-session-monitor-and-tracking`)
  — production source files (10 files: `fx/bot/*`,
  `fx/data/refresh.py`, `pipelines/{freshness,health_check}.py`, 5
  `systemd/mhde-*` units) lived in the working tree on the VPS
  without ever being `git add`-ed. Discovered when an audit on
  master flagged them as `??` Untracked despite being imported by
  tracked code and live in active systemd units. **Regression test
  in place**: `tests/regression/test_no_untracked_production_imports.py`
  walks every tracked .py outside `tests/`, `legacy/`,
  `.claude/local_scripts/`, `venv/` and asserts every import
  resolving to a path in the repo is in `git ls-files`; plus
  asserts every `.service`/`.timer` under `systemd/` is tracked;
  plus (when on production host) every deployed mhde-* unit's
  source in `systemd/` is tracked. Wired into
  `scripts/pre-commit.sh`. Verified fail-then-pass with a canary.

- **Pipeline_execution monitor false positive** (resolved
  2026-05-09 on `discipline-session-monitor-and-tracking`) — the
  monitor's 14-day rolling baseline was contaminated by training/
  walk-forward backtest rows that share the predictions tables
  with production scoring. Fixed by filtering BOTH the latest
  count and the baseline to `is_active=true` model_ids in the
  corresponding `*_model_runs` table. Regression test:
  `tests/regression/test_pipeline_execution_baseline.py`. After the
  fix, crypto's 2026-05-09 ratio rose from 0.24 (warn) to 0.78
  (ok) using the same underlying data — proving the previous
  result was the baseline's fault, not a real volume drop.

---

## Conventions for new issues

When a bug is found:

1. Add an entry here under a new `## Open` section. Use the next ID
   in the `KI-0XX` range. Include:
   - **Symptom** (what was observed, ideally with a copy-paste line
     from a log or alert)
   - **Root cause** (where in the code / config / topology it lives)
   - **Detection / fix path** (the operator action when this recurs)
2. When the fix lands:
   - Move the entry to `legacy/RESOLVED_ISSUES_ARCHIVE.md` under
     "All resolved".
   - Replace **Symptom / Root cause / Fix path** with **Resolved
     (date or commit) / Symptom / Fix / Regression test**.
   - Confirm the regression test exists (and fails without the fix —
     this is the discipline from Session 5).
3. Update this file's introductory line: `**N open issues.**` or
   `**No open issues.**` so a future Claude Code session sees state
   at a glance.

---

## Why we keep the archive

The 28 KIs in the archive trace the production-grade transition
documented in `HARDENING_PLAN.md`. Most fall into a few patterns:

- **Schedule / unit drift** (KI-101, KI-106, KI-109, KI-112) →
  caught now by `tests/regression/test_systemd_units.py` and the
  `monitoring/config_drift` runtime monitor.
- **Outcome-window math errors** (KI-103, KI-104) → caught now by the
  per-engine `test_predict.py::test_fill_outcomes_*` and the
  integration `test_*_pipeline_end_to_end` tests.
- **Empty-input crashes** (KI-005, KI-006, KI-007) → caught now by
  unit tests that exercise the empty-DB / empty-universe paths.
- **Model-promotion gaps** (KI-003, KI-009) → caught now by
  `test_active_model_paths_resolve` plus auto-deactivation in every
  engine's train command.
- **Alerting / notification mistakes** (KI-110, KI-001) → caught now
  by FX position-aware suppression tests and the nginx route
  regression check.

When you next find a bug, look for its pattern here before treating
it as novel — the fix likely already has a template.
