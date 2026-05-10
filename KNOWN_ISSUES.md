# Known Issues

**4 open observations** (KI-122, KI-123, KI-126, KI-131). KI-122/123
are cosmetic; KI-126 is a future Phase 0 enhancement deferred until
weekly reliability snapshots accumulate; KI-131 is a low-priority
single-day production-model row-count dip. None requires a hot fix —
all tracked so a future session triages deliberately rather than
letting them rot in the working tree.

**KI-130** opened + resolved 2026-05-10 — DuckDB 1.5.2
`SELECT DISTINCT col … ORDER BY col DESC LIMIT N` planner regression
caused both dashboard date-selectors to surface only the 2 most
recent prediction_dates instead of 30. Fix: helper
`get_distinct_prediction_dates` using `GROUP BY` in
`dashboard/services/queries.py`. See "Recently resolved" below.

**KI-127** opened + resolved same session (Phase 0 calibration
drift detector false-fired on small-sample-per-bucket noise; fix:
`min_samples_per_bucket=10` guard in
`check_calibration_buckets`). See "Recently resolved" below.

**KI-119, KI-120, and KI-124 resolved** in the 2026-05-09 sessions.
KI-119 reclassified after empirical verification on the merged
`crypto-phase-1a-1b-backtest` branch: the writer isolation is
sound (38 prediction model_ids match 38 model_runs entries
exactly; 36 walkfold model_runs all `is_active=false`); only the
monitor false-positive was real and that was already patched. See
"Recently resolved" below.

**Walk-fold semantics — FAQ.** Walk-fold predictions
(`crypto_{5d,10d}_walkfold_YYYY_MM`) are produced by a **one-shot
Phase 1A backfill, not a daily pipeline**. No systemd timer runs
`crypto/ml/backfill_walkforward.py`. Each backfill execution writes
predictions for the test windows of every walk-forward fold and tags
them `is_active=false` so the daily `crypto predict` pipeline and
the production monitors ignore them. Apparent "walk-fold stopped
writing on day X" patterns reflect the most-recent fold's test-window
boundary — not a pipeline outage. See KI-119 below for the writer
isolation contract.

The historical record of resolved bugs lives in
[`legacy/RESOLVED_ISSUES_ARCHIVE.md`](legacy/RESOLVED_ISSUES_ARCHIVE.md).

## Open

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

### KI-126 — Phase 0 calibration drift definition (b) week-over-week relative not yet implemented

**Symptom (anticipated, not yet observed).** The Phase 0 calibration-
bucket criterion (`crypto/ml/phase0_evaluate.py:check_calibration_buckets`)
currently uses **definition (a) absolute**: flag when ≥ 3 consecutive
same-direction reliability buckets are off the bucket midpoint by
> 10pp. This catches systematic miscalibration in any single
evaluation. It does NOT catch slow drift week-over-week when each
weekly snapshot is individually within tolerance but the trajectory
is shifting (e.g. -3pp this week, -6pp next week, -9pp the week
after — none failing alone, but the trend is real and would matter
at week 6).

**Plan (deferred).** Add **definition (b) relative**: persist the
weekly reliability diagram into a `phase0_reliability_snapshots`
table; the monitor compares this week's per-bucket hit rates against
last week's snapshot and flags > 5pp week-over-week swings in the
same direction across 3+ buckets. Needs ~4 weekly snapshots before
the comparison is meaningful, so wiring it before that is premature.

**Detection / fix path.** When the
`mhde-monitor-phase0-calibration.timer` has accumulated 4+ Sunday
firings, add the snapshot table to `crypto/schema.py`, extend
`monitoring/phase0_calibration.py` to read/write it, add
`check_calibration_drift_relative()` to `phase0_evaluate.py`, and
wire it into the weekly monitor as a fourth alert path. Tests in
`tests/equity/test_monitoring.py` should cover both directions
(rapid drift week 2 → week 3 vs slow drift across 4 weeks).

**Out of scope for the Phase 0 evaluation infrastructure session
2026-05-09.** Recorded here so the future session knows the
definition-(a) coverage today and how to extend.

### KI-131 — crypto 5d model wrote 23 predictions on 2026-05-09 vs ~30 expected

