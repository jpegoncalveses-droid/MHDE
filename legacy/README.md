# legacy/

Code preserved here is **dormant**. It is no longer reachable from any
running pipeline, service, or dashboard tab. Nothing under this directory
is imported by ACTIVE code. The contents are kept for reference and
rollback safety, not for execution.

Created 2026-05-07 by Session 0 of `HARDENING_PLAN.md`. After two weeks
of stable operation post-Session 7, this directory is a candidate for
deletion (see `DECISIONS.md`).

## What's here, and why

### Whole-directory moves

| Original path | Reason |
|---|---|
| `backtest/` | Stub backtest framework — never wired into prediction or evaluation pipelines. |
| `governance/` | Source/scorecard/prompt registries, signal governance, decision log, feature flags — all dormant infrastructure that was never adopted. |
| `learning/` | Feedback loop (calibration, error taxonomy, experiments, insights) — never wired into the prediction loop. |
| `models/` | Original ML scaffolding (shadow_ranker, dataset_builder, promotion_gates, registry, xgboost_ranker, evaluation) that predates the per-engine `ml/`, `crypto/ml/`, `fx/ml/` rebuilds. The trained-artifact directory `models/saved/` was deliberately **kept at the active path** — every engine still reads/writes there. |
| `review/` | Flask review server (`server.py`, ~3900 lines) and its packet importer. Served `https://mhde.duckdns.org/review/` until Session 0 — now disabled (`mhde-review-server.service`, `mhde-bridge-relay.service`) and the nginx route is removed. |

### Individual files

| Original path | Reason |
|---|---|
| `crypto/ml/hypothesis_tests.py` | Dev-only research harness, not in any pipeline. |
| `fx/ml/hypothesis_tests.py` | Dev-only research harness, not in any pipeline. |
| `dashboard/pages/_legacy/*` (19 pages) | Streamlit pages from the pre-rebuild dashboard. ML / crypto / FX content was rewritten as tabs inside `dashboard/app.py`. The `_legacy` subdir was already a quarantine area. |
| `hypotheses/registry.py` | Old hypothesis registry, replaced by per-engine flow. |
| `ml/retrain.py` | Earlier retrain entrypoint, superseded by `ml/train.py:train_walk_forward` (called by `ml_train_cmd`). |
| `missed/attribution.py`, `catalyst_report.py`, `detector.py`, `episode_tracker.py`, `investigator.py`, `labels.py`, `llm_policy.py`, `report.py`, `sector_attribution.py` | Dormant pieces of the missed-opportunity pipeline. The ACTIVE subset of `missed/` (catalyst_queue, catalyst_digest, prediction_report, root_cause_enrichment, ...) stays at the original path because daily-analysis still runs them. |
| `outcomes/candidate_lifecycle.py`, `outcomes/labels.py` | Dead code: only ever imported by the legacy review server and a re-export in `outcomes/__init__.py` that has been removed. |
| `pipelines/weekly_review.py` | Weekly-review CLI orchestration — the CLI itself is no longer wired to a timer. |
| `reports/weekly_review.py` | Companion to the weekly-review CLI. |
| `scoring/incomplete_diagnostics.py` | Dev tool, not invoked by any active path. |
| `storage/inventory.py` | Powers the `data inventory` CLI (not wired to any timer). |
| `universe/ticker_details_enricher.py` | Powers the `data enrich-ticker-details` CLI (not wired to any timer). |

### Tests (in `legacy/tests/`)

29 tests target modules that moved to `legacy/`. They import from
`backtest`, `governance.feature_flags`, `governance.signal_governance`,
`learning.*`, `models.*`, `missed.{attribution,detector,episode_tracker,
investigator,labels,llm_policy,report,sector_attribution,catalyst_report}`,
`outcomes.{candidate_lifecycle,labels}`, `review.*`, `scoring.incomplete_
diagnostics`, `storage.inventory`, `universe.ticker_details_enricher`.

Pytest is configured (`pytest.ini`) with `testpaths = tests`, so these
files are not collected automatically.

## What is *not* here

The `INFRASTRUCTURE.md`, `OPERATIONS.md`, `ARCHITECTURE.md`, and
`HARDENING_PLAN.md` documents at the repo root remain canonical sources
for the system as it runs. Don't refer to anything inside `legacy/` for
how production behaves today.

## Running this code from `legacy/`

Import paths inside `legacy/` still reference top-level package names
(e.g., `legacy/review/importer.py` imports `from learning.error_taxonomy
import ...`). Because `learning/` is no longer at the top of `sys.path`,
those imports will fail. **The code is reference-only.** If you ever need
to re-activate something, move it back to its original path first.

## Plan accuracy notes

`HARDENING_PLAN.md`, Session 0, listed several directories as legacy that
turned out to be partially or fully ACTIVE:

- `scoring/` — `scoring/scorecard.py` is still imported by
  `pipelines/daily_radar.py` (which runs as part of
  `mhde-daily-analysis.service` Mon-Fri 23:15). Only
  `scoring/incomplete_diagnostics.py` was movable.
- `features/` — same story; `features/feature_builder.py` is still
  imported by daily-radar via `scoring.scorecard.compute_scores`.
- `missed/` — only the dormant subset moved; `catalyst_queue`,
  `catalyst_digest`, `prediction_report`, `root_cause_enrichment` are
  invoked daily by `run_mhde_daily_analysis.sh`.
- `daily_radar` orchestration — the plan implied it was legacy beyond
  equity ingestion. In fact every `daily_radar` step is still wired into
  `mhde-daily-analysis.service`.
- The `/review/` Flask server *was* legacy and is now retired; the plan
  was correct on that one.
