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

## Resumption queue (in order)

### 1. Run cross-asset backfill — ✅ COMPLETE 2026-05-14

Shipped on branch `fix-vix-symbol-and-macro-freshness` (merge `75ec5da`).
Backfill ran via `.claude/local_scripts/backfill_cross_asset_2026-05-05.py`
+ `venv/bin/python main.py ingest fred`. Verification surfaced and
fixed a Yahoo symbol bug: bare `VIX` resolved to a dormant
mutual-fund placeholder, not CBOE VIX. Fix translates ticker → Yahoo
symbol at the API boundary (`_YAHOO_SYMBOL = {"VIX": "^VIX"}`).
Added `check_macro_series_freshness()` covering DGS10/DGS2/VIXCLS to
close the symmetric blind spot.

### 2. Verify backfill post-state — ✅ COMPLETE 2026-05-14

Folded into Step 1's branch. Post-state: all 13 reference tickers
T-0 fresh; DGS2 + VIXCLS T-1 fresh in `macro_series`;
`check_cross_asset_freshness()` and `check_macro_series_freshness()`
both pass.

### 3. Fix KI-149 (silent T-2 skip) — ✅ COMPLETE 2026-05-14

Shipped on branch `fix-ki149-honest-equity-freshness` (merge `a75efc5`).
Three-layered fix in single branch:

- `pipelines/freshness.py:check_equity_freshness` — coverage-aware
  (latest trade_date row count must be ≥ 50% of mean daily count over
  prior 30 trade dates); `FreshnessReport` extended with `reason`,
  `coverage_row_count`, `coverage_expected_min`.
- `ml/predict.py:score_universe` — `StaleFeaturesError` cross-check
  when `MAX(ml_features.trade_date) < MAX(prices_daily.trade_date)`;
  `--allow-stale-features` CLI flag for the soft-mode backfill case.
- `ingestion/ingest_prices.py:PricesIngestor.ingest_dates` —
  `IngestionError` raised post-loop when Polygon grouped returns 403
  with `in_universe=0`, naming the affected date(s). Surviving non-403
  dates are persisted before the raise.

10 new tests (3 freshness + 4 predict + 3 ingest); 880 tests pass
across equity + integration, 0 regressions.

### 4. Fix KI-150 (broken monitor services) — ✅ COMPLETE 2026-05-14

Shipped on branch `fix-ki150-monitor-services` (merge `f8762d7`).

Phase 1 investigation surfaced that the original KI-150 diagnosis
mis-attributed `mhde-equity-pipeline-monitor.service` exit-1 to a
service crash. Local dry-run confirmed exit-1 is by-design RED signal
per `monitoring/pipeline_monitor/daily_runner.py:142`. The
operator-clarity gap (`systemctl status` reporting `Active: failed`
indistinguishably from a real crash) is filed as [[KI-153]] for a
later session.

Parts 2 + 3 (data-quality + pipeline-execution monitors bypassing
their alert path on DuckDB lock contention) were the real bug. The
failing path was `monitoring/alert.py:_open_default_conn`, which
opened a writable DuckDB connection to persist `monitor_alert_state`.
Fixed by moving throttle state to a JSON sidecar
(`monitoring/alert_state_store.py`, `fcntl.flock`-serialized RMW).
`send_alert` no longer depends on DuckDB writer availability. New
regression test reproduces the failure mode under a real DuckDB
writer-lock and asserts state persists + Telegram fires.

138 monitoring tests pass; full equity + integration regression
clean.

### 5. Update dashboard to advertise T-2 honestly — ✅ COMPLETE 2026-05-14

Shipped on branch `feat-dashboard-t2-labeling` (merge `8044da8`, commit
`6de1187`). Three surfaces:
1. Page-level caption under the "ML Predictions" title explaining the
   T-2 cadence and pointing operators to this doc.
2. Per-date banner labelling the gap as T-0 / T-1 / **expected T-2** /
   stale. Banner copy from pure helper
   `dashboard.services.maturity.format_equity_t2_banner` so the four
   cadence branches are unit-testable without Streamlit.
3. Predictions subheader names the date verbatim.

### 6. Freshness graceful degradation (replaces "T-2 alignment retrain")

**Original step 6 ("retrain models on T-2 alignment") was MISFRAMED
and is closed.** See
`data/processed/finding5_pipeline_gap_and_t2_alignment.md` §5. The
ML pipeline (`ml/labels.py`, `ml/features.py`, `ml/train.py`) is
time-of-inference-invariant: features at trade_date D are computed
identically whether D is read as T-0 by yesterday's pipeline or as
T-2 by today's. There is no "T-0 vs T-2 distributional shift" to
retrain away. KI-149 (strict freshness) + Step 5 (dashboard
labelling) were the complete fix for the operator-facing concern.

What did need closing was a **secondary operational defect** that
finding5 surfaced: under KI-149's strict-MAX coverage check, the
2026-05-15 morning showed the pipeline failing entirely rather than
degrading to the latest fully-covered trade_date (T-2). Polygon
free-tier 403-on-current-day + Yahoo/ReferenceTickers partial
fallback every weekday meant `MAX(trade_date)` had partial coverage
every morning, so the daily predict run skipped indefinitely.

Branch `fix-freshness-backward-scan` (push pending operator merge)
extends `pipelines/freshness.py:check_equity_freshness` with a
scan-backward selector: walk distinct `trade_date` values DESC from
MAX (bounded by `max_trading_days`), accept the first that satisfies
the existing 50% coverage threshold. `FreshnessReport` gains
`latest_covered_date` (the selected date) and `is_partial_max`
(True if MAX was partial and the selector degraded). The pipeline
passes `latest_covered_date` to `score_universe`. A WARNING fires
on every degraded run so the upstream gap stays visible.

`mhde-retrain.timer` continues its normal Sun 21:30 UTC weekly
cadence — that's the standard walk-forward retrain that picks up
new cross-asset features now that DGS2/VIXCLS/sector ETFs are warm.
It is **not** a "T-2 alignment retrain"; just the regular weekly run.

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