**Symptom.** `crypto_5d_ab428f75` produced 23 rows in
`crypto_ml_predictions` for `prediction_date=2026-05-09`, while the
adjacent days (2026-05-07, 2026-05-08, 2026-05-10) all wrote 15 rows
each per active model (10d + 5d = 30 per date). The 5d total on
2026-05-09 is `15 (10d) + 8 (5d) = 23` rather than 30. The 10d model
wrote a normal 15 rows that day.

**Why monitoring didn't fire.** `monitoring/pipeline_execution.py`'s
14-day ratio test only flags when the latest-day row count drops
below 50% of the 14-day average. 23/30 = 0.77 — comfortably above
the 50% warn threshold and 20% fail threshold. The check is doing
exactly what it was tuned for, just not tight enough to surface this
specific gap.

**Hypotheses (not yet investigated).**
1. Partial pipeline failure mid-run: the 5d predict ran, then crashed
   before completing all 15 universe symbols. 7 of 15 outputs were
   lost. Check `data/logs/crypto_predict.log` for 2026-05-09 ~00:30
   UTC for retry/exception traces.
2. Feature warmup gap: 7 universe symbols had missing features for
   the 5d horizon on 2026-05-09 (5d-horizon-specific feature, not
   shared with 10d). Less likely — the universe is the same for both
   horizons and 10d had no shortfall.
3. Mid-run universe shift: a universe symbol was deactivated between
   the 10d and 5d calls. Unlikely given the 30-min pipeline runtime.

**Detection / fix path.** Low priority — single-day blip, both
horizons fresh on 2026-05-10. Worth a future session reading the
2026-05-09 predict log + tightening the 14d ratio test for the 5d
model specifically (or per-model rather than per-engine) only if the
pattern recurs.

**Out of scope for the dashboard fix session 2026-05-10.** Flagged
during investigation of the dashboard DISTINCT bug (Finding 1
side-observation) and recorded for future triage.

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

## Recently resolved (post-Session-7)

- **KI-130 — Dashboard date-selector returned only 2 dates instead
  of 30** (opened + resolved 2026-05-10). Both prediction-tab date
  dropdowns in `dashboard/app.py` (equity at line 117, crypto at
  line 387) ran:
  ```sql
  SELECT DISTINCT prediction_date FROM <table>
  ORDER BY prediction_date DESC LIMIT 30
  ```
  Against the production DuckDB file this returned only the 2 most
  recent dates rather than 30, despite the crypto predictions table
  containing 523 distinct prediction_dates. The same query with
  `LIMIT 100` returned 100 rows correctly, and the
  `GROUP BY`-shaped equivalent returned 30 rows correctly — pinning
  the cause to a DuckDB 1.5.2 TopN-with-DISTINCT planner fusion that
  triggers data-volume-dependently (does not reproduce in fresh
  in-memory or file DBs even at 40k rows; only manifests on the
  production DB's specific block layout). **Fix.** New helper
  `dashboard.services.queries.get_distinct_prediction_dates` uses
  `GROUP BY` + `ORDER BY` + `LIMIT`, which avoids the broken planner
  path. Both `app.py` call sites switched to the helper. **Tests.**
  `tests/dashboard/test_distinct_date_selector_regression.py` (5
  tests): four contract tests verify the helper's behaviour under
  varied data shapes; a fifth source-level anti-pattern test
  intercepts the SQL the helper actually executes and asserts it
  contains `GROUP BY` and not `DISTINCT` — needed because the bug is
  not reliably reproducible in test data and source inspection is
  the only durable guard. Smoke test
  `.claude/local_scripts/smoke_distinct_dates.py` confirms the fix
  returns 30 crypto dates against the production DB. FX tab
  unaffected (uses `MAX(datetime_utc)` and `WHERE datetime_utc = ?`
  rather than `DISTINCT + ORDER BY + LIMIT`); `filters.py`
  unaffected (uses `GROUP BY run_id` shape, not DISTINCT on a single
  column).

- **KI-129 — engine-export preflight conflated stale pipeline with
  warmup-window symbols** (opened + resolved 2026-05-10 during the
  engine-export contract session). The strict-100% coverage gate in
  `crypto/exports/write_daily_predictions.py:_check_freshness_and_coverage`
  refused to emit a 48/50 file when `BSBUSDT` and `PRLUSDT` had no
  features for `2026-05-10`. Investigation showed both symbols were
  in their 60-day features warmup window (BSBUSDT had 47 days of
  klines, PRLUSDT had 40; the BTCUSDT control showed features start
  exactly `klines_first + 60 days`, matching the longest lookback
  in `crypto/ml/features.py` — `return_60d`, `price_vs_50d_ma`,
  etc.). The features pipeline was working correctly. The bug was
  the preflight gate's premise: not every active universe symbol is
  predictable on every day; symbols recently added to the universe
  must age in. **Fix.** Loosened the preflight to staleness-only
  (`MAX(trade_date) FROM crypto_ml_features == today UTC`); dropped
  the per-symbol coverage check. `n_predictions` now reflects
  whatever's predictable on the export date (48 today; 50 once
  BSBUSDT/PRLUSDT age in around 2026-05-24/2026-05-31). INTERFACE.md
  §3 doesn't mandate `n == universe_size`; engine validation only
  requires `predictions` non-empty + ranks unique + consecutive.
  See the engine-export design doc §5.5 for the corrected semantics.

