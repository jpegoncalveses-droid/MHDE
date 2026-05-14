# Equity workstream — PAUSED

## Status

**PAUSED as of 2026-05-14.** Architectural direction committed (T-2
honest). Partial infrastructure fix shipped. Resumption requires no
relitigation of decisions, just continuation of execution.

The crypto workstream remains active in parallel.

## Architectural decision (committed, not for re-debate)

**T-2 honest.** Polygon free-tier limitation is accepted; the system
is to be made truthful about its T-2 cadence rather than upgraded to
paid Polygon.

Rationale:

- Paper trading does not require T-0 prediction freshness.
- Free-tier Polygon is the current baseline; no operating-cost
  increase.
- Forces honest plumbing across freshness checks, dashboard labels,
  and downstream contract surfaces — fixes a defect class
  ([[KI-149]]) rather than papering over it with a paid tier.

Future live equity execution would require revisiting (~$29–79/mo
Polygon paid or alternative source). That is a separate workstream
not in scope until paper trading is calibrated and a go-live decision
is made.

## What's done

- **ADR-032** — Validation methodology for parameters affecting
  portfolio drawdown. Institutional methodology; applies equally to
  the crypto workstream and any future equity ML spec changes.
- **`feat-cross-asset-ingestion` merged** (`53faccc`,
  2026-05-14) —
  - `ingestion/ingest_reference_tickers.py:ReferenceTickersIngestor`
    registered in `orchestrator._ALL_INGESTORS`.
  - Constant `REFERENCE_TICKERS = (SPY, VIX, XLK, XLF, XLV, XLE, XLY,
    XLI, XLP, XLB, XLU, XLRE, XLC)` — 11 SPDR sector ETFs (including
    XLC, the one the SECTOR_ETF_MAP needed for the 23 Communication
    Services tickers in primary) + SPY + VIX.
  - `ingestion/ingest_fred.py:_SERIES` extended with `DGS2` (yield
    curve input, missing since initial commit) and `VIXCLS`
    (FRED-side VIX backup).
  - `health/ml_checks.py:check_cross_asset_freshness()` asserts each
    reference ticker has a `prices_daily.trade_date` within T-3;
    fail/warn message names the offending ticker(s). Will become the
    alert signal once [[KI-150]] (broken monitors) is fixed.
  - Tests: `tests/equity/test_reference_tickers_ingestor.py` (5),
    `tests/equity/test_fred_ingestor_series.py` (3),
    `tests/equity/test_health_ml_checks.py` (+4).
- **KIs filed:** KI-146, KI-147, KI-148, KI-149, KI-150 covering
  follow-ups (script extension + runbook), the deployed-vs-killswitch
  gap, the silent T-2 skip defect chain, and the broken monitor
  services.
- **Investigations preserved:**
  - `data/processed/finding1_cross_asset_ingestion_root_cause.md`
  - `data/processed/finding3_ml_pipeline_gap_root_cause.md`
  - `data/processed/stooq_t0_coverage_audit.md`

## What's NOT done — resumption queue (in order)

### 1. Run cross-asset backfill

Script ready at `.claude/local_scripts/backfill_cross_asset_2026-05-05.py`
(local-only, gitignored). Invokes `ReferenceTickersIngestor.ingest()`
with a 1y window; ON CONFLICT preserves any prior manual bootstrap
rows. Also need a FRED backfill for DGS2 + VIXCLS — easiest path is
`venv/bin/python main.py ingest fred` which runs only the FRED
ingestor and writes DGS2 + VIXCLS observations under the extended
`_SERIES`.

**Estimated effort:** 30 min.

### 2. Verify backfill post-state

- `health/ml_checks.py:check_cross_asset_freshness()` returns
  `status=pass`.
- `prices_daily` latest `trade_date` per source for SPY, VIX, all 11
  sector ETFs is T-1 or T-0 (not 2026-05-04/05).
- `macro_series` populated for `DGS2` (was 13d stale) and `VIXCLS`
  (was absent).

### 3. Fix KI-149 (silent T-2 skip)

The smallest fix that closes the silent-skip is the two-part change in
[[KI-149]]'s resolution paths:

- Tighten `pipelines/freshness.py:67` to require row-count coverage
  for the latest date (e.g. ≥ 50% of the 30d-window ml_features
  ticker count), not just `MAX(trade_date)`.
- Add a cross-check in `ml/predict.py:93-95`: when
  `MAX(ml_features.trade_date) < MAX(prices_daily.trade_date)`, log
  `WARNING` so the divergence is visible in
  `data/logs/equity_predict.log`.

