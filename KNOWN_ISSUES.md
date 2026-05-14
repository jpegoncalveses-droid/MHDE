# Known Issues

**19 open observations** (KI-122, KI-123, KI-126, KI-131, KI-132,
KI-134, KI-136, KI-137, KI-139, KI-141, KI-144, KI-145,
KI-146, KI-147, KI-148, KI-149, KI-151, KI-152, KI-153). KI-122/123 are cosmetic; KI-126 is a future
Phase 0 enhancement deferred until weekly reliability snapshots
accumulate; KI-131 is a low-priority single-day production-model
row-count dip; KI-132 is a dashboard-deployment-process gap (no
auto-restart on merge); KI-134 is an alerting-signal-quality
observation (operator missed 8 freshness alerts under weekend
false-positive noise that KI-128 has since cleaned up); KI-136 is the
planned "Gap 2.5" follow-up — the paper-trading drift monitor's
P&L-band / drawdown / monthly arms, deferred until the engine's
`daily_pnl` table starts filling (blocked on engine-side RECONCILE-001);
KI-137 is the crypto post-parabolic re-entry bias — *mitigated* by the
exclusion filter (ADR-021 + ADR-028) but the model-label root cause is
the open follow-up; KI-139 catalogs the per-pipeline monitor's v1 scope
cuts (no auto-remediation, coarse equity-dashboard mtime check, no
engine-side "why 0 positions" reason, reconcile timer unchecked —
ADR-026); KI-141 is a follow-up to ADR-029 —
add a true run-time timestamp column to `crypto_ml_predictions` so the
pipeline_execution monitor can detect a single missed fire (the
ADR-029 budget bump fixed the false-positive but trades sensitivity
for truthfulness); KI-144 is the shared-helper extraction follow-up
to ADR-031 (the universe-tier sort lives in two byte-identical SELECTs
and any future drift would silently re-introduce the KI-143 bug class);
KI-145 is the `max_symbols=520` cap removal — vestigial after the
2026-05-09 Polygon grouped-daily switch and would moot the KI-143
displacement entirely; KI-146 and KI-147 are the two ADR-032
follow-ups — extending the walkfold validation script to compute
per-fold portfolio max DD so both ADR-032 gates run inside one
script (KI-146), and documenting the two-gate pre-ship checklist in
OPERATIONS.md so the methodology binds operators who weren't in the
room for the trail_pct=0.10 decision (KI-147); KI-148 is the
deployed-spec-vs-kill-switch gap surfaced by the same workstream —
`PHASE1B_WINNER_RUN_ID` points to a filter-OFF backtest stating
`portfolio_max_dd_pct = -23.7%` while the filter-ON re-run of the
same config is -31.9%, which already exceeds the -30% engine kill
switch (no production trip yet because no F2-shaped regime under
filter-ON live, but the envelope is misstated and the switch is
calibrated against the wrong number); KI-149 is the equity pipeline's
silent T-2 skip — a three-layer defect chain (Polygon free-tier 403
on current-day grouped, row-count-blind freshness check in
`pipelines/freshness.py:67`, and no scoring-date cross-check in
`ml/predict.py:93-95`) that ships predictions two trading days behind
every weekday while every component reports OK. KI-151 is the
"Other"-class characterization follow-up to KI-140's closure — 47% of
deep losers fit a "late-stage uptrend reversal" profile that is
describable (AUC 0.74) but not filterable without unacceptable
winner-rejection cost; productive paths are non-exclusion (probability
haircut / direction-aware label). KI-152 is the alert-channel
signal-to-noise observation surfaced during today's GBPEUR_C001 reset
— operational monitoring alerts and trading signals share a single
Telegram channel, the EXIT_TARGET alert was buried in ops chatter, and
channel/severity separation is the unscoped fix. KI-153 is the
operational observability gap surfaced by the KI-150 Phase 1
investigation — `*-pipeline-monitor.service` units exit 1 by design
when the pipeline they monitor is red, which `systemctl status`
reports as `Active: failed (Result: exit-code)` and is
indistinguishable from a real monitor crash; the initial KI-150
diagnosis confused the two and prescribed a fix for a non-bug.

**KI-138** opened + resolved (option A) 2026-05-12 — the cap-at-today-1
OHLCV ingestion fix (commit 8f9d707) made `MAX(trade_date)` in
`crypto_ml_features` structurally `today-1`, but the export's staleness
gate required it to equal `today`, so `crypto export-predictions` aborted
every day, `predictions_latest.json` went stale, the engine rejected on
`export_date != today_utc`, and no positions were placed. Fix (option A):
widen the gate to accept `today` or `today-1`, set `export_date` in the
JSON to today UTC (the trading date), add an informational
`features_as_of_date` field. Option B — aligning
`crypto_ml_predictions.prediction_date` semantics with the features-as-of
date vs the trade date — is the deferred follow-up. See "Recently
resolved" below and ADR-025.

**KI-142** opened + resolved 2026-05-14 — the equity Stooq fallback's
2-day freshness window silently broke after the 2026-05-09 Polygon
grouped-daily switch (commit `473b92a`). With Polygon now reliably
writing T-1 prices the same evening, every universe ticker satisfied
"has prices in last 2 days" and Stooq stopped fetching today's quotes
(production logs: 517 → 2 → 6 rows on 05-11 → 05-12 → 05-13). The
universe ended each daily-radar run without T-0 prices,
`ml backfill-features` could only advance to T-1, and
`ml_predictions.prediction_date` slipped from T-1 to T-2. Fix:
`ingestion/ingest_stooq.py:_tickers_needing_prices` now requires
`trade_date = today` exactly (matches the `/q/l/` endpoint's actual
data semantic). Pinned by 4 regression tests including an
orchestration-shape integration test. See "Recently resolved" below
and ADR-030.

**KI-143** opened + resolved 2026-05-14 — the equity orchestrator's
universe sort `ORDER BY universe_tier, ticker` combined with
`max_symbols=520` displaced 99 ML-universe primary-tier tickers
(ORCL, WMT, XOM, PLTR, UNH, …) every nightly run because `'extended'`
sorts alphabetically before `'primary'`, and the 174 extended-tier
rows populated 2026-05-01/02 consumed the first 174 slots of the cap.
ML feature coverage dropped 411 → 312 on 2026-05-04 and stayed there
two weeks (caught by the per-pipeline equity monitor's T-1 vs T-2
flag). Fix: add `DESC` to both call sites (`ingestion/orchestrator.py`
+ `pipelines/daily_radar.py`) so primary fills the cap first. Live
verification: 312 → 416 ML-universe tickers in the cap (full
recovery, +104 tickers including all the displaced names). Pinned
by 5 regression tests including duplication-pins for both call sites.
See "Recently resolved" below and ADR-031.

**KI-135** opened + resolved 2026-05-10 — crypto retrain
auto-promoted new models without validation; a regressed model could
have silently entered Phase E paper trading. Fix: validation gate in
`crypto/ml/validation_gate.py` blocks promotion when new hit rate <
0.9× old. See "Recently resolved" below.

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

**2026-05-14 update (related fix landed).** ADR-031 / KI-143
addressed the *displacement* side-effect of the 174 stale extended
rows: under the old `ORDER BY universe_tier, ticker` they were
sorted before primary and consumed the first 174 slots of the
`max_symbols=520` cap, displacing 99 ML-universe primary tickers.
The sort change (added `DESC`) puts primary first so the 174 stale
rows no longer cost ML-universe coverage. KI-122 itself remains
open: the reconciliation that should mark those 174 rows
`is_active=false` is still not implemented; this just removes the
production-impact urgency. KI-145 (`max_symbols` cap removal,
post-grouped-daily) is a complementary follow-up that would moot
the displacement entirely.


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

### KI-132 — Streamlit dashboard not auto-restarted after dashboard merges

**Symptom.** Dashboard merges that change Python imports leave streamlit serving stale code (because Python module cache keeps old objects). Manual restart required. Today's KI-130 fix went unnoticed for ~30 min after merge until user saw ImportError.

**Detection / fix path.** Add post-merge hook OR auto-restart when key dashboard files change OR rely on mhde-monitor-streamlit-freshness alerting (currently broken - see KI-133).

### KI-134 — Operator missed 8 streamlit-freshness alerts during today's weekend noise

**Symptom.** Streamlit-freshness monitor fired hourly from 12:35 to 19:35 UTC about stale dashboard code. Telegram delivery confirmed working post-incident. Operator did not act on alerts until dashboard threw ImportError, ~7 hours of dashboard drift.

**Root cause.** Signal-to-noise was already low due to weekend false-positives (now fixed in KI-128). 8 legitimate critical alerts were lost in the noise.

**Mitigation already applied.** KI-128 fixes weekend false positives, restoring signal quality.

