# Known Issues

**4 open observations** (KI-119, KI-122, KI-123, KI-124). KI-119
surfaced 2026-05-09 during the discipline session. KI-122 / KI-123
surfaced 2026-05-09 during the equity ingestion fix session as
out-of-scope cleanups identified while triaging the 520-ticker
universe cap. KI-124 surfaced 2026-05-09 during the same session's
verification — the pipeline_execution recency budget for equity is
too tight given equity's T-1 scoring schedule. None require a hot
fix — they are tracked here so a future session triages them
deliberately rather than letting them rot in the working tree.

**KI-120 resolved** the same session by switching the Polygon
ingestor to the grouped-daily endpoint (see "Recently resolved"
below for fix detail).

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

### KI-122 — Universe builder reconciliation leaks stale extended-tier rows

**Symptom.** `companies WHERE is_active=true` returns 678 rows
(504 primary + 174 extended), but a fresh universe build only
intends to populate ~520 rows (504 primary + ~16 extended slots
under `max_symbols=520`). The 174 extended-tier rows are residue
from prior builds — old extended fillers that were active on a
previous date and never got deactivated when the SP500 list
shifted or when the SEC filter chose a different set of fillers.

**Root cause.** `universe/universe_builder.py:148-164` deactivates
primary-tier rows that fall off the current S&P list, but has no
analogous reconciliation for extended-tier rows. So extended-tier
`is_active=true` rows accumulate monotonically across builds.

**Detection / fix path.** Mirror the primary-tier reconciliation
for extended: `UPDATE companies SET is_active = false WHERE
universe_tier = 'extended' AND ticker NOT IN (<current_extended_set>)`.
Add a regression test that walks back the universe builder twice
with disjoint extended sets and asserts `companies` flips correctly.

**Out of scope for the equity ingestion fix session 2026-05-09.**
The 174 stale rows don't currently flow through to `ml_features`
or `ml_predictions` (the predict/features stages don't carry
extended-tier tickers in practice — confirmed in the cap audit),
so no production data quality impact today. Tracking for a future
universe-cleanup session.

### KI-123 — Misleading "Dev mode" log line in daily_radar.py

**Symptom.** `pipelines/daily_radar.py:83` logs `"Dev mode: capped
tickers to %d (universe has %d)"` whenever `len(tickers) >
max_symbols`. The "Dev mode" prefix implies the cap is a debugging
shortcut, but `max_symbols=520` is the deliberate production
universe scope (see ADR-014). The log line gives operators the
wrong impression that production is running in a degraded mode.

**Root cause.** Historical: the cap was added during early dev as
a runtime-tunable to limit Polygon-cost while iterating, and the
log line predates the decision to make 520 the canonical scope.

**Detection / fix path.** Drop the "Dev mode: " prefix. Suggested
replacement: `"Universe capped to %d (companies WHERE is_active=true
has %d, see ADR-014 for cap rationale)"`. Trivial one-liner.

**Out of scope for the equity ingestion fix session 2026-05-09.**
Documentation/clarity fix; no behavioral impact.

### KI-124 — pipeline_execution recency budget too tight for equity's T-1 scoring

**Symptom.** After the equity ingestion fix landed in the
2026-05-09 session, `pipeline_execution.run()` against the
production DB returned:
```
[equity] recency_ok=False count_ok=True
  reason: latest prediction_date=2026-05-08 is 1 day, 8:34h old,
          threshold 1 day, 3:00h
  n_latest=43 n_avg=50.0 ratio=0.86  ← count side green
```
Volume is fine; recency flags every cycle.

**Root cause.** `monitoring/pipeline_execution.py` derives `age =
now - latest_dt`, where `latest_dt = MAX(prediction_date)` treated
as midnight UTC of that date. Equity scoring runs at 00:15 UTC
daily, but writes `prediction_date = T-1` (yesterday). So as soon
as the 27h `RECENCY_BUDGET["equity"]` window passes 24h since
midnight of the prediction_date — i.e., for ~22 of every 24 hours
between fires, plus the entire weekend — recency_ok is False even
though the pipeline ran on schedule. The schema has no
`row_inserted_at` column on `ml_predictions`, so the monitor has
no other anchor available.

**Detection / fix path.** Two reasonable fixes:

1. *Easy.* Raise `RECENCY_BUDGET["equity"]` to 75h (covers the
   weekend gap: Friday 00:15 fire → next Monday 00:15 fire = 72h
   plus 3h grace). Same for crypto, which has the same daily
   pattern (RECENCY_BUDGET["crypto"]=27h today; crypto is
   continuous so a 27h budget might be correct for it — verify).
2. *Better but more work.* Add `row_inserted_at TIMESTAMP` to
   `ml_predictions` / `crypto_ml_predictions` (default
   `CURRENT_TIMESTAMP`). Have the monitor check
   `MAX(row_inserted_at)` instead of `MAX(prediction_date)`. This
   gives a true freshness signal independent of the prediction-date
   cycle.

**Out of scope for the equity ingestion fix session 2026-05-09.**
The volume question (KI-120) is what was asked for; this recency
issue pre-existed and just became visible after the volume side
went green. Tracked here for a future monitor-tuning session.

## Recently resolved (post-Session-7)

- **KI-120 — equity ml_predictions volume thinning May 5-8** (resolved
  2026-05-09 on `fix-equity-ingestion-degradation`). The original
  triage incorrectly suspected (a) Yahoo thinning, (b) smaller
  eligible universe, or (c) model drift. Real cause: the Polygon
  ingestor (`ingestion/ingest_prices.py`) looped per-ticker
  against `/v2/aggs/ticker/{ticker}/range/1/day/` for every active
  universe ticker. Free-tier rate limit (~5 req/min) made the
  ~520-call run unreliable; most days only 50-200 of 520 tickers
  succeeded, which thinned `prices_daily` → `ml_features` →
  `ml_predictions` linearly. **Fix.** Switched the ingestor to
  Polygon's grouped-daily endpoint
  (`/v2/aggs/grouped/locale/us/market/stocks/{date}`), one HTTP
  call per date returning ~12k US tickers in ~1s. Added bounded
  per-ticker fallback for the rare universe ticker missing from
  the grouped feed. Added 13s throttle between consecutive calls
  + 65s 429 retry to stay under the free-tier limit. Backfilled
  May 5-8 from one-shot script
  `.claude/local_scripts/equity_backfill_prices.py`. Re-ran
  `ml backfill-features` + `ml predict` for those dates.
  **Verification (post-fix vs the May 9 diagnostic):**

  | trade_date | prices_daily | ml_features | ml_predictions |
  |---|---|---|---|
  | 2026-05-05 | 82 → **520** | 42 → **312** | 24 → **43** |
  | 2026-05-06 | 47 → **519** | 24 → **312** | 0  → **41** |
  | 2026-05-07 | 463 → **514** | 282 → **311** | 43 → **45** |
  | 2026-05-08 | 53 → **514** | 29 → **311** | 10 → **37** |

  pipeline_execution monitor: equity `count_ok=True` with
  `n_latest=43, n_avg=50.0, ratio=0.86`. (Recency side still
  flags — tracked separately as KI-124.) Regression test:
  `tests/equity/test_ingest_prices.py` (7 cases covering grouped
  filter, non-trading-day, fallback firing, fallback cap,
  idempotency, missing key, default lookback). Verified
  fail-then-pass would require reverting to the per-ticker loop;
  the new tests pin the grouped-path contract.

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