Single branch, single concern. Compatible with the T-2 honest
direction — the warning labels the prediction as stale, doesn't change
which date is scored.

**Estimated effort:** 1–2 hr.

### 4. Fix KI-150 (broken monitor services)

Three services failing simultaneously:

- `mhde-equity-pipeline-monitor.service` — exit-1 every daily fire
  since 2026-05-14 01:00 UTC. Most likely a deterministic failure
  inside `main.py monitor equity-pipeline`. Reproduce interactively,
  capture traceback, fix.
- `mhde-monitor-data-quality.service` + `mhde-monitor-pipeline.service`
  — exit-0 but log shows
  `_duckdb.IOException: Could not set lock on file …` in a loop,
  followed by `alert: could not open MHDE DB — bypassing throttle`.
  Open the monitor connection with `read_only=True` (these monitors
  only read tables); move alert-throttling state off DuckDB to a
  JSON-on-disk sidecar so the alert path doesn't need the write lock.

Once green, `check_cross_asset_freshness()` (already shipped in
`feat-cross-asset-ingestion`) becomes the alert path for [[KI-149]]
and Finding 1's gap class.

**Estimated effort:** 2–4 hr depending on root cause of the
equity-pipeline-monitor exit-1.

### 5. Update dashboard to advertise T-2 honestly

- Header / page-level "predictions as of T-2 [date]" copy.
- Any "today's signals" framing softened to "latest predictions
  (T-2)".
- Tooltip explaining the cadence so future operators don't think the
  pipeline is broken.

Pure UI; pairs with step 3 conceptually but can ship independently.

### 6. Retrain models on T-2 alignment

Current models were trained on T-0 historical features (labels formed
from prices following the T-0 feature snapshot). Applying a
T-0-trained model to T-2 inputs creates a subtle distributional shift
because the input distribution at T-2 differs slightly from T-0
(missing two days of decay). Retrain on T-2 alignment to remove the
shift.

The next scheduled retrain is Sunday (`mhde-retrain.timer` Sat 21:30
UTC?, confirm in `systemctl list-timers`). Could be brought forward
once steps 1–4 are green and the inputs are clean.

### 7. Wait for forward windows under clean conditions

After step 6, forward outcomes accumulate:

- 5d outcomes resolve in ~5 trading days.
- 10d outcomes resolve in ~10 trading days.
- 20d outcomes resolve in ~20 trading days.

No work to do here, just calendar time.

### 8. Calibration assessment

Only after steps 1–7. Compare hit rate vs base rate by horizon,
prediction-probability calibration plot, drift analysis on
feature-distribution shift between training set and live, sector
concentration vs ADR-021's correlation-limit goal. Decide whether
paper trading produces predictions worth pursuing to live equity
execution (which itself would re-open the T-2 honest vs T-0 paid
question per the architectural decision above).

## Pause reason

Operator focus shift to crypto workstream (rescue-rate heatmap
analysis, deep-loser intervention strategies). The equity workstream
infrastructure has reached a state where it can be left for days or
weeks without compounding damage: predictions continue to ship every
weekday (silently stale, but not corrupting the training data); the
ADR-032 methodology binds any future spec change; the KI tracker
records the four open defects so a future session triages
deliberately.

## To resume

Start at step 1. No decisions to re-make; just execution.

If the architectural direction changes (e.g. operator decides to fund
paid Polygon for T-0), the only impact is on step 5 (dashboard
labels) and step 6 (retrain alignment) — steps 1–4 are still required
regardless.

## References

- `DECISIONS.md` → **ADR-032** — validation methodology.
- `KNOWN_ISSUES.md` → [[KI-146]], [[KI-147]], [[KI-148]] (crypto
  spec / methodology); [[KI-149]] (silent T-2 skip); [[KI-150]]
  (broken monitors).
- `data/processed/finding1_cross_asset_ingestion_root_cause.md` — the
  ingestion-side investigation that produced
  `feat-cross-asset-ingestion`.
- `data/processed/finding3_ml_pipeline_gap_root_cause.md` — the
  pipeline-side investigation that produced KI-149 + KI-150.
- `data/processed/stooq_t0_coverage_audit.md` — the audit that
  closed the door on "Stooq as T-0 primary" as an alternative to the
  T-2 honest direction.
- `SESSION_LOG.md` 2026-05-14 entries — `feat-cross-asset-ingestion`
  merge + universe-tier sort fix.
- This doc is the entry point for resumption. Read it first.