**Future improvements (not implementing now).**
- Severity-based alert routing (CRITICAL gets a different sound/channel)
- Alert deduplication (don't repeat same alert hourly - escalate after 3 misses)
- Acknowledgment requirement (operator must explicitly clear alert)

These are real Phase E observation tasks, not pre-deployment requirements.

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

### KI-136 — Paper-trading drift monitor: P&L-band / drawdown / monthly arms deferred ("Gap 2.5")

**Context.** The Gap 2 paper-trading drift monitor
(`monitoring/paper_trading_drift.py`, ADR-020) ships with checks A
(engine liveness), B (stuck positions), C (closed-trade win rate — but
see below) and D (label hit rate). Three planned arms were
intentionally **not** built, and a fourth (C) ships but cannot compute:

- **Realised-P&L band** — rolling-30-day realised P&L vs
  `active_spec.json.backtest_expectations` (±20% `divergence_alert_threshold_pct`).
- **Drawdown breach** — realised account drawdown vs `portfolio_max_dd_pct`.
- **Monthly portfolio return** — rolling-21-trading-day return vs the
  walkfold monthly band (~+27% median).
- **Closed-trade win rate (Check C) — ships but currently uncomputable.**
  Computing post-cost win rate needs the exit fill price. The engine
  records market exits with `orders.price = NULL` (a market order has no
  limit price) and the exit `order_filled` event payload carries only
  `{qty, note}` — no price. So there is no readable exit price today;
  Check C counts such trades under `closed_trade_no_exit_price` and
  reports "uncomputable" (informational, not an alert). It activates
  automatically once the engine persists a readable realized exit price /
  P&L. (The engine's own event note — "realized_pnl_usd_approx includes
  funding per FUNDING-001" — implies that value is computed but not yet
  persisted in a place a reader can see; likely lands in `daily_pnl` once
  reconcile runs, or a future `trades` table.)

**Why deferred.** The P&L-band, drawdown and monthly arms need the
engine's `daily_pnl` table, which is **empty**: the engine's
`trading-engine-reconcile.timer` (which populates `daily_pnl`) is
disabled on the VPS pending the engine-side RECONCILE-001 fix. Building
those arms now would mean shipping inert code with "no P&L snapshots
yet" placeholders — noise, not signal. Check C's blocker is the same
family (no readable realized exit P&L) — likely resolved by the same
engine change.

**Resolution path.** Once RECONCILE-001 is resolved and `daily_pnl`
starts accumulating (and/or the engine persists a readable per-trade
realized exit price), add the three P&L arms as a follow-up increment
on `monitoring/paper_trading_drift.py` (checks A–D already establish the
module structure, the sample-gating pattern, and the `MonitorResult`
aggregation — the new arms slot in alongside); Check C starts producing
a real rate with no code change. No new schema or cross-repo
coordination needed; just read the new data read-only under ADR-020.

**Not blocking.** Checks A, B and D deliver real signal from real data
today (A from the first cycle, D once positions age past the 10-day
label-settlement horizon); the deferred arms and Check C's activation
add coverage, not correctness.

**Update (2026-05-11) — engine now persists exit price / realized P&L
(engine-side EXIT-PRICE-001 + reconcile-side backfill).** The
crypto-trading-engine `positions` table gained `exit_price` and
`realized_pnl_usd`: `place_exit` / the exit-fill handler write the SELL
weighted-average price and `(exit_price − entry_price)·qty`, and the
reconcile cycle backfills both from Binance for pre-fix closes. Two
read-side consequences in this repo:

- The dashboard's "Recent closed positions" table
  (`dashboard/services/queries.py:get_paper_closed_trades`) now shows the
  real `exit_price` (verbatim) and `realized_pnl` (rounded to cents) read
  straight from those columns. `"uncomputable (KI-136)"` now appears
  **only** when a column is genuinely NULL — pre-EXIT-PRICE-001 closes the
  reconcile backfill hasn't healed yet, and reconcile auto-closes of
  `engine_only_position` rows (no real SELL fill, so no recoverable price).
  Caption in `dashboard/app.py` updated accordingly.
- Check C still computes the win rate from the SELL `orders.price` join,
  which `place_exit` now records and the reconcile backfill repairs — so
  it activates automatically as priced closes accumulate (still
  sample-gated at 20). No code change to `monitoring/paper_trading_drift.py`;
  switching it to read `positions.realized_pnl_usd` directly is an optional
  future simplification, not required.

The remaining KI-136 scope (P&L-band / drawdown / monthly drift arms) is
unchanged — still blocked on the engine's `daily_pnl` accumulating, which
needs the VPS redeploy + the `trading-engine-reconcile.timer` re-enable.

### KI-137 — Crypto model re-emits buy signals immediately post-parabolic-crash (model-label root cause; mitigated, not yet fixed at source)

**Symptom.** The crypto prediction model fires high-conviction buy
signals on coins right after a parabolic blow-off top. Documented case:
SKYAIUSDT ran from ~$0.12 (Apr 12) to a $0.86 peak (May 5–6) then
roughly halved; the model emitted probabilities 0.72–0.88 on it across
the crash window — confirmed on *clean* data (so not the OHLCV-corruption
artifact; that was a separate, now-fixed issue). The historical scan
finds the broader pattern: predictions on coins that are >20% below
their 90-day high while still up >200% on 60 days carry ~2× the realised
max-drawdown of the rest (−25% vs −4%).

**Root cause.** In the model, not the data. The `label_Nd_10pct` target
("did the price tag +10% above today's close within N days") rewards
volatility regardless of direction — a freshly-crashed high-vol coin
genuinely *does* tag +10% intraday at some point — and the momentum-lag
features (`return_60d`, `drawdown_from_90d_high`) keep reading bullish
for weeks after a top. So the probability is "honest" w.r.t. its
objective; the objective is just the wrong one for a risk-aware entry
signal.

**Mitigation (shipped, two rounds).** Post-parabolic exclusion filter
(`crypto/ml/postparabolic_filter.py`):
- **v1 (ADR-021, branch `feat-crypto-postparabolic-filter`, 2026-05-11)** —
  Rule A: drop a coin iff `drawdown_from_90d_high < -0.20` **and**
  `return_60d > 2.0`. Targets the SKYAI-class (still-parabolic +
  just-starting-to-crash).
- **v2 (ADR-028, branch `feat-postparabolic-add-ret5-filter`, 2026-05-14)** —
  added Rule B: drop a coin iff `return_5d < -0.30`, OR-combined with
  Rule A. Targets the SWARMSUSDT-class (acute short-window weakness).
  Backtest: Sharpe +0.18, max DD unchanged, cumRet -2% relative; 30% of
  deep losses fit the class.

In both rounds the raw signal stays in `crypto_ml_predictions`; exclusions
are recorded in `crypto_signal_exclusions` and logged. The filter
suppresses the *symptom* before order entry; it does not change the model.

**Open follow-up (the real fix).** A direction-aware / risk-adjusted
crypto label (e.g. forward return net of forward max-drawdown, or a
"closes higher in N days" target) so the model stops mistaking
volatility for opportunity. Until then KI-137 stays open: the filter is
a guard rail, not a cure. Threshold retuning, if any of the three
constants in `crypto/config.py` proves too narrow or too wide, is a
one-line change (the historical scan supports −0.15/+1.5 as a
more-aggressive alternative for Rule A). The related-but-distinct
4USDT-class pattern is closed at KI-140 (Recently Resolved
2026-05-14); the 47% "Other"-class residual is at KI-151.

### KI-141 — `crypto_ml_predictions` carries no run-time stamp; pipeline_execution can only detect a 2-day outage, not a 1-day miss

**Symptom (the false-positive that surfaced this).** Until ADR-029,
`monitoring/pipeline_execution.py` set `RECENCY_BUDGET['crypto'] =
27h`, calling it "24h cycle + 3h grace". But the column the monitor
reads is `prediction_date`, which `crypto/ml/predict.py:score_universe`
sets to `MAX(trade_date) FROM crypto_ml_features` — the last completed
features day, T-1 calendar. Immediately after a healthy 00:30 UTC fire
the age was already ~24h 30m; from ~03:30 UTC every day onward the
monitor false-fired through the rest of the UTC day, regardless of
pipeline health.

**Fix shipped (ADR-029).** Threshold raised to 51h (`timedelta(days=2,
hours=3)`). Catches a real two-day outage at ~03:30 UTC on day-2 of
the outage, well inside the operator's response window. Tested by
`tests/regression/test_pipeline_execution_crypto_t1.py`.

**Open follow-up.** Add a run-time stamp column to
`crypto_ml_predictions` so the monitor can detect a one-day miss.
Concrete shape:

  1. **Schema.** `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP` on
     `crypto_ml_predictions` (mirrors `ml_model_runs.created_at`).
     Idempotent migration via `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
     inside `create_all_tables`. Equity `ml_predictions` benefits from
     the same treatment under ADR-015 — out of scope for this KI, but
     worth bundling if the same operator is making the change.
  2. **Writer.** `crypto/ml/predict.py:score_universe` is already an
     INSERT path; default would populate on each insert. The
     existing `DELETE FROM crypto_ml_predictions WHERE
     prediction_date = ?` upsert idiom keeps re-runs correct.
  3. **Monitor.** Swap the recency check to
     `MAX(created_at)` filtered by active model_ids; tighten budget
     to 24h + grace (or hourly + grace if a future job re-runs the
     scoring intra-day). The row-count check stays on
     `prediction_date` because that is still the unit of "today's
     scored universe".
  4. **Back-fill.** Historical rows (pre-migration) get `NULL`
     `created_at`. Two options: back-fill from
     `crypto_ml_model_runs.created_at` joined on `model_id` (lower
     bound), or leave NULL and have the monitor query
     `MAX(created_at) WHERE created_at IS NOT NULL`. The latter is
     simpler — the monitor only needs the *latest* run, not history.

**Why deferred.** ADR-029 fixed the operational pain (continuous
false-firing) with a one-line change. The schema-level fix is a
multi-file change that touches a writer on the live daily path; doing
it under the same banner as the false-positive fix would have inflated
blast radius and made revert non-trivial. Open as a separate workstream
so a future session takes it deliberately.

**Status.** Open. Priority: medium — pick up when the next operator
incident demonstrates that a one-day-miss matters in practice, or
bundle with the equity `ml_predictions.created_at` change under a
joint ADR.

### KI-139 — Pipeline monitor v1 limitations (no auto-remediation, coarse equity dashboard check, no "why 0 positions", reconcile timer unchecked)

**Context.** The per-pipeline monitor (`monitoring/pipeline_monitor/`,
`feat-pipeline-monitoring`, ADR-026) ships with deliberate v1 scope cuts —
none is a bug, all are tracked so a later session tightens them on purpose.

1. **No auto-remediation.** The monitor reports 🟢/🔴/⚪ to Telegram; the
   operator acts. (By design — re-running a pipeline step from a monitor is
   the kind of thing that should be an explicit decision.)
2. **No dashboard view.** Telegram only in v1. A "pipeline status" dashboard
   tab is a possible v2.
3. **Equity "dashboard data refresh" step is coarse.** It checks the mtime of
   one daily-analysis output file (`data/processed/prediction_vs_actual_rows.csv`)
   with a 4-day tolerance — wide enough to absorb a Friday→Tuesday weekend plus
   a market holiday (the 23:15 daily-analysis path runs Mon-Fri only). A
   3-day-stale dashboard on a normal week is therefore *not* flagged by this
   step alone. A multi-day outage is still caught by it and by the existing
   health-check / `pipeline-execution` monitor; tightening would need a
   trading-calendar-aware expected-mtime instead of a flat day count.
4. **"0 positions opened today" → 🔴 with a note, not a precise diagnosis.**
   The engine DuckDB carries no machine-readable "why did entry place 0" field
   (no `entry_complete` event with a reason), so the crypto monitor's step 9
   reports 🔴 with a note telling the operator to check the engine entry log
   (all top-N filtered? predictions file rejected? max_concurrent reached?).
   It is softened to 🟢 only when the book is already at `max_concurrent`
   (read from `active_spec.json`). A clean fix needs an engine-side change to
   record the entry outcome reason — out of scope (don't modify the engine
   repo) and tracked alongside the engine-data-recording follow-ups.
5. **Engine `reconcile` timer not checked.** The continuous monitor checks the
   engine `monitor` and `entry` timers but not `reconcile` — that timer is
   disabled pending RECONCILE-001, and a permanently-red check would be noise.
   A `CHECK_ENGINE_RECONCILE` flag in `continuous_runner.py` flips it on once
   RECONCILE-001 lands.

**Status.** Open observation, low priority. Resolve item-by-item as the
underlying constraints (trading-calendar helper, engine entry-reason recording,
RECONCILE-001) are addressed.

### KI-144 — Universe-tier sort SQL is duplicated in two files; extract to a shared helper

**Symptom.** The same SELECT — `SELECT ticker FROM companies WHERE
is_active = true ORDER BY universe_tier DESC, ticker` — appears
byte-identically in `ingestion/orchestrator.py:75` and
`pipelines/daily_radar.py:77`. Both apply the same `max_symbols`
cap. Originally the duplication wasn't load-bearing, but the
ADR-031 / KI-143 fix had to be applied in lock-step to both files
because daily-radar passes its result as `tickers_override` and
short-circuits the orchestrator's own SELECT at runtime. A future
change to the universe-selection contract that touches only one
file will silently drift the two paths.

**Detection / fix path.** Extract a single helper, e.g.
`universe/active_tickers.py:get_active_tickers_for_ingest(conn,
max_symbols=None) -> list[str]`. Replace both call sites. Add a
test asserting both call sites import the helper and that the
helper's tier-aware behaviour matches the existing duplication-pin
tests in `tests/equity/test_orchestrator_universe_sort.py`. Once
the helper lands, the duplication-pins can either be retired or
refactored to assert "no SELECT in either file uses the universe
table directly."

**Out of scope for the ADR-031 / KI-143 universe-drop fix.** The
duplication was the pattern that let the bug slip past code review
in the first place; this KI is the durable structural fix. ADR-031
explicitly defers the refactor so the immediate one-character ML-
universe recovery could ship without bundling.

### KI-145 — `max_symbols=520` ingest cap is a pre-grouped-daily artifact and no longer rate-limits anything

**Symptom (no current production impact, post-KI-143).** The
`universe.max_symbols` config (default 520 in production) was
introduced when `ingestion/ingest_prices.py` looped per-ticker
against Polygon's rate-limited per-ticker endpoint
(`/v2/aggs/ticker/.../range/...`) and 504 universe tickers took
~50 minutes to ingest under the free-tier ~5 req/min budget. After
the 2026-05-09 grouped-daily switch (commit `473b92a`, ADR pending)
Polygon serves the entire ~12k US stock list in one HTTP call
regardless of universe size. The cap therefore no longer
rate-limits anything and only exists to keep Stooq / Yahoo per-
ticker calls bounded — both of which are themselves bounded by
their own ingestor logic (`_BATCH_SIZE=50` + `_REQUEST_DELAY=0.15s`
in Stooq; per-ticker calls in Yahoo are fallback-only).

**Why open it now.** ADR-031 / KI-143 made the ML-universe
recovery work *under* the cap by changing sort order, but the cap
itself is structurally vestigial. Removing it would (a) auto-extend
ML coverage to any future S&P additions without intervention,
(b) eliminate the displacement risk if the extended tier ever
exceeds 16 rows, and (c) close the architectural debt that made
KI-143 possible.

**Detection / fix path.** Remove `universe.max_symbols` from the
default config; remove the corresponding `[:max_symbols]` slice in
both `ingestion/orchestrator.py` and `pipelines/daily_radar.py`
(or refactor to the helper from KI-144 first and remove from one
place). Audit downstream consumers that may have assumed
`len(tickers) ≤ 520` (most likely none — none have been
identified). Update `pipelines/daily_radar.py:83` log line to drop
the "Dev mode" framing.

**Out of scope for ADR-031.** Larger blast radius; needs a separate
investigation pass to confirm no consumer depends on the cap. KI-143
is the single-day fix; KI-145 is the structural cleanup.

### KI-146 — Extend walkfold validation script to compute per-fold portfolio max DD

**Symptom (methodology gap, not a production bug).** ADR-032 now
requires two gates for any spec change that affects portfolio
drawdown: walkfold per-fold dominance ≥ 4/6 AND portfolio-simulator
full-window `max_dd ≤ -25%` (5pp under the live -30% kill switch).
Today the two gates live in separate scripts:

- `.claude/local_scripts/backtest_trail_walkfold.py` runs the
  per-fold sweep and reports per-fold Sharpe + raw-P&L "Max DD" (the
  capital-naive, sum-of-per-trade-fractions metric).
- `simulate_portfolio` (`crypto/execution/backtest/report.py`) runs
  full-window over a single trade stream and produces the capital-
  constrained `portfolio_max_dd_pct` the spec's
  `backtest_expectations` consume.

The walkfold script does not call `simulate_portfolio` per fold.
A candidate parameter can therefore pass gate (1) and still fail
gate (2) at the full-window simulation step — exactly the trail_pct=0.10
case ADR-032 immortalises. Worse, the failure mode might be regime-
specific: a fold-2-shaped drawdown can be invisible in the full-
window simulation if a benign late-window regime drowns it out.
Computing portfolio max_dd *per fold* would surface the
capital-constrained metric at walkfold granularity and catch the
envelope problem before the ship workstream starts.

**Detection / fix path.** Extend
`.claude/local_scripts/backtest_trail_walkfold.py` to, for each
(fold × parameter-value) cell, additionally call
`simulate_portfolio(starting_capital=1000, max_positions=6,
deploy_fraction=0.8, leverage=1.0)` on that cell's persisted trades
(reading from `crypto_backtest_trades`) and emit two new tables in
the output markdown:

1. *Per-fold portfolio Sharpe* — analogous to the existing per-fold
   harness Sharpe table, but computed from the capital-constrained
   simulator.
2. *Per-fold portfolio max DD* — analogous to the existing per-fold
   raw-P&L Max DD table, but computed from the simulator. This is
   the metric that should be compared against the -25% safety floor
   per fold, not the raw-P&L Max DD.

Keep the existing raw-P&L Max DD column for continuity (it answers a
different question: signal-and-exit fit). Add a small section at the
top of the report explaining the difference so a future reader does
not conflate the two metrics — same mistake the trail_pct=0.10
workstream nearly shipped.

**Effort: low/medium.** The simulator is already a pure function over
a trade list; the wiring change is per-fold trade-list construction
+ two extra DataFrame columns. No new persistence. Worth doing
before the next Policy-D-axis sweep so the methodology check happens
inside one script instead of across two.

**References.** ADR-032 (the binding gate definition).

### KI-147 — Document the two-gate pre-ship checklist in OPERATIONS.md

**Symptom (operational gap, not a production bug).** ADR-032
defines the validation gates that any spec change affecting
portfolio drawdown must pass, but the operational procedure — the
*sequence of steps* an operator or agent should run before
regenerating `active_spec.json` and repointing the active prediction
path — is not yet codified anywhere a runbook reader would find it.
The ADR is the binding rationale; the runbook is the checklist.
Today the only place the two-gate pattern is written down is in
`DECISIONS.md` and the two `data/processed/trail_pct_*` research
markdowns, none of which a fresh operator opens during a ship
workstream.

**Detection / fix path.** Add a "Spec-change procedure" subsection
to `OPERATIONS.md` (or extend the existing one if present) covering
parameter changes on the axes ADR-032 binds — `trail_pct`,
`activation_pct`, `top_n`, `max_concurrent`, `deploy_fraction`, and
any new position-sizing logic. Checklist shape:

1. *Walkfold gate.* Run the walkfold validation script (per
   KI-146, ideally extended to compute per-fold portfolio max DD).
   Confirm per-fold dominance over the deployed baseline in ≥ 4 of
   6 folds.
2. *Portfolio-simulator gate.* Persist a full-window backtest at
   the candidate value; run `simulate_portfolio(starting_capital=1000,
   max_positions=6, deploy_fraction=0.8, leverage=1.0)` with
   post-parabolic filter ON; confirm `max_dd ≤ -25%` AND
   `sharpe_ratio ≥ baseline_sharpe + 0.10`.
3. *If either gate fails:* HOLD the deployed value, write a research
   markdown documenting the result (template:
   `data/processed/trail_pct_constrained_sweep.md`), and do not
   regenerate the spec.
4. *Only after both gates pass:* repoint `PHASE1B_WINNER_RUN_ID`,
   regenerate `data/exports/active_spec.json`, and verify the spec's
   `backtest_expectations.portfolio_max_dd_pct` matches the gate (2)
   number (catches the filter-ON/filter-OFF inconsistency noted at
   the bottom of `trail_pct_constrained_sweep.md`).

Cross-link from `OPERATIONS.md` → ADR-032 so the rationale is one
hop away. Mention this KI in the existing pre-ship section of
`docs/PATH_TO_LIVE_PLAN.md` if there is one, so Phase-2 operators
inherit the checklist.

**Effort: low.** Pure documentation; no code change. Worth doing
soon so the methodology binds even when the operator running the
ship workstream is not the one who participated in the trail_pct=0.10
case.

**References.** ADR-032 (gate definitions); [[KI-146]] (the script
extension that makes gate (1) cheap to run); the
`trail_pct_constrained_sweep.md` research markdown (canonical
worked example of the two-gate decision).

### KI-148 — Deployed spec's `portfolio_max_dd_pct` understates real deployment drawdown by ~8pp (filter-ON envelope exceeds kill switch)

**Severity: medium — not currently active, but real risk in an
F2-like regime.** Discovered 2026-05-14 during the
`feat-trail-pct-tighten-to-0.10` constrained-sweep workstream.

**Symptom.** The deployed crypto strategy spec
(`data/exports/active_spec.json`) carries
`backtest_expectations.portfolio_max_dd_pct = -23.7%`, sourced from
`PHASE1B_WINNER_RUN_ID = "backtest_10d_D_top_n_a02e15a0"`. That row
in `crypto_backtest_runs` has no `apply_postparabolic_filter` field
in its stored `params` — i.e. the backtest was run with the
post-parabolic filter **OFF**. Live execution
(`crypto/exports/write_daily_predictions.py`) applies the
post-parabolic filter to every prediction batch (per ADR-021 +
ADR-028, the filter is part of production behaviour). A filter-ON
re-run of the same trail=0.30 config — persisted during the
constrained sweep as `backtest_10d_D_top_n_f74ee424` — reports
`portfolio_max_dd = -31.9%`.

The deployed spec therefore states a -23.7% drawdown envelope while
the trade stream the engine actually produces has a -31.9% envelope.
That -31.9% **already exceeds the live `risk.max_account_drawdown_pct
= 0.30` kill switch** by ~1.9 percentage points. The kill switch has
not tripped only because we have not been through an F2-shaped regime
(2025-06-05 → 2025-08-04, the window that drove this drawdown) under
filter-ON production yet — the filter-ON config has been live for
weeks, but the regime that exercises its tail has not recurred.

The two numbers diverge by ~8pp: -23.7% (filter-OFF, stated) vs
-31.9% (filter-ON, real). The spec's published expectations are
therefore self-inconsistent with the engine the spec configures.

**Implications.**

1. *Spec expectations diverge from real production envelope.* Any
   drift-monitor that compares observed drawdown against the spec's
   `portfolio_max_dd_pct` will flag normal filter-ON behaviour as
   anomalous once the realised DD exceeds the -23.7% stated number.
   The existing divergence-alert threshold of 20% (per
   `trail_pct_constrained_sweep.md`) is wide enough to absorb this
   today, but the underlying inconsistency will surface as alerts
   mature.
2. *Kill-switch trip on F2 recurrence.* In a regime resembling
   Jun-Jul 2025, the engine running trail=0.30 with filter-ON will
   draw down past -30% and trip its own
   `risk.max_account_drawdown_pct` guard. The operator recovery
   protocol on a kill-switch trip is not currently exercised in
   production (no historical trip event).
3. *Kill switch likely calibrated against the wrong envelope.* The
   -30% threshold was set when the spec's stated DD was -23.7%
   (filter-OFF). It was probably chosen as "stated envelope + a
   margin." If the real envelope is -31.9%, the margin is negative
   — the switch trips on the strategy's *normal* tail, not on
   anomalous behaviour. Recalibration is its own analysis: what is
   the switch defending against, what failure modes need the trip,
   what is the operator recovery procedure.

**Resolution paths (none chosen yet; all imperfect).**

1. *Re-point `PHASE1B_WINNER_RUN_ID` to the filter-ON 0.30 baseline
   (`backtest_10d_D_top_n_f74ee424`).* The spec then states what the
   engine actually does. Truthful expectations; spec-vs-reality
   divergence resolved at source. Does **not** fix the underlying
   gap — the kill switch is still violated by the strategy's stated
   envelope. Only stops the alert-noise problem from mismatched
   expectations. Low-cost; one config line + spec regeneration.
2. *Recalibrate the kill-switch envelope itself.* The right number
   for `risk.max_account_drawdown_pct` depends on what failure modes
   the switch defends against (catastrophic strategy decay vs
   normal-regime tails), what's recoverable vs unrecoverable, and
   what the operator's intervention protocol looks like when it
   trips. Multi-step investigation; touches engine-repo `risk.yaml`
   and operational runbook. Largest blast radius but the only path
   that produces a kill switch matched to the actual deployed
   strategy.
3. *Accept the divergence-alert discrepancy.* Status quo. The 20%
   divergence-alert threshold absorbs the gap for now; operators
   manually triage any DD-related alerts against both the stated
   spec and the filter-ON baseline. Operational cost from recurring
   alert noise as the alert threshold tightens. Zero code change.

**Out of scope for ADR-032 and the trail_pct workstream.** ADR-032
binds the validation methodology for future spec changes; it does
not retroactively fix the deployed spec or the kill switch
calibration. The trail_pct workstream documented the gap and reverted
to deployed; this KI is the durable tracker for resolution.

**References.**

- ADR-032 (validation methodology that surfaced this gap).
- `data/processed/trail_pct_constrained_sweep.md` — final section
  "Adjacent finding: filter-ON vs filter-OFF inconsistency" is the
  canonical write-up.
- `crypto_backtest_runs` row `backtest_10d_D_top_n_f74ee424` —
  filter-ON 0.30 baseline; the evidence backing the -31.9% number.
- `crypto_backtest_runs` row `backtest_10d_D_top_n_a02e15a0` —
  filter-OFF 0.30 baseline; the currently-deployed
  `PHASE1B_WINNER_RUN_ID`.
- `crypto/exports/write_daily_predictions.py` — confirms live
  execution applies the post-parabolic filter.
- `data/exports/active_spec.json` — `risk.max_account_drawdown_pct
  = 0.30` and `backtest_expectations.portfolio_max_dd_pct = -23.7%`.

### KI-149 — Equity ML pipeline silently ships T-2 predictions every weekday — layered defect chain

**Severity: high — recurring every weekday since at least 2026-05-12,
not transient.** Filed 2026-05-14 from the same investigation that
produced `data/processed/finding3_ml_pipeline_gap_root_cause.md`.

**Symptom.** The equity ML pipeline reports "Freshness OK" and "OK"
exit code every daily run, but the predictions it writes carry a
`prediction_date` two trading days behind today rather than T-1. On
2026-05-13 00:20 the predict service logged
`Freshness OK: Equity prices_daily latest=2026-05-12 (1 trading-day
gap; threshold=2)` followed immediately by
`Scoring universe for 2026-05-11`; on 2026-05-14 00:20 it logged
`latest=2026-05-13` followed by `Scoring universe for 2026-05-12`. In
both cases the "latest" prices_daily row count for the named date was
**4** (vs ~520 normal) — fallback-source noise — but the pipeline
treated `MAX(trade_date)` as sufficient and proceeded to score the
previous trading day. `ml_predictions` for 2026-05-13 does not exist;
the May 14 run rewrote `2026-05-12` predictions (idempotent UPSERT)
and exited 0. `trading-engine-entry.timer` at 00:45 then consumed
predictions whose `prediction_date` is two trading days stale relative
to the live market.

**Root cause is layered, not single-point.** Three defects compound:

1. *External: Polygon free-tier blocks current-day grouped endpoint.*
   `adapters/polygon.py` documents recent_daily_prices as "Last 5
   days OHLCV available on free tier", but the `grouped` endpoint for
   `date == today` (and often T-1) returns HTTP 403. The
   `daily_analysis_2026-05-13.log` records
   `Polygon grouped 2026-05-13: HTTP 403` and at T+1 still
   `2026-05-13: {'grouped_status': 403, 'in_universe': 0,
   'fallback_inserted': 0}`. Free-tier policy unlocks the date only
   at ~T+2. Treated by the orchestrator as a generic `WARNING` rolled
   into "1 failed" out of 11 sources.
2. *Freshness check is row-count-blind.* `pipelines/freshness.py:67`
   computes `SELECT MAX(trade_date) FROM prices_daily` and compares
   `trading_days_between(latest, today) <= 2`. Four rows of
   fallback-OTC data for 2026-05-13 (`HTHIY`, `IFNNY`, `RSHGY`,
   `SUNB`) raise `MAX(trade_date)` to 2026-05-13, the trading_gap
   becomes 1, and the check passes despite ~99% missing universe
   coverage for that date.
3. *Scoring-date selector cross-checks nothing.* `ml/predict.py:93-95`
   reads `SELECT MAX(trade_date) FROM ml_features` as the default
   prediction_date. `ml_features` for 2026-05-13 has zero rows (the
   feature compute saw no in-universe prices_daily rows for that
   date), so the selector returns 2026-05-12. The pipeline never
   compares `MAX(ml_features.trade_date)` against
   `MAX(prices_daily.trade_date)`; the only signal of the
   regression is an `INFO Scoring universe for 2026-05-12` log line
   in `data/logs/equity_predict.log`, which no alerting consumer
   reads.

Without an alerting layer to surface the divergence, the gap recurs
every weekday silently. The would-be alerting layer
(`mhde-equity-pipeline-monitor.service`) is itself broken — see
[[KI-150]].

**Architectural direction (committed; not for re-debate as part of
this KI).** The equity workstream's pause-state doc
`docs/EQUITY_WORKSTREAM_PAUSED.md` records the decision: **T-2
honest**. The free-tier Polygon constraint is accepted; the system
should be made truthful about its T-2 cadence rather than upgraded to
paid Polygon (~$29–79/mo) at this stage. Paper trading does not
require T-0 freshness; the gap is in honest labelling and silent
skipping, not in data-source budget. Live execution would re-open the
question.

**Resolution paths.**

1. *Tighten `pipelines/freshness.py:67` to require coverage, not just
   `MAX(trade_date)`.* Replace the bare MAX with a query that selects
   the most recent date whose row count is at least some fraction
   (e.g. 0.5) of the ML universe size. Four-row "today" coverage will
   then fail freshness and `ml predict` will return
   `{"skipped": "stale_data"}` — visible upstream and alertable. Low
   blast radius; mirrors the row-count guard already used in the
   pipeline-execution monitor (`monitor pipeline-execution`).
2. *Cross-check predict scoring-date in `ml/predict.py:93-95`.* If
   `MAX(ml_features.trade_date) < MAX(prices_daily.trade_date)`,
   log `WARNING` (or refuse to score, behind a flag). Either signals
   the divergence in `data/logs/equity_predict.log` so external
   monitors can flag it. Compatible with the T-2 honest decision —
   the warning labels the prediction as stale, doesn't change which
   date is scored.
3. *Make the Polygon adapter explicit about the free-tier delay.* If
   `grouped_status=403` and `in_universe=0`, the orchestrator should
   classify the affected date as missing rather than rolling it into
   the generic warning bucket. Pairs with (1) by making the upstream
   signal less ambiguous.
4. *Operator-facing labelling.* Once the silent skip is fixed,
   downstream consumers (dashboard, trading-engine-entry input
   contract, monthly P&L reports) should advertise the T-2 cadence
   explicitly. Pause-state doc step "Update dashboard to advertise
   T-2 honestly".

The smallest fix that closes the silent-skip is (1) + (2). The
dashboard / labelling change is a separate workstream tracked in the
pause doc, downstream of this KI shipping.

**References.**

- `data/processed/finding3_ml_pipeline_gap_root_cause.md` — full
  investigation with smoking-gun evidence, log excerpts, and the
  three-defect breakdown.
- `pipelines/freshness.py:67` — `MAX(trade_date)`-only freshness
  check.
- `ml/predict.py:93-95` — silent scoring-date fallback.
- `adapters/polygon.py` — current 403-as-WARNING handling.
- `data/logs/equity_predict.log` — May 13 (line ~631) and May 14
  (~731) "Scoring universe for [T-2]" lines.
- `data/logs/daily_analysis_2026-05-13.log` — Polygon 403 on
  current-day grouped.
- [[KI-150]] — broken monitor that would have caught this gap on day
  one.
- `docs/EQUITY_WORKSTREAM_PAUSED.md` — architectural direction
  (T-2 honest) and resumption queue placement (step 3).

### KI-151 — "Other"-class deep losers (47% of deep losers) characterized as "late-stage uptrend reversal" but not filterable

**Symptom.** After KI-140's exclusion-rule closure, 47% of deep losers
(44 of 93 in the 941-trade Phase-1B backtest, mean final P&L −24.59%)
remain unattributed to either the SWARMSUSDT-class (ADR-028 addresses)
or the 4USDT-class (KI-140 closes). They share a coherent entry-time
profile but no filter rule on the available features can isolate them
without unacceptable winner-rejection cost.

**Profile (extended uptrend, overbought, calm BTC vol).** Vs the 776
winners in the same window, all KS p < 1e-4 unless noted:

- `drawdown_from_90d_high`: Other **−17.9%** vs winners **−39.6%**
  (mean shift +1.08 SD) — Other are *near peak*, not pulled back.
- `rsi_14d`: Other **59.0** vs winners **49.8** (shift +0.70 SD).
- `bollinger_position`: Other **+0.39** vs winners **+0.01** (shift
  +0.58 SD) — pressed against upper band.
- `price_vs_50d_ma`: Other **+25.7%** vs winners **+8.1%** (shift
  +0.33 SD).
- `return_60d`: Other **+53.1%** vs winners **+28.0%** (shift
  +0.18 SD).
- `funding_rate_zscore`: Other **+0.47** vs winners **−0.30** (shift
  +0.21 SD) — crowded long, expensive carry.
- `btc_vol_30d`: Other **0.34** vs winners **0.41** (shift −0.74 SD)
  — Other losses happen in *tame* tape, not crashes.

Synthesis: late-stage uptrend reversal. Coins that have been running,
are overbought, sit near peak, attract crowded longs, and the model
buys them just before a pullback in an otherwise calm market.

**Status: describable but not filterable.** A 5-fold-CV logistic
regression on all crypto_ml_features columns reaches **AUC 0.743** —
modestly informative but not concentrated enough for a precision
filter. The cohort base rate is 44 / 941 ≈ 4.7%; every economically
meaningful threshold filters 5–13 winners per Other-loser caught.
Economic analysis using BASELINE's empirical edges (winners +9.26%,
Other-losers −24.59% — a 2.7× P&L asymmetry):

| threshold | TP saved | FP foregone | NET |
|---:|---:|---:|---:|
| 0.50 | +664 pp | −1759 pp | **−1095 pp** |
| 0.70 | +393 pp |  −991 pp | **−598 pp** |
| 0.80 | +221 pp |  −574 pp | **−353 pp** |
| 0.90 | +123 pp |  −222 pp | **−99 pp** |
| 0.95 |  +49 pp |   −56 pp | **−7 pp** (N=2, noise) |

No threshold is Sharpe-positive — the 5-13× volume asymmetry overwhelms
the 2.7× P&L asymmetry at every operating point. This generalises the
ADR-028 Variant E rejection: the broader cohort exhibits the same
"filter would gut the winner distribution" pathology.

**Productive paths from here (none scoped).**

1. **Probability haircut (Path E)** — multiplicative downscale on
   `predicted_probability` for entries matching the Other-class
   feature shape, applied *before* Top-N selection. Avoids the
   binary-filter Top-N backfill problem that killed Variant E and the
   classifier-as-filter analysis above. Same lever proposed for KI-140
   (item 2 of its original hypothesised directions); KI-151
   strengthens the case for *the same fix* across both unaddressed
   classes.
2. **Direction-aware label** — the [[KI-137]] long-term root-cause
   fix: a label that penalises forward drawdown directly (long-only
   knockout from entry) would naturally down-rate all three named
   classes — SWARMSUSDT (still-falling = high downward forward
   probability), 4USDT-class (bounce-fade = high downward), Other
   (late-stage reversal = high downward) — at training time, without
   any exclusion-rule plumbing.

**References.**

- `data/processed/other_deep_loser_characterization.md` — full cohort
  characterization, clustering analysis, classifier results, threshold
  sweep, and economic analysis.
- `data/processed/rescue_rate_heatmap.md` — the parallel finding that
  no conditional-cut state shows Sharpe-positive EV either.
- [[KI-140]] — the related 4USDT-class closure that motivated this
  analysis as the residual workstream.
- [[KI-137]] — the underlying model-label root cause that any
  long-term fix routes through.
- `DECISIONS.md` ADR-028 — the SWARMSUSDT-class exclusion rule that
  addressed the third 30% of deep losers.

### KI-152 — Operational monitoring alerts drowning out trading signals — channel separation needed

**Symptom (alerting signal-to-noise observation, not a production
bug).** All MHDE alerts — operational monitoring AND trading signals —
flow through the same Telegram channel via the ATSRP Ops Bot. With 18
tracked observations and multiple monitoring layers active
(`streamlit_freshness`, `paper_trading_drift`, `pipeline_execution`,
`data_quality`, the per-pipeline checks under
`monitoring/pipeline_monitor/`, and so on), operational alerts
significantly outnumber trading signals (FX cycle exits, crypto/equity
predictions, position management). Time-sensitive trading signals get
lost in operational noise.

**Specific incident (2026-05-14).** The GBPEUR_C001 EXIT_TARGET alert
fired at 17:00 UTC (signal rate 1.1484, −1.00% from entry) and was
delivered to the operator's Telegram at 18:11 UTC. The operator did
not notice until manual review of the alert thread later in the
session, by which time the rate had already moved. Today's session
generated 10+ operational alerts in the same channel; the trading
signal was buried in monitoring chatter. This is the same operator-
attention failure mode as [[KI-134]] (operator missed 8
streamlit-freshness alerts under weekend false-positive noise) but
inverted: there the noise was *false-positive* monitoring; here the
noise is *true-positive but operationally routine* monitoring.

**Implications.**

- Missed trading signals = missed execution windows = missed return,
  in a regime where the strategy's edge is small per-trade and depends
  on operator response inside a ~hour-scale window.
- Alert fatigue reduces operator response quality to *all* alerts,
  including legitimate critical ones (e.g. the equity pipeline silent
  T-2 skip in [[KI-149]] / [[KI-150]] went undetected for an unknown
  number of weekdays partly because the alerting layer was itself
  failing silently — the symptoms are different but the operator-
  attention budget is the shared resource).
- Today's severity prefix (INFO / WARN / CRITICAL) and sha-based
  throttle (`cooldown_hours=4`, shipped today in `monitoring/alert.py`)
  reduce *frequency* of repeats but do not solve volume or
  signal-to-noise; a CRITICAL FX signal and an INFO streamlit-freshness
  hiccup still land in the same chat with the same notification
  weight.

**Resolution paths (none scoped).**

1. **Separate Telegram channels.** `trading-signals` (push,
   always-notify) vs `ops-alerts` (silent or daily digest). Cleanest
   separation; requires a new chat ID and routing config in
   `monitoring.yaml`.
2. **Severity-based routing within single channel.** Only CRITICAL
   pushes immediately; WARN/INFO accumulate into a daily digest.
   Lowest-effort but operator still has to disambiguate signal type
   from prefix.
3. **Trading-signals as a distinct alert category in
   `config/monitoring.yaml`** that bypasses the ops throttle and gets
   priority routing (e.g. its own channel ID, retry policy, no
   cooldown). Composable with paths 1 and 2.
4. **Separate bots.** ATSRP Signals Bot (trading) + ATSRP Ops Bot
   (operational) — distinct channels, distinct visual identity (icon,
   name). Highest operator-clarity; highest setup cost.

No path is selected. The operator-attention failure today is the
prompt; the design decision is deferred.

**Effort.** Medium (~1–2 days). Design the routing schema, implement
channel separation in the alert dispatcher, migrate existing alert
categories to the new routing keys, test that severity / category
filtering works end-to-end without breaking idempotency or throttle.

**References.**

- `monitoring/alert.py` — severity prefix + sha-based throttle (shipped
  today; reduces repeat frequency, not category mixing).
- `config/monitoring.yaml` — where channel routing would be configured.
- [[KI-134]] — the prior alert-quality observation (false-positive
  noise variant); same operator-attention failure mode.
- [[KI-149]], [[KI-150]] — the equity workstream's alerting-layer
  failure; complementary failure mode (alerts that should fire don't).
- ATSRP `research/gbpeur_personal_fx/ledger.json` — the 2026-05-14
  GBPEUR_A002 EXIT_TARGET alert whose burial in operational noise is
  the immediate prompt for this observation.

### KI-153 — Monitor-service exit codes conflate "reports RED" with "crashed"

**Severity: low–medium (operational clarity, not data correctness).**
Surfaced 2026-05-14 during the KI-150 Phase 1 investigation.

**Symptom.** `*-pipeline-monitor.service` units (the per-pipeline
"one Telegram message, every step green/red/skipped" monitors —
crypto, equity, fx) exit **1** by design when any monitored step is
red. `monitoring/pipeline_monitor/daily_runner.py:142`:

```python
return 0 if not result.has_red else 1
```

`systemctl status` reports this as
`Active: failed (Result: exit-code)`, which is **indistinguishable
from a real monitor crash** (import error, traceback, env var missing,
etc.). The initial KI-150 diagnosis confused the two — "the monitor
itself is broken" — and prescribed a fix for the equity-pipeline
monitor that wasn't actually broken; the monitor was correctly
flagging the underlying [[KI-149]] silent-T-2-skip defect.

**Implications.**

- Operator can't tell from `systemctl status` whether the *monitor*
  needs attention or the *system it monitors* needs attention.
- Future "broken monitor service" audits risk the same misdiagnosis.
- Delayed correct response in today's KI-150 triage (the equity-
  pipeline-monitor exit-1 was diagnosed as a service bug when it was
  the alert).

**Resolution paths (none chosen).**

1. Reserve exit 1 for "monitor reports red", use a different exit
   code (2 or 3) for actual monitor crashes / unexpected exceptions.
   `daily_runner.main` would `try/except` the Telegram + state path
   and return 2 on the exception branch.
2. Use `sd_notify` (or the `STATUS=` notify protocol) to communicate
   pipeline RED state separately from process exit, so systemd can
   distinguish "monitor process succeeded; pipeline state is red"
   from "monitor process failed".
3. Wrapper script around each monitor that catches the exit code,
   logs the disposition, and surfaces "red" via a different signal
   (e.g. a `.failed` marker file the operator can read).
4. Documentation only — accept the conflation, label
   `pipeline_monitor_*.log` with explicit "exit 1 = pipeline red"
   notes, document the convention in `OPERATIONS.md`. Cheapest, but
   doesn't solve the operator-mental-model problem.

**Effort.** Low–medium depending on path. Path 1 is ~30 min and is
the most operator-friendly. Path 2 requires a sd_notify dependency
and per-unit `Type=notify` change.

**Why not in scope for KI-150's branch.** The KI-150 branch fixes a
real data path (DuckDB write-lock contention bypass). This is a
separate operational-clarity defect; folding it in would broaden the
blast radius of a freshly-tested fix.

**References.**

- `monitoring/pipeline_monitor/daily_runner.py:142` — the by-design
  exit semantics.
- `systemd/mhde-equity-pipeline-monitor.service` — the unit whose
  `failed` status the initial KI-150 diagnosis misread.
- [[KI-150]] — the misdiagnosis that surfaced this observation.

## Recently resolved (post-Session-7)

### KI-150 — Equity monitoring services failing silently — resolved 2026-05-14 (parts 2 + 3 fixed; part 1 was misdiagnosis)

**Original symptom (kept for archival).** Three monitor services were
reported as emitting no alerts despite pipeline gaps:

1. `mhde-equity-pipeline-monitor.service` — exit-code **1** every
   daily fire.
2. `mhde-monitor-data-quality.service` — exit-code **0** but
   `data/logs/monitor_data_quality.log` showed repeating
   `_duckdb.IOException: Could not set lock on file …mhde.duckdb` +
   `alert: could not open MHDE DB — bypassing throttle`.
3. `mhde-monitor-pipeline.service` — exit-code **0** but same
   DuckDB-lock pattern.

**Resolution by branch `fix-ki150-monitor-services` (2026-05-14).**

*Part 1 — misdiagnosis.* `mhde-equity-pipeline-monitor.service` was
**not broken.** Per `monitoring/pipeline_monitor/daily_runner.py:142`,
the service returns 1 by design when any monitored step is red. The
RED condition it was flagging was the underlying [[KI-149]] silent
T-2 skip (feature pipeline lagging prices by 1 day), now fixed in
the `fix-ki149-honest-equity-freshness` branch. The "exit 1 ⇒ crash"
reading was the misdiagnosis. Local dry-run reproduces the
production output verbatim — the monitor runs, Telegram fires, exit
1. Once `ml_features` backfills for 2026-05-13/14 (out-of-scope
backfill task), the monitor returns 0 and `systemctl` goes green.
The systemic operator-clarity gap surfaced by this misreading is
filed as [[KI-153]].

*Parts 2 + 3 — DuckDB write-lock contention.* Both monitors' own
queries already used `read_only=True`
(`monitoring/data_quality.py:92`, `monitoring/pipeline_execution.py:192`).
The failing path was the shared throttle-state writer in
`monitoring/alert.py:_open_default_conn` (opened a *writable* DuckDB
conn to persist `monitor_alert_state` — contention with predict /
daily-analysis writers caused IOException, which `send_alert`
swallowed and bypassed throttle state). Fixed by moving throttle
state from the DuckDB `monitor_alert_state` table to a JSON sidecar
(`monitoring/alert_state_store.py`, fcntl.flock-serialized
read-modify-write at `data/monitor_alert_state.json`). `send_alert`
no longer opens a DuckDB connection; the alert path is now
DuckDB-write-lock-independent. 10 throttle tests refactored + 1
regression test added (`test_send_alert_persists_state_while_
duckdb_writer_lock_is_held` reproduces the original failure mode
under a real lock and verifies state persists + Telegram fires).

**Residue.** The DuckDB `monitor_alert_state` table is left in place
(migration v10) as harmless residue — no readers, no writers. A
follow-up migration to drop it could be filed if cleanup matters,
but the cost of leaving it is zero.

**References.**

- Commit on branch `fix-ki150-monitor-services` (2026-05-14) —
  `monitoring/alert_state_store.py` (new), `monitoring/alert.py`
  (refactor — drop `_open_default_conn`, drop `conn` from
  `send_alert` signature), `tests/monitoring/test_alert_state_store.py`
  (new, 8 tests incl. concurrent-writers), `tests/monitoring/test_alert_throttle.py`
  (refactored to JSON store + new lock-contention regression).
- [[KI-149]] — the silent-skip defect this layer should have caught;
  fixed separately in `fix-ki149-honest-equity-freshness`.
- [[KI-153]] — the exit-code-conflation observability gap surfaced by
  the misdiagnosis of part 1.
- `data/processed/finding3_ml_pipeline_gap_root_cause.md` — the
  audit that surfaced the original three-services observation.



### KI-140 — 4USDT-class deep-drawdown failure pattern (closed; describable but not addressable by exclusion or entry-conditional-horizon rules; resolved 2026-05-14)

**Original symptom (kept for archival completeness).** A recurring
deep-loss pattern distinct from KI-137's post-parabolic and
SWARMSUSDT-class shapes: a coin deeply below its 90-day high
(`dd90 < -0.40`) **in a 60-day downtrend** (`ret60` modestly positive or
negative, *not* parabolic) shows a recent bounce (positive
`ret5`/`ret10`) at the entry-time feature snapshot, and the model takes
the bounce as a buy signal. The bounce fails within days and the
position grinds down to a `time`-exit loss. Live case: 4USDT entered
2026-05-12 (pred 2026-05-11 — `dd90 -43.5%, ret60 +60%, ret5 -1.2%,
down_days 6/10`), realised −29.3% on horizon close.

**Original population.** 14 of 93 deep losses (15%) in the validated
941-trade Phase-1B-winner backtest. Avg loss −19.3%, worst −34.7%.
Characteristic profile: `dd90` mean −58.0%, `ret60` mean **−35.0%**
(median −32.6%), `ret5` mean **+9.0%**, `ret10` mean +11.9%,
`down_days` mean 4.5, exit_reason = `time` × 14 (none stopped on the
trailing).

**Why exclusion filters were rejected (ADR-028).** The proposed Variant
E filter (`dd90 < -0.40 AND -1.0 < ret60 < 1.0`) was backtested and
rejected: Sharpe collapsed from 6.32 to 4.36 (−1.96), max DD nearly
tripled (−16.98% → −40.92%), cumRet dropped 43% relative. Root cause:
`dd90 < -0.40` matches ~40% of the universe in a non-bull regime,
turning the rule into a regime gate that forces Top-N backfill from
rank-7+ low-probability trades. No tighter dd90/ret60 combination
caught the class without similar over-filtering.

**Closure (2026-05-14).** Tested via walk-fold + portfolio-simulator
(ADR-032 gates) at
`data/processed/variant_e_narrow_4usdt_validation.md`. Both gates FAIL:
walkfold dominance 0/6, portfolio Sharpe gain +0.007 (noise). Root
cause: the narrowed filter (`dd90 < -0.40 AND ret10 > +0.10 AND
ret60 < -0.20`) does NOT isolate a loser cohort. It matched 46 of 941
baseline trades with mean P&L +4.13% and hit rate 87.0% — matches
BASELINE's 87.6%. The model picks these entries correctly; there's
nothing for a shortened-horizon rule to fix.

**Implication.** The exclusion-rule family for BASELINE Policy D is
closed across three independent probes (rescue-rate heatmap,
Other-cohort classifier, Variant E''). Productive paths now require
non-exclusion approaches: probability haircut (multiplicative downscale
on `predicted_probability` before Top-N selection), direction-aware
label retrain (KI-137 root-cause fix), or a second uncorrelated edge.

**References.**

- `data/processed/variant_e_narrow_4usdt_validation.md` — both-gates
  validation that produced the closure.
- `data/processed/other_deep_loser_characterization.md` — the 47%
  Other-cohort analysis that led to the narrowed filter probe and now
  becomes [[KI-151]].
- `data/processed/rescue_rate_heatmap.md` — the parallel conditional-cut
  evidence (no Sharpe-positive cut state exists).
- [[KI-137]] — the underlying model-label root cause; remains open.
- [[KI-151]] — the residual "Other"-class follow-up.
- `DECISIONS.md` ADR-028 §"4USDT-class explicitly NOT addressed" — the
  prior rejection that motivated this probe.

### KI-143 — Universe-tier sort displaced 99 ML-universe primary tickers under the dev-mode cap (resolved 2026-05-14)

**Symptom (before fix).** ML feature coverage dropped from 411 →
312 tickers between trade_date 2026-05-01 and 2026-05-04 and
persisted at ~311 for two weeks. The displaced 99 tickers included
major large-caps: ORCL ($494B), WMT ($450B), XOM ($638B), PLTR
($345B), UNH ($334B), PFE, PM, PG, PEP, TXN, WFC, ODFL through XYL
alphabetically. They had clean state in `companies` (active,
non-ETF, sectored, ≥$10B market cap) but zero `prices_daily` rows
for any date on or after 2026-05-04. The drop was invisible in
service logs (every component exited cleanly) and was eventually
caught by the per-pipeline equity monitor (deployed 2026-05-12,
ADR-026) flagging the downstream ml_predictions T-1 → T-2
prediction_date lag.

**Root cause.** Both `ingestion/orchestrator.py:75` and
`pipelines/daily_radar.py:77` selected the universe with `ORDER BY
universe_tier, ticker` and sliced to `max_symbols=520`. In SQL,
`'extended'` sorts alphabetically before `'primary'`, so the 174
extended-tier rows (populated 2026-05-01 09:51 → 2026-05-02 09:02
per `companies.created_at`) consumed positions 0-173 of the cap,
pushing 153 primary-tier tickers (positions 520-672, alphabetical
ODFL → XYL) out of the ingest list. 99 of those passed the ML
universe filter (≥$10B, non-ETF, sectored). Pre-2026-05-01 the
universe was 504 active companies (all primary, no extended), all
of which fit under the 520 cap — the sort was latent-buggy from
inception and the extended-tier population on 2026-05-01/02 was
the trigger.

**Fix.** Add `DESC` to the ORDER BY in both call sites:
```sql
ORDER BY universe_tier DESC, ticker
```
`'primary'` > `'extended'` alphabetically, so DESC puts the 504
primary-tier tickers in positions 0-503 of the sorted list. The
520-slot cap now fills as 504 primary + first 16 extended.

**Pinned by.** 5 regressions in
`tests/equity/test_orchestrator_universe_sort.py`:
- `test_primary_tier_fills_cap_first` — behavioural pin: with 500
  primary + 174 extended seeded and a 520-slot cap, all 500 primary
  tickers are in the cap.
- `test_full_universe_returned_when_below_cap` — edge case: small
  universe (4 tickers) returns full set.
- `test_inactive_tickers_excluded` — sanity pin: the
  `is_active = true` filter remains intact under the new sort.
- `test_orchestrator_uses_primary_first_sort` — duplication-pin A:
  reads `ingestion/orchestrator.py` source, asserts the fixed
  ORDER BY clause is present and the buggy form is not.
- `test_daily_radar_uses_primary_first_sort` — duplication-pin B:
  same on `pipelines/daily_radar.py`. Both fail-loud if either
  file drifts.

**Verification.**
- RED: `.venv/bin/python -m pytest tests/equity/test_orchestrator_universe_sort.py`
  before fix → 2 of 5 tests fail (the duplication-pins) with the
  expected "must contain 'ORDER BY universe_tier DESC, ticker'"
  messages. The 3 behavioural tests pass under both code paths
  because they exercise SQL semantics (which is identical pre/post
  on the test seed) and the cap intent (which only differs at
  scale — caught by the duplication-pins).
- GREEN (post-fix): same command → **5 passed**.
- Full equity suite (`.venv/bin/python -m pytest tests/equity/
  --ignore=tests/equity/test_ml_predict.py -q`) → **775 passed,
  2 failed** (both `joblib`-import failures pre-existing on master,
  unrelated to this branch).
- Live verification against current `data/mhde.duckdb` via
  `.claude/local_scripts/diag_post_fix_universe.py`: pre-fix sort
  yields 312 ML-universe tickers in the cap; post-fix sort yields
  416 (full ML universe recovered, +104 tickers including all
  displaced names — ORCL, WMT, XOM, PLTR, UNH visible in the first
  8). Zero ML-universe tickers are newly excluded. Tier composition
  under the post-fix cap: 504 primary + 16 extended = 520.

**Operator follow-up (post-merge).** No deploy step beyond the
merge. The next `mhde-daily-analysis.service` fire (23:15 UTC)
will ingest all 504 primary tickers (vs ~346 pre-fix). The next
morning's `mhde-predict.service` (00:15 UTC) will compute features
for the full ~411 ML-universe coverage (vs 311 pre-fix, with the
remaining 5 gaps being the IPO ingestion holes — UBER, TXT, PTC,
TYL, PSKY — flagged in the assessment but unrelated to this fix).
The 01:00 UTC equity pipeline-monitor should flip 🟢 on the
features step. The historical T-2 prediction surface is not
retroactively rewritten — coverage advances cleanly going forward.

**Open follow-ups.** KI-122 (extended-tier reconciliation leak)
amended above to note the displacement-side-effect is now
mitigated, but the underlying reconciliation gap remains.
KI-144 (shared helper to eliminate the parallel-query
duplication) and KI-145 (`max_symbols` cap removal post-grouped-
daily) are the structural follow-ups; ADR-031 explicitly defers
both.

### KI-142 — Stooq freshness window short-circuited T-0 fill after the Polygon grouped-daily switch (resolved 2026-05-14)

**Symptom (before fix).** Two days running (2026-05-13 and 2026-05-14)
the per-pipeline equity monitor (ADR-026) flagged `Feature pipeline
(ml_features)` 🔴 — `MAX(trade_date)=2026-05-12 (311 rows) — expected
features for 2026-05-13`. `mhde-predict.service` itself was firing on
schedule (00:15 UTC daily, exit 0, `ml backfill-features → ml predict`
chained as designed); the predict log showed `Scoring universe for
2026-05-12` with a healthy `Loaded features for 311 tickers`. The
problem was upstream: `prices_daily` for 2026-05-13 had only 4 rows
(three foreign ADRs from Stooq, one macro from Yahoo) — no rows for
the active universe — so `ml backfill-features` could not write a
feature row for trade_date 2026-05-13 and `ml predict` correctly
fell back to the latest universe-complete date (2026-05-12, T-2).

The Stooq side told the same story across three nightly runs of
`mhde-daily-analysis.service` (23:15 UTC):

```
2026-05-11 23:15  Stooq: 517 rows inserted for 517/520 tickers   ← pre-grouped
2026-05-12 23:15  Stooq:   2 rows inserted for 2/3   tickers     ← post-grouped
2026-05-13 23:15  Stooq:   6 rows inserted for 6/6   tickers
```

**Root cause.** Commit `473b92a` (2026-05-09) replaced the per-ticker
Polygon ingestor with the grouped-daily endpoint, which serves T-1 the
same evening at 23:15 UTC for the entire ~12k-ticker US stock list and
403s on T-0 (Polygon hasn't published the same UTC day yet). Every
universe ticker gained a T-1 row in `prices_daily` immediately after
Polygon's pass — and that satisfied
`ingestion/ingest_stooq.py:_tickers_needing_prices`, which used
`trade_date >= today - 2 days` as its "fresh" predicate. Stooq's
fallback consequently dropped from a 504-ticker universe sweep to
2-3 ADR fills per night.

The end-to-end chain looked healthy at every layer — Polygon ingested
504 tickers, Stooq exited "ok", `mhde-predict.service` exited 0, the
predict log line `Loaded features for 311 tickers` was present — and
yet the production output silently shifted from T-1 to T-2 features
because no surface in the ingestion stack distinguished "Polygon wrote
yesterday's prices" from "today's prices are now in the table". The
gap was visible only by reading `MAX(trade_date)` against an external
expectation, which is exactly what the per-pipeline monitor (deployed
2026-05-12) started doing.

**Fix.** `ingestion/ingest_stooq.py:_tickers_needing_prices` rewritten
to compare `trade_date = today` exactly. Yesterday's polygon row no
longer counts as "fresh" for the purpose of deciding whether to call
Stooq. This restores the pre-2026-05-09 behaviour (Stooq sweeps the
full universe every nightly run) and aligns the freshness predicate
with what the Stooq `/q/l/` endpoint actually returns (today's
quote). `_FRESHNESS_DAYS` constant deleted.

**Pinned by.** 4 regressions in `tests/equity/test_ingest_stooq.py`:
- `test_tickers_needing_prices_returns_universe_when_only_yesterday_in_db`
  — unit-level: a universe with only T-1 rows must come back as
  needing today's price.
- `test_tickers_needing_prices_skips_when_today_already_present` —
  forward-pin: a ticker with today's row is fresh and not re-fetched.
- `test_ingest_fetches_today_when_universe_has_only_yesterday` —
  end-to-end pin: with T-1 polygon rows present, `ingest()` makes the
  HTTP call to Stooq and inserts a T-0 row.
- `test_polygon_t1_does_not_short_circuit_stooq_t0` —
  orchestration-shape integration test (the gap that originally let
  the regression slip through the test suite): 5-ticker universe with
  T-1 polygon rows, asserts Stooq writes T-0 for all 5.

`test_polygon_prices_not_overwritten_by_stooq` re-seeded to use today's
date so it remains a meaningful PK-overwrite guard under the new
contract.

**Verification.**
- RED: `.venv/bin/python -m pytest tests/equity/test_ingest_stooq.py -v`
  before fix → 3 of 4 new tests fail with the expected messages
  (`assert set() == {'AAPL','MSFT','NVDA'}`, `assert 0 == 1`, `assert 0 == 5`).
- GREEN (post-fix): same command → **15 passed**.
- Full equity suite (excluding the pre-existing
  `test_ml_predict.py` collection error from the missing `joblib` in
  `.venv`): **770 passed, 2 failed** — both failures
  (`test_smoke_test_fails_without_active_models`,
  `test_smoke_test_flags_missing_joblib`) confirmed identically
  failing on master via `git stash` baseline; both fail on `import
  joblib` in `monitoring/smoke_test.py`, unrelated to this branch.
- Regression suite: **30 passed, 3 failed** — all three
  (`test_no_module_level_connection` KI-105,
  `test_active_model_paths_resolve`,
  `test_repo_vs_deployed_unit_parity` KI-112) pre-existing on master.

**Operator follow-up (post-merge).** No deploy step beyond merging.
The next `mhde-daily-analysis.service` fire (23:15 UTC) should show
the Stooq line jump back to ~500 rows for ~500/500 tickers. The next
morning's `mhde-predict.service` should log `Scoring universe for
{T-1}` matching `prices_daily latest` (the existing T-2 lag in
`ml_predictions` will close on its own from the next predict run
forward; historical rows are not retroactively rewritten — the
prediction surface simply advances cleanly going forward). The
per-pipeline equity monitor's 01:00 UTC fire should flip 🟢.

**Open follow-up.** None tracked here. ADR-030 records the
freshness-contract rationale; the gap that let it slip past the
existing per-ingestor test suite (no test exercised the
"polygon-just-wrote-T-1-then-stooq-runs" sequencing) is now closed
by `test_polygon_t1_does_not_short_circuit_stooq_t0`.

### KI-138 — Cap-at-today-1 OHLCV ingestion broke the prediction-export staleness gate (option A resolved 2026-05-12)

**Symptom (before fix).** Commit `8f9d707` ("stop freezing partial-day
OHLCV candles") changed `backfill_ohlcv` to ingest only fully-closed UTC
days (`end_date = today - INGESTION_LAG_DAYS`, =1). Downstream,
`MAX(trade_date)` in `crypto_prices_daily` — and therefore in
`crypto_ml_features` — became structurally `today - 1`. But
`crypto/exports/write_daily_predictions.py:_check_freshness` required
`MAX(trade_date) == prediction_date` where `prediction_date` defaulted
to `today` UTC. So the daily `crypto export-predictions` run (the
`mhde-crypto-export-predictions.timer`) aborted with `ExportPreflightError("features stale: MAX(trade_date)=…-1, expected …")`
every day; `data/exports/predictions_latest.json` stayed pinned to the
last good pre-`8f9d707` file; and the engine — which validates
`export_date == today_utc` per INTERFACE.md §3.2 — rejected it and
skipped the entry phase. Net effect: no positions placed since
`8f9d707` landed (2026-05-11).

**Root cause.** Two coupled assumptions in the exporter that the
ingestion fix invalidated: (1) `_check_freshness` treated "freshest
features are for today" as the only healthy state; (2) the JSON
`export_date` was set to `prediction_date`, which was being used both as
"the features date" and "the trading date these predictions drive" —
fine when those coincided, wrong once ingestion lags a day.

**Fix (option A — this branch `fix-export-preflight-cap-at-today-1`).**
- `_check_freshness(conn, export_date)` now accepts `MAX(trade_date) ==
  export_date` **or** `== export_date - 1` and returns the validated
  features-as-of date; anything older still raises `ExportPreflightError`
  (genuine pipeline staleness — e.g. ≥2 days behind).
- `build_predictions` loads features for that returned features-as-of
  date (not blindly for `export_date`).
- JSON `export_date` is now unambiguously today UTC — the trading date,
  matching INTERFACE.md §3.1 and the engine's §3.2 validation.
- New informational JSON field `features_as_of_date` (= the
  `MAX(trade_date)` used for inference; `export_date - 1` on a normal
  cap-at-today-1 run) for downstream consumers / debugging. The engine
  loader is unchanged and does not validate this field.
- Post-parabolic exclusion rows (`crypto_signal_exclusions.export_date`)
  and `predicted_at` continue to use the export date — unchanged.

**Regression tests.**
`tests/crypto/exports/test_write_daily_predictions.py`:
`test_preflight_accepts_features_one_day_old`,
`test_preflight_fails_when_features_two_days_stale`,
`test_export_date_is_today_utc_and_features_as_of_is_yesterday`,
`test_features_as_of_date_equals_max_trade_date_when_same_day`.

**Operator follow-up (post-merge).** Run a one-off
`venv/bin/python main.py crypto export-predictions` to regenerate
today's `predictions_latest.json`, then trigger a manual entry-timer
fire on the engine side to confirm it picks up the fresh file and opens
positions.

**Open follow-up (option B — deferred).** `crypto_ml_predictions.prediction_date`
(written by `crypto/ml/predict.py:score_universe`, defaulting to
`MAX(trade_date)` = `today - 1`) is semantically the *features / entry*
date, while the export's `export_date` is the *trading* date (`today`).
The two now differ by a day. The exporter does its own inference and
never reads `crypto_ml_predictions`, so this is not an operational
conflict today, but the dual meaning of "prediction_date" is a latent
trap for outcome-fill and any future consumer that joins the two. Option
B is to make the schema carry both dates explicitly (or rename) so
"signal generated from day X-1 features, traded on day X" is
unambiguous. Schema change → deferred until prioritised. See ADR-025.

### KI-135 — Crypto retrain auto-promoted without validation (resolved 2026-05-10)

**Symptom (before fix).** `crypto/ml/train.py:244-259` unconditionally
flipped `is_active=true` on every newly-trained model, demoting
whichever row had been active for the horizon. Today's retrain
promoted `crypto_10d_7760a3f6` and `crypto_5d_ac900cbf` with zero
comparison against the prior active model. A regression in either
(training data corruption, feature pipeline issue, degenerate
solution) would have silently entered Phase E paper trading on the
next entry phase.

**Fix.** Branch `gap1-model-retrain-validation-gate`. New
`crypto/ml/validation_gate.py` runs after training: gates the
`is_active` flip on the new model's label hit rate ≥ 0.9 × previous
active model's. On fail the new row stays `is_active=false` with
`promotion_status='promotion_blocked'` and a critical Telegram alert
fires; old model stays active. See ADR-019 for the full design.

**Commits.** `2a666cd` (schema), `70563ed` (sharpe utility, unused by
final gate), `7eca751` + `222345d` (gate; second commit drops Sharpe
arm discovered to be non-functional), `b584e2a` (train.py wiring).

**Escape valve.** Manual override via OPERATIONS.md "Retrain
validation gate" section if a false positive blocks a good model.

**Open follow-up.** If hit-rate-only proves too forgiving, add AUC
arm (`auc_roc` is also stored, directly comparable). Defer until
observed.

- **KI-133 — mhde-monitor-streamlit-freshness service in failed
  state** (opened + resolved 2026-05-10). No bug — exit 1 is the
  intentional alert signal. The monitor is working as designed; the
  `failed` systemd state is how it surfaces "dashboard code is
  stale" to the operator. Re-classified from "monitor broken" to
  "monitor fired correctly but operator missed the alerts under
  weekend false-positive noise" — that operator-side observation now
  tracked under KI-134.

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