### KI-128 — Health check thresholds don't account for weekend market closure

**Resolved 2026-05-10.** Fixed via ADR-018. Added
`pipelines/market_calendar.py` with `expected_equity_prediction_date`,
`is_forex_closed`, and `fx_close_floor` helpers. Three callers gate
their existing recency checks on these helpers:

- `pipelines/health_check.py::_check_equity` — uses
  `expected_equity_prediction_date(now)` instead of the literal
  `now - 1d`. No more Sun/Mon false positives.
- `pipelines/health_check.py::_check_fx` and
  `monitoring/pipeline_execution.py` (FX leg) and
  `pipelines/freshness.py::check_fx_freshness` — branch on
  `is_forex_closed(now)`; inside the window the gate is
  `latest >= fx_close_floor(now)` (where `fx_close_floor` returns
  Fri 21:00 UTC, the last bar timestamp expected before close),
  outside it's the existing 2h budget. No more Fri 22:00 UTC →
  Sun 22:00 UTC false positives.

**Accepted limitation.** US market holidays still produce one warn
the day after (Thanksgiving Friday, MLK Monday, etc.). Mirrors
ADR-015's trade-off — a holiday calendar adds dependency surface
for a small noise reduction and weakens active-day outage
detection.

- **KI-127 — phase0 calibration drift detector false-fired on
  small-sample-per-bucket noise** (opened + resolved 2026-05-09 on
  `phase0-evaluation-infrastructure`). Surfaced during the L5
  verification run of `monitor phase0-calibration` against
  production. The 5d crypto model had only 32 filled outcomes
  spread across 9 reliability buckets; one bucket had 3 samples,
  three had 6-8, and the consecutive-runs detector fired on
  three adjacent buckets each with single-digit samples. At n=3
  in a bucket, the 95% CI half-width exceeds 30pp, so the
  observed-vs-midpoint difference was noise, not signal. **Fix.**
  Added `min_samples_per_bucket=10` parameter to
  `crypto/ml/phase0_evaluate.py:check_calibration_buckets`.
  Buckets below the minimum now break the consecutive-run chain
  the same way empty buckets do. Detail line surfaces the count
  of qualifying buckets so the operator can see when the detector
  has enough data to be meaningful. Verified post-fix: monitor
  exits 0 against production. New test in
  `tests/crypto/test_phase0_evaluate.py` pins the small-sample
  guard against a synthetic scenario (3 adjacent 100%-hit
  buckets at n=3 each → no flag); a second test confirms the
  parameter is tunable for future research.

- **KI-125 — sensitivity grid produces multi-axis configs through
  iterated CLI invocations** (opened + resolved 2026-05-09 on
  `phase1b-winner-and-followups`). The factory
  `sensitivity_grid_configs(conn, base_run_ids)` correctly emits
  single-axis sweeps per the agreed Phase 1B spec. But running
  `crypto backtest-grid --grid sensitivity` more than once against
  an evolving DB produces multi-axis configs through greedy axis-
  by-axis hill climbing: the second invocation re-ranks against
  the first invocation's outputs (sensitivity-shape configs now
  have higher Sharpe than the original bases) and starts sweeping
  around them. Each individual invocation respects the contract;
  the chain emerges via repeated re-ranking. The Phase 1B winner
  selection on 2026-05-09 was initially reported with a config
  (`db11de9b`) produced by THREE chained invocations — not the
  agreed single-axis grid. Caught by operator review of the
  reported provenance. **Fix.** `main.py:crypto backtest-grid` now
  detects when any selected base is not in the canonical 20-row
  base grid (the deterministic `run_id` set emitted by
  `base_grid_configs()`) and refuses with a clear error message
  that points at this KI. `--allow-iterated` overrides with a
  loud warning. The factory docstring in
  `crypto/execution/backtest/runner.py` documents the gotcha for
  programmatic callers (tests, notebooks) that bypass the CLI
  guard. Tests in `tests/crypto/test_backtest_runner.py` pin
  both the block path and the bypass+warn path.
  Side-effect: the actual Phase 1B winner is the strict-slice
  result `backtest_10d_D_top_n_a02e15a0` (single trail-axis change
  from a published base; 4/4 gates pass). See
  `docs/PATH_TO_LIVE_PLAN.md` and `docs/PHASE1B_HANDOFF.md` for
  the locked-in spec.

- **KI-119 — Phase 1A/1B walkfold backfill writer isolation**
  (originally opened 2026-05-09 in the discipline session;
  reclassified to "by design, verified" 2026-05-09 in the Phase
  1A/1B resumption session). The original framing claimed the
  walkfold writer left model_id rows in `crypto_ml_predictions`
  without matching rows in `crypto_ml_model_runs`. Empirical
  verification on the merged
  `crypto-phase-1a-1b-backtest` branch contradicted this: every
  one of the 38 distinct model_ids in `crypto_ml_predictions`
  has a corresponding row in `crypto_ml_model_runs`. The 36
  walkfold model_runs are all `is_active=false`; the 2 production
  model_runs (`crypto_5d_ab428f75`, `crypto_10d_db171418`) are
  `is_active=true`. The original symptom (monitor flagging crypto
  on 2026-05-08/09) was real, but the proximate fault was in the
  monitor — the 14-day baseline counted both walkfold and
  production rows because it didn't filter on
  `is_active=true`. That fault was patched in the 2026-05-09
  discipline session
  (`monitoring/pipeline_execution.py:_check_engine_pipeline` JOINs
  `*_ml_model_runs` with the active filter; regression test
  `tests/regression/test_pipeline_execution_baseline.py`). With
  that filter in place, the walkfold rows are correctly
  segregated from the production baseline. The Phase 1A/1B writer
  is doing the right thing — it always was — and
  `PATH_TO_LIVE_PLAN.md` codifies the design ("is_active integrity
  preserved" is one of the six validation checks the Phase 1A
  backfill enforces). No further fix needed; KI-119 closes here.
  Probe script that produced the verification:
  `.claude/local_scripts/probe_ki119_isolation.py` (kept under the
  session-artifact gitignore prefix).

- **KI-124 — pipeline_execution recency budget too tight for
  equity's T-1 scoring** (resolved 2026-05-09 on
  `fix-ki124-equity-recency-budget`). Equity's `prediction_date`
  is `T-1` and stays at "Friday" for 72+ hours over a weekend
  (the Friday 00:15 fire scores Thursday; the Monday 00:15 fire
  scores Friday; nothing fresh between). The `RECENCY_BUDGET`
  was 27h for both equity and crypto with the same comment, but
  crypto trades 24/7 and equity does not. Production monitor
  was firing `recency_ok=False` for ~22 of every 24h on equity
  even when the pipeline was healthy. **Fix.** Raised
  `RECENCY_BUDGET["equity"]` to 75h (72h weekend roll + 3h
  grace). Crypto stayed at 27h; FX stayed at 2h. Inline comment
  documents why the budgets are asymmetric. ADR-015 captures
  the design decision and explains why holiday-extended
  weekends are deliberately not covered (each hour added to the
  budget weakens the monitor's ability to detect a real
  outage). **Verification.** Monitor against production DB now
  reports all three engines green:
  ```
  [equity] recency_ok=True count_ok=True   latest=2026-05-08
           n_latest=43  n_avg=50.0  ratio=0.86
  [crypto] recency_ok=True count_ok=True   latest=2026-05-09
  [fx]     recency_ok=True count_ok=True   latest=2026-05-09 08:00
  ```
  Future option captured: add `row_inserted_at TIMESTAMP` to
  `ml_predictions` and key the recency check off real write time
  (would let all three budgets shrink back to single-hour
  multiples). Schema migration; deferred.

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
