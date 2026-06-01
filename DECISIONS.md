# Architecture Decision Records

Format: one record per major decision. Each record states the context,
the decision, and the consequence so future-you (or future Claude Code)
can re-evaluate without re-deriving the rationale.

---

## ADR-001 — Preserve legacy code rather than delete it

**Date:** 2026-05-07
**Session:** Session 0 of `HARDENING_PLAN.md`
**Status:** Active

**Context.** Roughly 100 .py files (across 5 whole directories and
~20 individual files) had no path from any ACTIVE entry point —
systemd unit, dashboard tab, or pipeline. They were imported only by
dormant CLI commands or by other dormant code.

**Decision.** Move them to `legacy/` rather than delete. Preserve git
history via `git mv`. Re-evaluate deletion after 2 weeks of stable
operation post-Session 7.

**Consequence.** A safety net: if a regression points at a missing
function, the code is recoverable in one `git mv` step. The repo gets
quieter without losing institutional memory. The trade-off is the
~4 MB on disk and the ongoing temptation to grep into `legacy/` instead
of treating it as opaque.

---

## ADR-002 — Retire the Flask catalyst-review server

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `review/server.py` (~3900 lines) plus
`mhde-review-server.service` and `mhde-bridge-relay.service` (always-on
user-level units) served `https://mhde.duckdns.org/review/`. The
catalyst review UI it powered is no longer used in the workflow.

**Decision.** Move `review/` to `legacy/review/`. Disable both services
(`systemctl --user disable --now`). Remove the
`upstream mhde_review { ... }` block and the `location /review/`
proxy from `/home/jpcg/homeboard/nginx/nginx.conf`. Reload nginx.

**Consequence.** The `/review/` subpath now returns 502 (Streamlit
returns an error for the path because it falls through to
`location /` → Streamlit relay, and Streamlit's relay rejects the
unknown path). The architectural goal — no review server can serve
traffic — is achieved. A clean 404 would require an explicit
`location /review/ { return 404; }` block and is deferred. No
production code or data depends on the review server being reachable.

---

## ADR-003 — External FX data repo (`/home/jpcg/ATSRP/`) stays put

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `HARDENING_PLAN.md` Session 0 deliverable 7 asked whether
to relocate `/home/jpcg/ATSRP/research/gbpeur_personal_fx/` (the old
FX research code) into MHDE. `INFRASTRUCTURE.md` confirms ATSRP is
**actively used**: `fx/data/refresh.py` shells out into ATSRP for
Dukascopy bi5 hourly bars, and `notifications/telegram.py` reads
`/home/jpcg/ATSRP/.env` for `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`.

**Decision.** Leave ATSRP exactly where it is. Do not relocate the
research code; do not duplicate the secrets. The plan's "Option B" —
keep ATSRP as a historical reference *and* an active dependency —
matches reality.

**Consequence.** ATSRP remains a hard dependency of the FX engine.
Anything that touches FX data refresh or Telegram credentials must
continue to reach ATSRP via subprocess / .env path. Document this in
INFRASTRUCTURE.md (already done) so it isn't surprising in future
sessions.

---

## ADR-004 — Drop the dead `outcomes.compute_forward_returns` re-export

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `outcomes/__init__.py` re-exported
`compute_forward_returns` from `outcomes/labels.py`. A grep across the
codebase showed zero callers of `outcomes.compute_forward_returns`
outside of `__init__.py` itself. The active forward-return code lives
in `outcomes/tracker.py:update_forward_returns`, not in
`outcomes/labels.py`.

**Decision.** Move `outcomes/labels.py` to `legacy/outcomes/labels.py`
and delete the re-export line from `outcomes/__init__.py`. Same for
`outcomes/candidate_lifecycle.py` (only used by the legacy review
server).

**Consequence.** `outcomes/__init__.py` exposes only the symbols that
are actually consumed: `create_outcome_record`, `update_forward_returns`,
`get_pending_outcomes`, `update_review_status`. No behavior change for
any caller.

---

## ADR-005 — Plan deviates from the codebase; codebase wins

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `HARDENING_PLAN.md` Session 0 listed several directories
as legacy (`features/`, `scoring/`, parts of `missed/`, the
`daily_radar` orchestration). An import-graph analysis showed those
items are still reachable from `mhde-daily-analysis.service` (which
runs `daily_radar` → `prediction-vs-actual` → `enrich-root-causes` →
`priority-refresh-queue` → `daily-catalyst-queue` Mon-Fri 23:15 UTC).

**Decision.** The codebase is the source of truth, not the plan. Files
that are imported by ACTIVE entry points stay at the active path;
everything else moves to `legacy/`. `legacy/README.md` documents the
specific corrections.

**Consequence.** The "30-50% file count reduction" target in the plan's
Session 0 exit criteria is not met as stated — the plan was wrong about
what was movable. Actual reduction is roughly 100 files out of ~250
project .py files (~40%, mostly concentrated in `models/`, `learning/`,
`governance/`, `backtest/`, `review/`, and the 19 `_legacy` dashboard
pages). Update HARDENING_PLAN.md before Session 1 to reflect the real
ACTIVE / LEGACY boundaries.

---

## ADR-006 — XGBoost over logistic regression for all three engines

**Date:** pre-existing, ratified Session 1 (2026-05-07).
**Status:** Active.

**Context.** Each of the three engines could in principle use any binary
classifier. The original (legacy) MHDE engine used a hand-tuned
scorecard. The rebuild needed a model class that handled
non-linearities, mixed-scale features (counts vs ratios vs prices),
and partial NaNs without aggressive preprocessing.

**Decision.** All three engines use `xgboost.XGBClassifier` as the
production model. Confirmed in `ml/train.py`, `crypto/ml/train.py`,
`fx/ml/train.py`. Logistic regression is not used in any production path.

**Consequence.** All three retrain pipelines depend on the `xgboost`
PyPI package (checked at runtime by
`health/operational.py:88` via `importlib.util.find_spec`). Feature
preprocessing is intentionally minimal — no scaling, no imputation,
because XGBoost handles them internally. Trade-off: model interpretability
is via `feature_importance_json` only, not via signed coefficients.

---

## ADR-007 — Walk-forward CV, not random split

**Date:** pre-existing, ratified Session 1 (2026-05-07).
**Status:** Active.

**Context.** Time-series data has temporal autocorrelation; a random
train/test split lets the model "see" rows from the future via near-
duplicate neighbors, inflating reported metrics relative to live
performance.

**Decision.** All three retrain pipelines use walk-forward
cross-validation: train on rolling window ending before fold start,
test on the next chunk. `ml/train.py:train_walk_forward`,
`crypto/ml/train.py`, `fx/ml/train.py`. AUC / lift / precision metrics
written to `*_model_runs` are computed over the held-out walk-forward
folds, not in-sample.

**Consequence.** Reported metrics are honest to the autocorrelation
structure. Models train on less data per fold than a random-split
training would, so absolute AUC tends to be lower (but more reflective
of live performance). All four FX models PASS at AUC 0.79-0.85, Lift
1.4-1.8x — see commit `f2ba4e8`.

---

## ADR-008 — DuckDB single-file as the sole persistence layer

**Date:** pre-existing, ratified Session 1 (2026-05-07).
**Status:** Active.

**Context.** The system needs persistent storage for prices,
fundamentals, features, predictions, outcomes, signals, audit logs,
and dashboard state. Options considered (implicitly): Postgres,
SQLite, DuckDB, flat files.

**Decision.** Single DuckDB file at `data/mhde.duckdb`. ~52 tables
share one writer. No schema partitioning per engine.

**Consequence.** Pros: zero ops overhead (no daemon, no backups
beyond file copy), strong analytical queries via columnar storage,
fast joins across engines for the dashboard. Cons: **single-writer
constraint** — DuckDB allows one writer at a time; long-running
writers (daily-analysis) block hourly writers (FX predict). This is
mitigated by:
1. `storage/db.py:_connect_with_lock_retry` — 30s/60s/120s back-off.
2. Staggered timer firings (see ADR-009).

If write contention becomes worse with crypto + FX growth, the next
step is per-engine DuckDB files joined at the dashboard layer, not a
move to Postgres.

---

## ADR-009 — Service chaining inside ExecStart, not multiple timers

**Date:** pre-existing, ratified Session 1.
**Status:** Active.

**Context.** Each engine's prediction needs multiple steps in sequence
(crypto: 6 steps; FX: 4 steps). Two ways to schedule:
(a) one timer per step, with `Requires=`/`After=` chaining;
(b) one timer that triggers a service whose unit lists multiple
sequential `ExecStart=` commands.

**Decision.** Use option (b) — the `oneshot` service contains all
chained `ExecStart=` lines. See `systemd/mhde-crypto-predict.service`
(6 ExecStarts) and `systemd/mhde-fx-predict.service` (4 ExecStarts).

**Consequence.** All chained steps share a single systemd "unit run",
so the journal shows them together and a partial failure aborts the
chain. Failure isolation is coarser than (a) would give but operational
clarity is much higher (one log file per timer firing). `TimeoutStartSec=1800`
on the crypto unit gives headroom for Binance rate limits.

---

## ADR-010 — Freshness guards at the top of every prediction pipeline

**Date:** 2026-05-07 (committed in `7b46c50`).
**Status:** Active.

**Context.** Pipelines previously assumed input data was fresh. When
upstream data was lagging (Yahoo missing a day, Binance throttled,
Dukascopy 404'ing), pipelines wrote predictions over stale data with
no warning. The dashboard then showed yesterday's predictions as if
they were today's.

**Decision.** Every predict pipeline calls `pipelines/freshness.py`
as its first action:
- Equity: skip with `DATA STALE` if `prices_daily` > 2 trading days old.
- Crypto: skip if `crypto_prices_daily` > 1 calendar day old.
- FX: log warning but **continue** (partial / lagging bars are still
  valid prediction surfaces; signal suppression downstream handles low
  confidence).

**Consequence.** Skip events are visible in logs and dashboard
freshness indicators. The asymmetry between equity/crypto (skip) and
FX (warn) is intentional: FX runs hourly so the cost of a missed
firing is high; equity/crypto run daily so a skip is cheap and a stale
prediction is more misleading than a missing one.

---

## ADR-011 — Position-aware FX alert suppression

**Date:** 2026-05-07 (committed in `7b46c50`).
**Status:** Active.

**Context.** The FX bot was sending BUY_GBP alerts when JP was already
long GBP/EUR, and SELL_GBP alerts when already short. The signal was
correct in the abstract but operationally noise — JP doesn't act on
"buy more of what you already have."

**Decision.** `fx/bot/telegram_bot.py:send_signal_alert` consults
`fx_position` before sending. If the signal direction matches the
current position direction, the signal is recorded in `fx_signals`
(with `telegram_sent=FALSE`) but no Telegram message is sent. The
4h cooldown via `fx_alert_state` runs in front of this gate; the
position check sits behind cooldown.

**Consequence.** The signal table remains complete (no information
lost) but the alert stream is actionable. Adding a position requires
the bot's `/setposition` command or `main.py fx set-position` CLI;
forgetting to update position re-introduces noisy alerts, so the bot
also handles `/clearposition`.

---

## ADR-012 — Per-engine `schema.py` instead of one shared schema module

**Date:** pre-existing, ratified Session 1.
**Status:** Active.

**Context.** Three engines share one DuckDB file but have very
different schemas (equity / crypto / FX). The original engine put all
tables in one `storage/schema.sql` file.

**Decision.** Each new engine got its own `schema.py` module
(`ml/schema.py`, `crypto/schema.py`, `fx/schema.py`). Each owns its
tables; `create_all_tables(conn)` is called by the engine's CLI on
first run. The original equity ingestion / scoring tables stayed in
`storage/schema.sql` + `storage/migrations.py`.

**Consequence.** Engines can evolve their tables independently without
touching shared migrations. Trade-off: there's no single place to read
"the schema" — `DATABASE_SCHEMA.md` exists to bridge that gap. A
future migration to add cross-engine relationships (e.g. shared model
registry) would have to choose one of the four schema-loading paths to
own the new table.

---

## ADR-013 — Migrate FX bars from Dukascopy (via ATSRP) to TwelveData

**Date:** 2026-05-07 (Session 1) / 2026-05-08 (Session 2 cutover).
**Status:** IMPLEMENTED. Production fx_prices_hourly is now fed by TwelveData.

**Cutover (2026-05-08, same day as Session 2).** Gate executed against a
30-day historical backfill instead of the originally-planned 24-hour
parallel window:

- **Coverage.** TwelveData covered 720/720 hourly bars over 30 days;
  Dukascopy was missing 240 (33%). The coverage gain was the dominant
  cutover driver, not the agreement metric.
- **Agreement.** 472/480 matched bars within 5 pips. The 8 breaches all
  sit in the 20:00-21:00 UTC NYSE-close window with consistent sign
  (Dukascopy > TwelveData by 5-7 pips), consistent with the post-close
  liquidity-venue rotation rather than random source disagreement.
- **Weekend bars.** Verified to be real OTC quotes (avg 2.84 pips
  close-open movement, 0/192 zero-range bars over 4 weekends). No
  filtering needed for downstream features/labels.

**What changed at cutover.**
- `fx/data/refresh.py` rewritten as a thin wrapper that calls the
  TwelveData implementation, writing to the production
  `fx_prices_hourly` table. ATSRP subprocess dependency removed.
- The 240 historical Dukascopy gaps in `fx_prices_hourly` were filled
  from `fx_prices_hourly_twelvedata_backfill` (the 30-day snapshot).
- `mhde-fx-predict.service` lost the parallel
  `fx refresh-prices-twelvedata` ExecStart; the remaining
  `fx refresh-prices` line now runs the TwelveData fetcher.
- Reader paths (predict / features / labels / freshness / dashboard)
  unchanged — they still read `fx_prices_hourly`. Only the writer flipped.

**1-week stability buffer (cleanup deferred to ~2026-05-15).** Drop
`fx_prices_hourly_twelvedata`, `fx_prices_hourly_twelvedata_backfill`,
the `fx refresh-prices-twelvedata` and `fx compare-sources` CLI
subcommands, and `fx/data/compare_sources.py`. Tests for those modules
move out at the same time.

**Original plan (preserved for context):**

**Context.** The FX engine (GBP/EUR hourly) currently fetches bars
from Dukascopy through a subprocess into `/home/jpcg/ATSRP/`
(`fx/data/refresh.py`). Two long-standing operational issues:

1. **Lag.** `fx_predict.log` regularly shows `ATSRP fetch:
   status=NO_DATA, bar=… HTTP 404` for the most recent hour;
   measured gap was 3+ hours on 2026-05-07 18:05. Pre-validation
   showed TwelveData is ~11 minutes behind realtime.
2. **External path dependency.** Dukascopy ingestion lives entirely
   outside the MHDE repo (in ATSRP). It's hard to test, hard to
   monitor, and ATSRP's `.venv` is a separate runtime
   (`/home/jpcg/ATSRP/.venv/bin/python`). We retain ATSRP for the
   Telegram credentials it stores (ADR-003) but want to remove it
   from the data path.

**Decision.** Migrate FX bar ingestion from Dukascopy to TwelveData
in two sessions:

- **Session 1 (this commit) — parallel run, no cutover.** Add
  `fx/data/refresh_twelvedata.py` that mirrors the Dukascopy
  `fx/data/refresh.py` interface but writes to a new mirror table
  `fx_prices_hourly_twelvedata`. Wire it into the hourly systemd
  unit (`mhde-fx-predict.service`) as an extra ExecStart line. The
  production path is unchanged — predict / features / labels /
  freshness all still read `fx_prices_hourly`.

- **Session 2 — cutover (deferred).** Gated on the comparison
  criterion: `main.py fx compare-sources --hours 24 --threshold-pips 5`
  exits 0, meaning every matched hourly bar agreed within 5 pips on
  close. Pre-validation already showed agreement within 1.5 pips on
  the 2026-05-07 18:00 bar, so passing 24 bars by the same standard
  is highly likely. After cutover: switch readers to TwelveData,
  remove the Dukascopy ExecStart, drop the mirror table.

**Why TwelveData.**
- Free tier delivers 800 API calls/day; we use 24 (one per hour).
- Simple REST API; no subprocess; in-process Python `requests`.
- Pre-validation showed real-time-ish data and bar agreement with
  Dukascopy within 1.5 pips on the 2026-05-07 18:00 sample.

**Why mirror-table strategy** (vs. dual-write to `fx_prices_hourly`).
Lowest risk: production readers see exactly the same data
they always have during the migration window. Comparison is an
independent SQL JOIN, not a constraint on the hot path. Drop is a
single `DROP TABLE` post-cutover.

**Rollback.** Revert the systemd ExecStart line and the mirror table
keeps existing rows but stops growing. Production has been untouched
by definition. No code path consumes the mirror table outside the
compare-sources CLI.

**Consequence.**
- New env var `TWELVEDATA_API_KEY` (in `MHDE/.env`, surfaced via
  `storage.config.load_engine_config` overlay).
- New table `fx_prices_hourly_twelvedata`. Will be dropped after
  Session 2 cutover stabilizes (1 week buffer).
- `mhde-fx-predict.service` ExecStart grows by one line; the
  KI-112 regression test (`test_repo_vs_deployed_unit_parity`) will
  fail until the deployed unit is refreshed with `sudo cp …
  /etc/systemd/system/` and `sudo systemctl daemon-reload`.

---

## ADR-014 — Equity universe scope is S&P 500 + named extras (max_symbols=520), not a Polygon-cost workaround

**Date:** 2026-05-09
**Session:** Equity ingestion fix session
**Status:** Active

**Context.** During the KI-120 triage we found that `pipelines/daily_radar.py:80-83`
applies a `max_symbols` cap from `config/universe.yaml` on top of the
universe loaded from `companies WHERE is_active=true`. The cap value
is 520 and the log line calls it "Dev mode". On inspection the cap is
not a debugging tunable and not a Polygon-cost workaround; it is the
deliberate production scope.

**Composition of the 520 (verified in production DB):**

- 504 primary-tier rows = current S&P 500 list from
  `universe/sp500_tickers.yaml` + the 6 named extras under
  `fallback_tickers` in `config/universe.yaml`
  (AAPL, NVDA, TSLA, JPM, UBER, RKLB).
- ~16 extended-tier slots filled per build, drawn from a SEC-filtered
  list (`universe.filter_non_equities`) until total reaches `max_symbols`.

`companies WHERE is_active=true` reports 678 (504 primary + 174
extended) only because the universe builder doesn't reconcile
extended-tier rows across builds — they accumulate as residue from
prior builds. This is tracked separately as KI-122 and does not
change the production scope. The extra 174 stale extended rows
don't reach `ml_features` or `ml_predictions` in practice.

**Decision.** Treat 520 as the canonical equity universe scope. The
authoritative source of truth is `config/universe.yaml`; the
existing comment block in that file ("Set high enough to leave a
few extended slots after ~503 S&P primaries") is what governs the
number. The cap in `daily_radar.py` is therefore a hard scope bound,
not a soft cost limit.

The grouped-daily Polygon endpoint (this session's ingestion fix)
removes the per-ticker API-call cost that the cap was historically
suspected to be guarding against. The cap stays where it is for
reasons of investment scope, not API economics.

**Consequence.**
- Raising `max_symbols` would add SEC-filtered extended-tier
  fillers (small-caps that pass only name heuristics, no
  liquidity/cap filter), not S&P 500 expansion. Most have thin
  data quality. Don't raise without a corresponding upgrade to
  the universe builder's extended-tier filter.
- The misleading "Dev mode" log prefix in `daily_radar.py:83` is
  tracked as KI-123. When that log line is updated, point operators
  at this ADR for the cap rationale.
- The `ml backfill-features` step computes features for the full
  result of the universe builder (~411 tickers per healthy day in
  practice — primary tier minus tickers without 60d price history).
  The 520-cap and the 411-feature numbers are not the same metric:
  cap is "how many tickers ingestion attempts", features is "how
  many computed successfully". They will rarely match exactly and
  this is fine.

---

## ADR-015 — Asymmetric pipeline_execution recency budgets per engine

**Date:** 2026-05-09
**Session:** KI-124 fix
**Status:** Active

**Context.** `monitoring/pipeline_execution.py` checks
`now - MAX(prediction_date)` against a per-engine `RECENCY_BUDGET`
to decide whether each prediction pipeline ran on schedule. The
`prediction_date` column has different semantics per engine:

- **Equity** writes `prediction_date = T-1` (the previous trading
  day). Equity ML predict fires at 00:15 UTC daily; on Mon morning
  the most recent prediction_date is Friday (no fresh trading data
  Sat/Sun), and the row stays "Friday" until Tuesday's fire writes
  Monday. So `now - prediction_date_midnight` is naturally up to
  ~72h on a Sunday evening even though the pipeline is healthy.
- **Crypto** writes `prediction_date = T` (today; crypto trades
  24/7). Latest prediction_date refreshes daily at 00:30 UTC; the
  pre-existing 27h budget covers a 24h cycle plus 3h grace.
- **FX** writes `prediction_date = datetime_utc` (timestamp, not
  date). Latest refreshes hourly at :05; a 2h budget covers a 1h
  cycle plus 1h grace.

**Decision.** Set `RECENCY_BUDGET` asymmetrically:

| Engine | Budget | Rationale |
|---|---|---|
| equity | 75h | 72h Fri→Tue weekend roll + 3h grace |
| crypto | 27h | 24h daily cycle + 3h grace |
| fx     | 2h  | 1h hourly cycle + 1h grace |

The 75h equity budget assumes an ordinary weekend. US market
holidays that extend a weekend (e.g. Thanksgiving Friday off,
Memorial Day Monday off) would push the gap past 75h and produce a
warn. That's accepted: a holiday-extended outage warrants an
operator-level note ("we're going to flag for ~24h after this
holiday weekend") rather than over-relaxing the budget so the
monitor can't catch a real outage on a normal weekend.

**Why not raise to 96h to cover holidays?** Each hour added to the
budget weakens the monitor's ability to detect a real outage. 75h
catches a real outage that misses the Tuesday morning fire; 96h
would not catch it until Wednesday morning. Holiday warnings are
acknowledgeable; outage warnings need to fire fast.

**Why not add `row_inserted_at TIMESTAMP` to `ml_predictions` and
key the recency check off the actual write time?** That's the
better long-term fix — it removes the conflation between
prediction_date semantics and write time entirely — but requires a
schema migration plus monitor refactor. Out of scope for this
session. Captured as a future option in the resolved KI-124 entry.

**Consequence.**
- The "Dev mode" log line in daily_radar.py:83 (KI-123) is no
  longer the only daily_radar message that mis-implies status —
  but the recency-budget asymmetry is now an explicit design
  choice rather than a forgotten constant.
- `monitoring/pipeline_execution.py:RECENCY_BUDGET` carries an
  inline comment per engine pointing back to this ADR.
- A future session that adds `row_inserted_at` should land that
  schema column AND tighten all three budgets back to single-hour
  multiples (the budgets stop needing to absorb prediction_date
  semantics once the recency check uses real write time).

---

## ADR-016 — Trust ladder: "fixed" requires Level 5 verification

**Date:** 2026-05-09
**Session:** Monitoring-gaps session
**Status:** Active

**Context.** On 2026-05-09 a real bug fix to the equity dashboard's
maturity-date column passed every code-side check — DB query
returned the right values, unit tests green, regression tests green,
the manual probe confirmed the helper computed the expected May 22
estimate from production data — but the user's CSV download still
showed blanks for the rest of the day. Root cause: the
`mhde-streamlit.service` process had been running since 18 hours
before the fix was committed, and Streamlit doesn't auto-reload in
this deployment. Every layer up to and including "Claude Code calls
the dashboard helper directly" was correct; only the user-visible
artifact lied.

The same failure mode is latent for any change that lands without
a restart of the consuming process: dashboard renders, scheduled
Telegram messages, exported CSVs, anything where the user reads
output produced by a long-lived process.

**Decision.** Adopt a six-level "trust ladder" and require **L5
verification** as part of every session's exit criteria. The levels:

| Level | Predicate |
|---|---|
| L0 | Code committed |
| L1 | Tests pass |
| L2 | Database state correct |
| L3 | Service / pipeline produces expected output |
| L4 | Dashboard renders correctly (and does so with the latest code) |
| L5 | User-visible artifact (CSV, Telegram message, report) matches expectation |

The full table with verification commands lives in
[`OPERATIONS.md` → "Trust ladder"](OPERATIONS.md#trust-ladder).
HARDENING_PLAN's universal exit criteria block adds an explicit L5
bullet pointing back here.

**Why six levels and not "code + tests + manual verify".** Naming
each level forces an explicit hand-off between layers. When a
session reports "fix verified", the report should say *which level*
was verified. "L4 verified" without "L5 verified" is exactly the
2026-05-09 failure: the dashboard layer can be correct while the
serving layer is stale.

**What L5 verification looks like in practice.** For a dashboard
fix: the **user** pulls a fresh CSV / loads the dashboard page in
their browser / takes a screenshot of the column they cared about,
and confirms it matches the code's expected output. Not Claude Code
running the same query helpers from a fresh Python process — that
is L2 (or possibly L3) at best. For a Telegram fix: a real send
into the channel after the bot service restart. For a pipeline
fix: the next scheduled firing's log + DB write, observed
end-to-end.

**Monitors that close the L4 → L5 gap.**

- `monitoring/streamlit_freshness` — process start time vs latest
  master commit timestamp; alerts if stale.
- `monitoring/dashboard_synthetic` — HTTP liveness on
  `/_stcore/health` plus calling the query helpers directly to
  confirm they don't raise.
- `monitoring/dashboard_consistency` (extended) — column-level
  completeness assertions per engine × horizon, so an "all-NULL
  column where data should exist" failure pages the operator
  rather than waiting for a user complaint.
- `monitoring/cross_artifact` — verifies the Telegram daily-health
  message agrees with direct DB queries.

**Consequence.**
- Session reports must state the highest level verified. "Tests
  pass" alone closes nothing.
- The "fix verified" claim now has an external check: the four
  monitors above either page within an hour of any L4-or-higher
  regression, or the operator writes a short L5-verification
  artifact (CSV screenshot / Telegram screenshot) into the session
  report when a monitor doesn't apply.
- Dashboard changes specifically gain a hard requirement to
  restart `mhde-streamlit.service` before claiming any level
  above L3. The OPERATIONS deploy matrix flags this; the
  streamlit_freshness monitor catches forgotten restarts.


## ADR-017 — Engine-export contract: file-based, MHDE-side production

**Date:** 2026-05-10
**Status:** Accepted

**Context.**
The crypto-trading-engine (separate repo at
`/home/jpcg/crypto-trading-engine/`) needs two inputs from MHDE for
Phase 2/3 paper trading: a strategy spec (rare updates) and a daily
ranked predictions list. Direct DB access from engine to MHDE was
ruled out as too coupling-heavy. The contract — schemas, hash
algorithm, validation rules — lives in
`/home/jpcg/crypto-trading-engine/docs/INTERFACE.md`.

**Decision.**
A file-based contract under `data/exports/` produced by
`crypto/exports/`. Six production-relevant choices made on the MHDE
side this session:

1. **Predictions source — re-score full universe in export script.**
   `crypto_ml_predictions` is filtered/capped (max 15 per horizon) by
   `score_universe()`'s adaptive-threshold logic. INTERFACE.md §3
   requires the full ranked list of predictable candidates. Solution:
   the export script does its own inference on the active 10d model
   bundle. It does not write to DB; the existing prediction pipeline
   is unchanged.

2. **Backtest expectations methodology — portfolio-realistic.**
   `report.simulate_portfolio` results (engine compares paper-trade
   portfolio P&L against these). Sum-of-fractions metrics in
   `crypto_backtest_summary` are docs-flagged as ranking-only and
   inflate absolute values; using them for divergence checks would
   compare the wrong methodology. Unit transforms pinned by tests:
   `portfolio_max_dd_pct` is passthrough fraction (despite the
   `*_pct` suffix in `PortfolioResult`); `expected_annualized_return_pct`
   multiplies by 100 to match INTERFACE.md's percentage-value form.
   Both bugs were caught by spec review during T5 and fixed in commit
   `2d018fb`.

3. **Risk envelope — adopt INTERFACE.md §2 example values for $1k
   Phase 2 paper trading.** `max_account_drawdown_pct=0.30`,
   `daily_loss_limit_usd=100`, `position_size_min_usd=5`,
   `position_size_max_pct=0.20`. Revisit at Phase 3 → 4 transition
   once paper trading shows real friction.

4. **Static config home — `crypto/exports/spec_config.py`.** Phase 1B
   winner `run_id` plus risk/sizing/runtime/universe constants live
   here as Python constants. Phase 1B re-runs require an explicit
   edit + commit. Git history is the audit trail.

5. **Cross-repo hash compatibility — shared JSON fixture in engine
   repo.** A single test-vector file at
   `crypto-trading-engine/tests/fixtures/specs/hash_test_vectors_v1.json`
   is read by both sides' tests. MHDE's parity test resolves the
   path via `MHDE_ENGINE_REPO` (default
   `/home/jpcg/crypto-trading-engine`) and skips with a clear message
   when the engine repo isn't present. Engine-repo coordinated
   changes (creating the fixture, INTERFACE.md §2.4 path
   documentation, engine `test_hash.py` update) tracked separately.

6. **Preflight gate — staleness only, not per-symbol coverage.**
   The first design called for a strict 100% coverage gate on top
   of staleness. The first production run (KI-129) demonstrated this
   was over-strict: newly-added universe symbols are in their 60-day
   features warmup window (longest lookback feature is `return_60d`)
   and have zero feature rows by design. Loosened to staleness-only
   in commit `ef0f12a`. `n_predictions` reflects the predictable
   subset of the active universe; INTERFACE.md §3 doesn't mandate
   `n == universe_size`.

Plus three smaller decisions:
- `data/exports/` gitignored (operational artifact, mirrors
  `data/reports/` policy from commit `0f04fc5`).
- Daily predictions timer fires 06:15 UTC, 7 days/week, between the
  existing crypto predict timer (00:30 UTC) and the engine's 06:30
  UTC entry phase. The export's preflight enforces freshness; systemd
  ordering is informational.
- Symlink replacement is atomic (write tmp + `os.replace`). Replaces
  pre-existing files silently for initial bootstrap.

**Consequences.**
- New module `crypto/exports/`, two new CLI commands
  (`crypto export-spec`, `crypto export-predictions`), one new
  systemd timer (`mhde-crypto-export-predictions.timer`).
- Phase 1B re-runs become a deliberate two-step ritual: re-run the
  grid, then edit `spec_config.PHASE1B_WINNER_RUN_ID` and commit.
- No DB schema changes; export reads existing tables only.
- 42 tests under `tests/crypto/exports/` (1 skip until engine-repo
  fixture lands).
- Engine-side coordinated changes (test fixture file, INTERFACE.md
  §2.4 path documentation, engine's `test_hash.py` update) are
  tracked separately in the engine repo.


---

## ADR-018 — Weekday/forex-closed gates for health_check + pipeline_execution

**Date:** 2026-05-10
**Session:** KI-128 fix
**Status:** Active
**Builds on:** ADR-015 (asymmetric pipeline_execution recency budgets)

**Context.** ADR-015 widened equity's `pipeline_execution`
RECENCY_BUDGET to 75h to absorb the Fri→Tue weekend roll. Two
sibling code paths still tripped on weekends:

- `pipelines/health_check.py::_check_equity` queried for
  `prediction_date = (now - 1d).date()`, which silently asked for
  Sat or Sun rows on Sun/Mon mornings — neither exists because
  NYSE is closed.
- `pipelines/health_check.py::_check_fx`,
  `monitoring/pipeline_execution.py` (FX leg), and
  `pipelines/freshness.py::check_fx_freshness` used a fixed 2h
  budget that fails through the entire forex weekend close
  (Fri 22:00 UTC → Sun 22:00 UTC).

Result: a predictable Telegram false alert every weekend.

**Decision.** Add `pipelines/market_calendar.py` as a single source
of truth and gate the three call sites on its helpers:

| Helper | Used by |
|---|---|
| `expected_equity_prediction_date(now)` — most recent Mon-Fri strictly before `now.date()` | `_check_equity` |
| `is_forex_closed(now)` — True iff Fri 22:00 UTC ≤ now < Sun 22:00 UTC | `_check_fx`, `pipeline_execution` FX leg, `check_fx_freshness` |
| `fx_close_floor(now)` — Fri 21:00 UTC of the active closure (the last bar timestamp expected before close) | same three call sites |
| `trading_days_between(start, end)` — moved from `freshness.py` | `check_equity_freshness` |

During `is_forex_closed(now)`, the gate becomes
`latest >= fx_close_floor(now)` instead of the wall-clock budget.
This preserves outage detection during the close (a real ingestion
failure that started before Fri 22:00 UTC still fails the gate).

**Why `fx_close_floor` returns Fri 21:00 UTC, not Fri 22:00 UTC.**
MHDE's `fx_prices_hourly.datetime_utc` stamps each hourly bar at
the START of the hour it covers. The bar covering 21:00–22:00 UTC
trading has `datetime_utc = 21:00:00`, finalizing at 22:00 UTC.
That bar is the last one that exists before forex closes. If the
floor were the close moment (22:00 UTC), `latest >= floor` would
make a healthy system look stale (21:00 < 22:00). Centralizing
this offset in the helper keeps each caller comparison clean
(`latest >= fx_close_floor(now)` with no per-caller adjustment).

**Holidays remain operator-acknowledged.** Mirrors ADR-015's
trade-off. NYSE-closed Fridays (Thanksgiving, Good Friday) and
Mondays (MLK, Memorial Day) will produce one warn the day after
because the helper expects a "weekday", not a "trading day". Adding
a holiday calendar (e.g. `pandas_market_calendars`) was rejected:

- Adds a runtime dependency for ~10 noise-suppressed days/year.
- The holiday list itself drifts (markets occasionally announce
  closures) and would need maintenance.
- A holiday warn is informational anyway — operator notes the
  date and moves on.

**Why not raise FX `RECENCY_BUDGET` to 50h to absorb the weekend?**
Weakens active-hours outage detection: a real FX ingestion failure
on Tuesday wouldn't alert until Thursday. The gate-on-window
approach keeps the active-hours budget at 2h.

**Why not add `row_inserted_at TIMESTAMP` and key recency off
write time?** Same answer as ADR-015: better long-term, requires
schema migration, out of scope here.

**Configurability.** No env var. Adding a kill-switch creates a
"forgot to set it back" failure surface and these market hours
don't change.


---

## ADR-019 — Crypto retrain validation gate: single-arm hit-rate (post-design-revision)

**Date:** 2026-05-10
**Session:** Gap 1 — three-gap observability plan
**Status:** Active
**Builds on:** ADR-016 (crypto ML model lifecycle)

**Context.** Before this branch, `crypto/ml/train.py` auto-flipped
`is_active=true` on every newly-trained crypto model with zero
validation. Today's retrain promoted `crypto_10d_7760a3f6` and
`crypto_5d_ac900cbf`; Phase E paper trading would have used them on
the next entry-phase fire. A regression in either model (training data
corruption, feature pipeline issue, degenerate solution) would have
silently degraded live trading with no alert and no rollback.

**Decision.** Insert a validation gate (`crypto/ml/validation_gate.py::validate_promotion`)
between model training and the `is_active` flip. The gate compares
the new model's label hit rate (`precision_at_threshold` stored on
`crypto_ml_model_runs` at training-time CV) against the
previously-active model's, gating promotion at
`new >= 0.9 × old`. On fail: keep old active, mark new
`promotion_status='promotion_blocked'`, dispatch a critical-severity
Telegram alert via `monitoring.alert.send_alert`. On pass: demote
old, promote new with `promotion_status='promoted'`. Always emit a
structured JSON log line (`event="retrain_validation"`) with the
comparison metrics.

**Journey (operator requested this record explicitly).**

1. *Initial design:* two-arm gate — label hit rate AND walkfold trade
   Sharpe, both at 0.9× threshold. Operator pre-approved a 10-min
   validation timeout for the heavyweight Sharpe path, with the rule
   "if >30 min in practice, propose async with auto-rollback."

2. *Discovery during Task 1.2 (Sharpe sim extraction):*
   `strategy_edge_analysis.py`'s headline Sharpe (5.10 in
   `active_spec.json`) comes from `simulate_portfolio` on
   `crypto_backtest_trades` for one specific completed backtest run
   (`backtest_10d_D_top_n_a02e15a0`), NOT from any per-model
   walkfold computation. The script had no walkfold-level Sharpe
   extractor to lift.

3. *Operator decision after Task 1.2:* proceed with a new
   gross-Sharpe-from-predictions function
   (`compute_walkfold_trade_sharpe` at `crypto/ml/sharpe_sim.py`)
   that reads `crypto_ml_predictions` directly with the same sizing
   constants (0.8/6 per position, top-6 daily picks). Gate would
   compare this metric model-to-model at 0.9× threshold. Explicitly
   NOT numerically comparable to the 5.10 in `active_spec.json` —
   that is a portfolio metric for a locked Phase 1B strategy on one
   specific backtest run; the gate metric would be a per-model
   comparator decoupled from execution friction.

4. *Discovery during Task 1.3 spec review:* walkfold predictions in
   `crypto_ml_predictions` are tagged with per-fold model_ids
   (`crypto_10d_walkfold_2024_03_*` per `model_id_for_fold(horizon,
   test_start)` in `backfill_walkforward.py`), never with the
   production model_id. The gate's
   `compute_walkfold_trade_sharpe(conn, new_model_id, horizon)` query
   returns zero rows for any production model_id; the
   degenerate-baseline rule then trivially passes. Sharpe arm is a
   no-op in production.

5. *Operator decision after Task 1.3 spec review:* drop the Sharpe
   arm entirely. Single-arm gate on hit rate. Reasoning: hit rate
   catches the main failure modes (training data corruption, feature
   pipeline issues, degenerate solutions); marginal
   worse-Sharpe-same-hit-rate cases are hard to distinguish from
   natural variance anyway; don't over-engineer upfront.

**Final design.**

- Gate: `validate_promotion(conn, new_model_id, horizon) → ValidationResult`.
- Single SELECT against `crypto_ml_model_runs.precision_at_threshold`
  for both old (previous `is_active=true`) and new. No backfill. No
  timeout machinery.
- Threshold: `new_hit_rate >= 0.9 × old_hit_rate`.
- Bootstrap rule: no prior active → pass with
  `reason="first_model_skip"`.
- Degenerate rule: `old_hit_rate <= 0` → arm passes (no positive
  baseline to defend).

**Consequences.**

- Catches catastrophic model failures where hit rate collapses by
  more than 10%.
- Does NOT catch failures where hit rate stays roughly equal but
  trade quality / risk-adjusted return degrades.
- `compute_walkfold_trade_sharpe` at `crypto/ml/sharpe_sim.py`
  remains as a utility module for ad-hoc Sharpe analysis but is
  unused by the gate.

**Escape valve.** If hit-rate-alone proves too forgiving in practice
(a bad model slips through and causes losses), add an AUC arm in a
separate PR. `auc_roc` is also stored on `crypto_ml_model_runs`
(training-time CV mean) and is directly comparable across models.
AUC + hit rate at 0.9× each is the natural next-step composite gate
if needed.

**Reference commits:** `2a666cd` (schema), `70563ed` (sharpe
utility), `7eca751` (initial two-arm gate), `222345d` (drop Sharpe
arm), `b584e2a` (train.py wiring).

## ADR-020 — Monitoring may read the engine DuckDB read-only (scoped exception to INTERFACE.md)

**Date:** 2026-05-11
**Session:** Gap 2 — three-gap observability plan
**Status:** Active
**Builds on:** ADR-017 (engine-export contract: file-based, no DB/API coupling)

**Context.** `INTERFACE.md` §1 states the MHDE↔engine contract is "two
files … no database access, no API calls, no code coupling." Gap 2
adds a paper-trading drift monitor (`monitoring/paper_trading_drift.py`)
that needs the engine's runtime state — `engine_runs` (liveness),
`positions` / `orders` (closed-trade win rate, stuck-position
detection) — which the engine does not (and should not) re-export as a
file. The only alternatives were (a) having the engine emit yet another
daily summary JSON, adding a coordinated cross-repo schema, or (b)
reading the engine's DuckDB directly.

**Decision.** Option (b), as a deliberate and *narrowly scoped*
exception: MHDE monitoring code may open the engine's DuckDB
**read-only** (`duckdb.connect(path, read_only=True)`), path from the
`CRYPTO_ENGINE_DB_PATH` env var (default
`/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`).
Constraints that keep this from becoming a real coupling:

1. **Read-only, always.** Monitoring never writes to the engine's DB.
   The engine remains the sole source of truth for its own state.
2. **Monitoring only.** This exception covers `monitoring/` — not the
   prediction pipeline, not exports, not the dashbord write paths. The
   file-based contract (`active_spec.json`, `predictions_*.json`) is
   still the *only* channel through which MHDE *influences* the engine.
3. **Tolerant of schema drift.** Monitor queries select a minimal
   column subset and degrade to a `warn`-severity "check errored"
   finding rather than crashing if the engine's schema moves under
   them. A schema change on the engine side does not require a
   coordinated MHDE commit (unlike the file contract).
4. **No reverse dependency.** The engine does not know the monitor
   exists; nothing in `crypto-trading-engine` imports or references
   MHDE.

The engine repo's `docs/INTERFACE.md` §1 should carry a one-line
back-reference to this ADR ("monitoring may read this DB read-only —
see MHDE DECISIONS.md ADR-020") so a future reader doesn't flag the DB
read as a contract violation. That is a doc-only edit in the *engine*
repo and is left for a coordinated commit there; it does not block this
MHDE change.

**Consequences.** If the engine ever moves to a non-DuckDB store or a
remote DB, this monitor's `_open_engine_db()` is the single point that
changes. The dashboard's Gap 3 paper-trading tab is expected to take
the same read-only-DuckDB approach under this ADR.

**Addendum (2026-05-31).** The dashboard Paper-Trading tab now reads
`orders`, `funding_log`, and `engine_runs` read-only (in addition to
`positions` / `price_snapshots`) for the today's-cohort Funding /
Commission / Net PnL columns; same constraints apply.

---

## ADR-021 — Post-parabolic exclusion filter at the prediction-export step

**Date:** 2026-05-11
**Session:** post-parabolic filter (after the SKYAI diagnostic + OHLCV repair)
**Status:** Active
**Builds on:** ADR-017 (engine-export contract: file-based, MHDE-side)

**Context.** The crypto model re-emits buy signals on coins immediately
after a parabolic crash — confirmed on clean data (SKYAI: calibrated
probabilities 0.72–0.88 across the crash window). Root cause is in the
model, not the data: the `label_Nd_10pct` target ("did the price tag
+10% above today's close within N days") rewards volatility regardless
of direction, and the momentum-lag features (`return_60d`,
`drawdown_from_90d_high`) keep reading bullish for weeks after a top.
The model's probability is *not wrong* — such coins really do tag +10%
— it is optimising the wrong objective for a risk-aware entry signal.

**Decision.** Add a deterministic pre-order-entry **risk gate** rather
than touch the model:

1. **Where:** the MHDE prediction-export step
   (`crypto/exports/write_daily_predictions.py::build_predictions`),
   right after Platt calibration, before ranking. Not at emit
   (`crypto_ml_predictions` keeps the full raw signal for
   diagnostics/backtest — untouched), not in the engine (the file
   interface doesn't carry the needed features, and that would be a
   coordinated cross-repo change). Single-repo, no change to the
   `predictions_*.json` schema (excluded coins are dropped and ranks
   renumbered consecutively — the engine's existing validation holds).
2. **Rule (hard exclusion, not a probability haircut):** drop a coin
   iff **both** `drawdown_from_90d_high < -0.20` **and**
   `return_60d > 2.0` (strict; constants `POSTPARABOLIC_DD90_THRESHOLD`
   / `POSTPARABOLIC_RET60_THRESHOLD` in `crypto/config.py`). Hard
   exclusion because this is a risk decision, not a probability
   adjustment — a `0.3×` haircut would be an arbitrary multiplier that
   interacts badly with the engine's top-N selection on thin days and
   makes the calibration meaningless for that coin. Same thresholds for
   5d and 10d (the bias and the two features are horizon-independent;
   the engine consumes 10d only today). Backed by a 60-day historical
   scan: the rule fires on 0.8% of predictions, isolating the
   high-drawdown tail (excluded set avg max-DD −25% vs retained −4%);
   on the daily top-6 it never removed more than 2 picks on any day.
3. **Fail-open:** if `drawdown_from_90d_high` or `return_60d` is
   NULL/NaN (warmup window), the coin is *not* excluded.
4. **Observability:** every exclusion is `logger.warning`-ed and
   UPSERTed into the new `crypto_signal_exclusions` table
   `(export_date, symbol, model_id, raw_probability, dd90, ret60,
   reason)`. An all-excluded day yields an empty predictions list (the
   exporter does not crash) — the engine then skips entry + alerts per
   INTERFACE.md §3.2 / §5.3. A dashboard expander and a cross-repo
   `excluded_postparabolic` JSON field were scoped out (phase 2).

**Consequences.** Thresholds can be retuned by editing two constants
(the scan supports −0.15/+1.5 as a more-aggressive alternative). The
root-cause model fix (a direction-aware / risk-adjusted label) remains
the open follow-up — tracked as KI-137. If graceful degradation is
ever wanted instead of hard exclusion, the right lever is a cap on
concurrent post-parabolic exposure, not a per-coin haircut.

---

## ADR-022 — OHLCV plausibility / volume-cliff guard in the daily crypto pipeline

**Date:** 2026-05-11
**Session:** data-quality guard (after the 2026-05-07 partial-candle incident + repair)
**Status:** Active

**Context.** On 2026-05-07 the OHLCV ingestion started writing
~30-minute *partial* candles for all 50 symbols (the bug fixed in
`backfill_ohlcv.py` — `INGESTION_LAG_DAYS` / `REFETCH_WINDOW_DAYS`).
The existing monitors (`monitoring/data_quality.py`, `pipelines/freshness.py`)
only check row *presence* and `MAX(trade_date)` recency — all 50 rows
were present and the date was current, so nothing fired; the corrupt
data flowed into features → predictions → the engine export for four
days. We need a check on row *plausibility*, run inline so it can
*block* propagation, not just a passive after-the-fact monitor.

**Decision.** New pure module `pipelines/data_quality_guard.py`:
`check_ohlcv_plausibility(conn, target_date) -> QualityReport`. For each
symbol with a row on `target_date` and a full 20-day prior window, flag
it if today's **volume** or **trade count** is below 10 % of its
trailing-20-day median, or its **(high − low) range** is below 20 % of
the median range (each check independent; constants in
`crypto/config.py`). The day is **systemic** iff ≥ 10 symbols are
evaluable *and* more than 30 % of them are flagged. Pure: reads only,
writes nothing, sends nothing, never exits.

A new CLI command `crypto check-data-quality` wraps it: persists the
flagged rows (UPSERT) into the new `crypto_data_quality_reports` table,
sends a Telegram alert (CRITICAL if systemic, WARN if per-symbol-only),
and on a systemic flag **exits non-zero**. It is inserted as the second
`ExecStart=` in `mhde-crypto-predict.service` — right after
`backfill-prices` — so (`Type=oneshot`) a systemic anomaly aborts the
unit and every step below it (backfill-funding/oi/labels/features/predict).
Per-symbol-only anomalies WARN and do not block. Escape hatch:
`MHDE_DATA_QUALITY_GUARD_OVERRIDE=1` (unit env var) → never block.

**Threshold rationale.** Tuned on a 90-day post-repair clean-data scan
(4 367 symbol-days, 50 symbols): the clean 1st-percentile volume ratio
is ~0.22, so a 0.10 cliff threshold sits well below organic quiet days
(per-symbol WARN rate ≈ 0.55 %, ~1 every few days for *some* coin —
informative, not spammy). **Zero systemic false positives at every grid
combo** (0.05–0.20 volume × 0.10–0.30 range): the clean-day maximum is
~10 % of the universe flagged, far under the 30 % systemic threshold. A
reconstruction of the 2026-05-07 corruption (real Binance volumes,
~2.5 %-volume partial candles vs the clean 20-day baseline) flags
≈64–96 % of the universe each of 05-07…05-11 at *every* combo →
systemic fires on day one with a large margin. So the chosen pair
(0.10 / 0.20) catches the bug and is quiet on clean data; 0.05 / 0.10 is
a near-silent conservative alternative.

**Consequences.** A genuine multi-symbol data-source incident (e.g. an
exchange outage affecting > 30 % of the perps for part of a day) will
also block the pipeline that day — which is correct (don't compute
predictions on bad data). Single-symbol thin-trading days produce WARNs
only. Thresholds are five constants in `crypto/config.py`. A dashboard
view of `crypto_data_quality_reports` is a deferred phase-2 item. The
deployed unit needs `systemctl daemon-reload` after this merge.

---

## ADR-023 — Knockout (triple-barrier) crypto label (phase 1: label + backfill, additive)

**Date:** 2026-05-11
**Session:** knockout label, phase 1 (after the post-parabolic filter / data-quality guard)
**Status:** Active — phase 1 (label code, schema, backfill). Phase 2 (training a knockout model, the validation/promotion path) is a separate task.
**Spec:** `crypto/ml/KNOCKOUT_LABEL_SPEC.md` (operator-approved 2026-05-11).

**Context.** The legacy crypto label `label_Nd_10pct = (max forward daily
close / close − 1) ≥ 0.10` is direction-agnostic and volatility-rewarding —
a coin in freefall still "wins" because it tags +10 % at *some* point — which
is the root cause of the SKYAI false-positive class (probabilities honest
w.r.t. the label, useless as risk-aware entry signals; mitigated by the
post-parabolic filter, ADR-021, which is the *symptom* guard). A knockout
(triple-barrier) label scores what a trader cares about: take-profit hit
*before* stop-loss, within the horizon.

**Decision (phase 1).** Add a knockout label alongside (not replacing) the
legacy one. `crypto/ml/knockout_label.py` — pure
`knockout_classify(forward_highs, forward_lows, entry_close, tp, sl, horizon,
sl_first=True) -> (outcome, resolve_day)`: walk forward bar by bar over up to
`horizon` bars; intraday HIGH ≥ `C·(1+tp)` first → `"tp"` (WIN); intraday LOW
≤ `C·(1+sl)` first → `"sl"` (LOSS, `sl` < 0); both in one bar → `sl_first`
tiebreak; no touch within `horizon` → `"neither"` → classified as a LOSS.
Parameters (operator-approved): **TP = +0.10, SL = −0.05** (same for both
horizons), horizons **5d and 10d**, **neither → loss**, **same-bar → SL-first
(pessimistic)** — constants `KNOCKOUT_TP` / `KNOCKOUT_SL` in `crypto/config.py`.
`crypto/ml/labels.py::compute_labels` gains a forward-walk pass
(`_compute_knockout_labels`) — the existing close-based INSERT can't express
first-touch ordering — that populates six new `crypto_ml_labels` columns:
`label_{5d,10d}_knockout BOOLEAN` (= `outcome == 'tp'`),
`knockout_outcome_{5d,10d} VARCHAR` (`'tp'|'sl'|'neither'`),
`knockout_resolve_day_{5d,10d} INTEGER` (1-indexed, NULL for `'neither'`).
Additive `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migrations; the
`INSERT INTO crypto_ml_labels` is changed to an explicit column list so the
new columns don't break it. Backfill runs in the existing `crypto backfill-labels`
step (no new pipeline stage, no systemd change) in ~sub-second.

**Backfill result (full window, 27,483 label rows):** label base rates —
`label_5d_knockout` 23.1 % (`tp` 23.1 % / `sl` 60.0 % / `neither` 16.9 %),
`label_10d_knockout` 27.8 % (`tp` 27.8 % / `sl` 65.5 % / `neither` 6.7 %) —
vs the legacy `label_10d_10pct` 35.8 %; the gap is the volatility-loving
false-wins the knockout label purges. Median resolve day: `tp` ≈ 2, `sl` ≈ 1–2.
The SKYAI 2026-05-10-close entry classifies as `'sl'` on day 1 (the
2026-05-11 low pierces the −5 % barrier before the high reaches +10 %).

**Consequences.** The legacy `label_Nd_10pct` columns are kept (trained
models, `fill_outcomes`, the dashboard accuracy panel, `phase0_evaluate` still
read them) — keeping both enables an A/B transition: phase 2 trains a knockout
model, runs it side-by-side, and gates it on the promotion criteria in the
spec §5 (walk-forward precision floor, calibration, a backtest verdict,
operator sign-off for the first one) before any `is_active` flip. Phase 1
touches nothing downstream (no `train.py` / `predict.py` / model artifacts /
dashboard / validation_gate change). Retuning TP/SL requires a full label
re-backfill (same as the legacy label's implicit 0.10).

---

## ADR-024 — Knockout label phase 2: training path, validation methodology, and the "hold" verdict

**Date:** 2026-05-11
**Session:** knockout label, phase 2 (training + validation + paired backtest)
**Status:** Active — methodology adopted; the trained models are **NOT promoted** (verdict below).
**Builds on:** ADR-023 (the knockout label), the spec `crypto/ml/KNOCKOUT_LABEL_SPEC.md` §5 (promotion criteria).

**Decision (training path).** `crypto/ml/train.py::train_walk_forward` gains a
`label_kind ∈ {legacy, knockout}` parameter (and an `auto_promote` flag);
`crypto train --label-kind knockout` trains a 5d and a 10d model on
`label_Nd_knockout` with `auto_promote=False` — the new `crypto_ml_model_runs`
rows are `is_active=false, promotion_status='pending', label_kind='knockout'`,
the validation gate (`validate_promotion`) is **not** invoked, and nothing is
flipped active. `model_id` is prefixed `crypto_{horizon}_knockout_…`; the joblib
bundle carries `label_kind`, `knockout_tp`, `knockout_sl`. A new `label_kind`
column on `crypto_ml_model_runs` (idempotent migration; existing rows default
`'legacy'`). The execution-backtest harness gains a `model_id_like` parameter
(default `'crypto_%_walkfold_%'`) so a paired A/B backtest can replay an
alternative walk-forward set without contaminating the baseline; phase 2 wrote
the knockout walk-forward OOS probabilities to `crypto_ml_predictions` as
`crypto_{horizon}_kowf_{YYYY_MM}` (deliberately *not* `…_walkfold_…` so the
default pattern excludes them). No change to `predict.py`,
`write_daily_predictions.py`, `validation_gate.py`, the engine repo, or the
dashboard.

**Validation methodology (spec §5).** Each retrained knockout model is judged
on: **C1** walk-forward precision-at-threshold (avg top-5% precision over folds)
≥ 0.40 absolute floor; **C2** calibration bucket check (predicted P ≈ realized
rate within ±0.15 per bucket with ≥10 samples); **C3** a paired execution
backtest vs the current legacy active models (Phase-1B-winner config — Policy D,
top_n n=6, trail 0.3 — post-parabolic filter OFF): Sharpe ≥ legacy − 0.5, max-DD
no worse, cumulative return within ±10% (or higher); plus a bonus diagnostic —
the fraction of each model's daily top-6 picks that trip the post-parabolic
`should_exclude` predicate (lower = the label organically avoids the SKYAI
profile). **The first** knockout model bypasses the auto gate and requires
explicit operator sign-off before any `is_active` flip.

**Phase-2 results & verdict — HOLD (do not promote either model).**
- C1: 5d precision 0.428 (PASS, barely); 10d 0.394 (FAIL, barely). Knockout
  AUC ≈ 0.55–0.57 — barely above random; the legacy label's apparent edge was
  partly the model exploiting "volatility", which the knockout label removes by
  design, so the new label is genuinely harder to learn with the current
  momentum/vol-heavy 35-feature set.
- C2: **FAIL both.** Lower probability buckets are roughly calibrated, but the
  upper buckets are wildly over-confident — e.g. 10d bucket [0.8,0.9) predicts
  0.84, realizes 0.30 (|diff| 0.54). The per-fold Platt fit on a 20% within-fold
  split does not generalise to the OOS distribution for this harder label.
- C3: **FAIL both, mixed.** 5d: Sharpe drops 6.10 → 4.39 (one 7.6× outlier
  blows up the return std), max-DD slightly better (−9.1% → −7.9%), cumRet
  ~+3.4 pp — net a regression. 10d: Sharpe 6.25 → **7.36** and max-DD
  −17.0% → **−10.7%** both *improve*, but cumRet 52.2% → 44.2% (≈ −15%, outside
  ±10%) — the 10d knockout trades a chunk of upside for materially lower risk.
- Bonus: knockout top-6 picks trip the post-parabolic filter ~half as often
  (5d 1.1% vs 2.2%; 10d 1.0% vs 2.4%) — the label *is* doing what it should.

**Consequences / next steps.** The direction is right (organically avoids the
post-parabolic profile; 10d risk metrics improve) but the execution isn't there:
the calibration is broken and the raw discriminative power is weak. Recommended
phase-3 retry before any promotion: (a) fix calibration (isotonic on a held-out
window, or a larger / dedicated calibration set, instead of the within-fold
20% Platt); (b) reconsider the TP/SL pair — the spec's scan showed a wider band
(e.g. +15%/−7%) has a higher `tp` base rate and may be more learnable; (c) add
directional features (the current set is momentum/vol-heavy, which the knockout
label penalises); (d) re-run the paired backtest. Until then, the post-parabolic
exclusion filter (ADR-021) remains the working symptom-guard — the knockout
label is **not yet** a replacement. The trained models stay persisted but
inactive; the `crypto_*_kowf_*` walk-forward predictions stay in
`crypto_ml_predictions` as a reproducibility artifact (and can be deleted if
they clutter the dashboard's historical view).

---

## ADR-025 — Prediction export decouples "trading date" from "features-as-of date" (option A); schema-level unification deferred (option B)

**Date:** 2026-05-12
**Session:** export-preflight regression fix after the cap-at-today-1 ingestion change
**Status:** Active (option A shipped); option B deferred
**Builds on:** ADR-022 (the cap-at-today-1 ingestion fix, commit `8f9d707`), ADR-010 (freshness guards), KI-138, INTERFACE.md §3.

**Context.** ADR-022's ingestion fix made `MAX(trade_date)` in
`crypto_prices_daily` / `crypto_ml_features` structurally `today - 1`
(only fully-closed UTC days are ingested). The prediction exporter
(`crypto/exports/write_daily_predictions.py`) had a single date,
`prediction_date` (default `today` UTC), doing double duty: the
freshness gate required `MAX(trade_date) == prediction_date`, and the
JSON `export_date` was set to `prediction_date`. Post-`8f9d707` the gate
could never pass on a normal day, so `crypto export-predictions` aborted
daily, `predictions_latest.json` went stale, and the engine rejected it
(`export_date != today_utc`, INTERFACE.md §3.2). No positions were
placed. See KI-138.

**Decision (option A — shipped).** Separate the two notions explicitly
in the exporter:
- **Trading date** = `export_date` in the JSON = `today` UTC (unchanged
  meaning per INTERFACE.md §3.1: "the trading date these predictions
  apply to"). This is what the engine validates.
- **Features-as-of date** = `MAX(trade_date)` in `crypto_ml_features`,
  validated by `_check_freshness` to be `export_date` **or**
  `export_date - 1` (the cap-at-today-1 normal). Anything older still
  aborts — that's genuine pipeline staleness. `_check_freshness` returns
  this date; `build_predictions` loads features for it.
- The JSON gains an informational `features_as_of_date` field for
  downstream consumers / debugging. The engine loader is **unchanged**
  and does not read it; adding an optional field to the daily
  predictions file (which, unlike `active_spec.json`, is not
  hash-canonicalised) is backward-compatible. INTERFACE.md §3 should be
  updated to document the new optional field — tracked as a doc
  follow-up.
- `crypto_signal_exclusions.export_date` and the per-prediction
  `predicted_at` continue to use the trading date — unchanged.

**Why option A and not option B now.** `crypto_ml_predictions.prediction_date`
(written by `crypto/ml/predict.py:score_universe`, = `MAX(trade_date)` =
`today - 1`) is semantically the *features / entry* date, used as such
by the outcome-fill join (`pred.prediction_date = entry.trade_date`). It
now differs from the export's `export_date` by a day. The exporter does
its own inference and never reads `crypto_ml_predictions`, so there is no
operational conflict — the engine gets a correct, fresh file. Fixing the
`prediction_date` dual-meaning properly means a schema change (carry both
dates, or rename), touching the predict pipeline, outcome-fill, the
dashboard, and the regression tests around them — out of proportion to
this incident, which option A fully resolves. Deferred as option B
(KI-138 open follow-up).

**Consequences.** On a normal day the predictions file now reports
`export_date` one day ahead of `features_as_of_date`; consumers that
assumed they were equal must read the right field. The freshness gate is
one day more lenient — a pipeline that silently stopped *exactly* one day
ago will no longer be caught by this gate alone (it is still caught by
`pipelines/freshness.py` recency budgets and the daily data-quality
guard, ADR-022). A pipeline ≥2 days stale still aborts the export. No
systemd / engine / dashboard changes; no migration.

---

## ADR-026 — Per-pipeline monitor: one Telegram message, outcome-based step checks, cross-DB reads, separate timers

**Date:** 2026-05-12
**Session:** pipeline-monitoring build (`feat-pipeline-monitoring`)
**Status:** Active
**Builds on:** ADR-020 (read-only cross-repo engine-DB access), ADR-022 (cap-at-today-1 ingestion), ADR-025 / KI-138 (the regression that motivated this), the Session-6 monitor framework (`monitoring/alert.py`).

**Context.** On 2026-05-11/12 the prediction-export staleness gate broke
(KI-138): every step of the crypto pipeline exited 0, `crypto export-predictions`
failed (or, depending on the day, the engine's entry phase ran cleanly and just
placed nothing), `predictions_latest.json` froze, the engine rejected it on
`export_date != today_utc`, and no positions were opened. None of the existing
monitors caught it for ~24h: the daily health-check is once-a-day and the
`pipeline-execution` monitor checks row counts, not "did today's export point at
today's file" or "did the engine actually enter". The operator wanted a single
daily message per pipeline that shows *every* step's outcome at a glance —
🟢 / 🔴 / ⚪ — so a regression of this shape is obvious the next morning, plus
a continuous (every-30-min) monitor for the things that can't wait a day.

**Decision.**
- **New package `monitoring/pipeline_monitor/`.** `core.py` defines `Status`
  (`GREEN`/`RED`/`SKIPPED`), `StepResult`, `PipelineResult`, `evaluate_steps`
  (runs `(name, callable)` checks; a raise → RED; `stop_on_red` short-circuits),
  and `render_telegram_message`. `checks/{crypto,equity,fx}.py` hold one check
  function per step. `daily_runner.py` runs one pipeline's checks and posts one
  message; `continuous_runner.py` runs the continuous checks and posts only if
  any is red.
- **Outcome-based, never exit-code-based.** Every check reads the DB / a file
  directly — `MAX(trade_date)` advanced, rows exist for the expected date,
  `actual_hit` non-NULL for matured predictions, `predictions_latest.json`'s
  `export_date == today`, the engine's `entry` phase ran today and positions
  exist with `entry_date == today`. The 2026-05-11/12 regression had every
  script exit 0; only an outcome check catches it.
- **Date conventions baked into the checks (not re-derived per call site).**
  Crypto OHLCV / features / predictions are produced for `today - 1` under
  cap-at-today-1 (ADR-022); the export's `export_date` is `today` and
  `features_as_of_date` is `today - 1` (ADR-025). Equity uses
  `pipelines.market_calendar.expected_equity_prediction_date(now)` (weekend-rolling
  "most recent closed market day"). FX reuses `pipelines.freshness.check_fx_freshness`
  (forex-closed-window aware, KI-128).
- **Strict linear cascade in the daily runner.** The production pipelines are
  sequential ("each step's output is the next step's input" — ARCHITECTURE.md),
  so the first RED short-circuits: every later step is reported ⚪ ("skipped — an
  earlier step failed") without being evaluated. The continuous monitor's checks
  are independent → no cascade.
- **Cross-repo engine-DB read, read-only.** The crypto daily monitor and the
  continuous monitor read the crypto-trading-engine DuckDB (`CRYPTO_ENGINE_DB_PATH`,
  default `/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`) read-only —
  the same scoped exception to INTERFACE.md's "no cross-system DB access" that
  ADR-020 made for the paper-trading-drift monitor and the dashboard's Paper-Trading
  tab. The engine repo is **not** modified.
- **Reuse the `monitoring/alert.py` Telegram bridge.** A new `alert.send_text(text)`
  sends a pre-formatted message unconditionally (the daily monitor posts every run,
  green or red, unlike `send_alert` which suppresses OK); it still respects
  `MONITORING_DRY_RUN` and logs the payload. Telegram still bottoms out in
  `fx.bot.telegram_bot.send_message`.
- **Separate `.service`/`.timer` per pipeline, not one mega-monitor.** Each
  pipeline finishes at a different time (crypto entry ~06:30 → monitor 06:40 UTC;
  equity predict 00:15 → monitor 01:00 UTC; FX hourly → a daily 12:10 UTC
  snapshot) and a failure in one shouldn't blur the picture for another. The
  continuous monitor is its own `*:0/30` timer. New units:
  `mhde-crypto-pipeline-monitor`, `mhde-equity-pipeline-monitor`,
  `mhde-fx-pipeline-monitor`, `mhde-continuous-monitor`. New CLI:
  `main.py monitor {crypto,equity,fx}-pipeline` and `main.py monitor continuous`.
- **No auto-remediation, no dashboard view in v1.** The monitor reports; the
  operator acts. (KI-139 tracks the v1 limitations.)

**Why not extend the existing `pipeline-execution` monitor.** That monitor is a
recency + row-count drift detector that returns a single `MonitorResult` and only
alerts on anomaly. The operator's ask is the inverse shape: a positive daily
confirmation enumerating every step, with a per-step ⚪ cascade and a cross-repo
view of engine entry. Different output contract, different cadence, different
data sources — a new layer alongside, not a rewrite of, the Session-6 monitors.

**Consequences.** Five new always-on facts per day (one Telegram message per
pipeline + a continuous monitor that's silent unless red); the operator gets a
daily green heartbeat per engine. The equity "dashboard data refresh" step is a
coarse mtime check on a daily-analysis output file with a 4-day tolerance (spans
a weekend + a market holiday) — a 3-day-stale dashboard on a normal week is
*not* flagged by this step alone (KI-139). "0 positions opened today" with no
machine-readable reason is reported RED-with-note (the engine DB carries no
"why" field) — softened to GREEN only when the book is already at `max_concurrent`
(KI-139). The engine `reconcile` timer is not checked (disabled pending
RECONCILE-001; a `CHECK_ENGINE_RECONCILE` flag flips it on). No schema change,
no migration, no engine-repo change, no change to any existing pipeline step.

## ADR-027 — Crypto daily chain (predict → export → engine entry → pipeline monitor) fires as a tight block at 00:30–00:50 UTC, not spread to 06:xx

**Status:** accepted 2026-05-12. Implemented in `systemd/mhde-crypto-export-predictions.timer`
(MHDE) and `systemd/trading-engine-entry.timer` (crypto-trading-engine);
`mhde-crypto-pipeline-monitor.timer` moved to match. No code change.

**Context.** The crypto daily pipeline had a built-in ~6h operational gap:
`mhde-crypto-predict.service` runs at **00:30 UTC** (and reliably completes in
~2 min — observed 2:05–2:40 over the last week, with the occasional +30s DuckDB
write-lock retry), but the downstream steps fired hours later — predictions
export at 06:15, engine `entry` at 06:30, and (since ADR-026) the pipeline
monitor at 06:40. So fresh predictions sat unused for ~6h, and any
overnight breakage in predict surfaced six hours late. The 06:xx times were
historical (chosen before the predict timer was moved to 00:30) with no
remaining reason to keep them.

**Decision.** Move the three downstream timers up so they run immediately after
predict completes, as a chain with generous buffers (≈2× the worst observed
duration of the preceding step, not the theoretical minimum):

| Step | Unit | Old (UTC) | New (UTC) | Buffer over the step before it |
|---|---|---|---|---|
| predict (6-step backfill chain → `crypto predict`) | `mhde-crypto-predict.service` | 00:30 | 00:30 (unchanged) | — |
| predictions export → `data/exports/predictions_latest.json` | `mhde-crypto-export-predictions.timer` | 06:15 | **00:40** | 10 min over predict's ~2–3 min run |
| engine `entry` phase (reads the export, places orders) | `trading-engine-entry.timer` (engine repo) | 06:30 | **00:45** | 5 min over export's ~30–60s run |
| crypto pipeline monitor (verifies the whole chain) | `mhde-crypto-pipeline-monitor.timer` | 06:40 | **00:50** | 5 min over the engine entry's ~1–30s run |

All four keep `Persistent=true` (catch-up after downtime); `mhde-crypto-export-predictions.service`
keeps `After=mhde-crypto-predict.service` so a boot-time catch-up still orders
export after predict.

**Deploy ordering is load-bearing.** The export must be redeployed **before**
the engine entry timer. If entry moves to 00:45 while the export is still at
06:15, the 00:45 entry finds yesterday's `predictions_latest.json`
(`export_date` mismatch / `generated_at` older than the 4h staleness window per
INTERFACE.md §3.2) and skips entry for the day — exactly the failure shape the
engine's stale-predictions guard is meant to catch, but self-inflicted. Correct
order: (1) deploy + activate the new MHDE export timer, confirm it has run and
written today's file; (2) deploy + activate the new engine entry timer; (3)
deploy the new monitor timer. The reverse window (export at 00:40, entry still
at 06:30) is harmless — entry just reads a 5h-old-but-correct file at 06:30
until its timer is updated.

**Not changed.** `active_spec.json` / `crypto/exports/spec_config.py` still
carry `runtime.entry_time_utc: "06:30"` — that field is loaded into the engine's
`RuntimeConfig` but never read by any engine code path (it's documentation), and
editing it changes the spec hash, which forces a coordinated spec-reload +
re-ack on the engine side (INTERFACE.md §4) for no functional gain. It can be
corrected to `"00:45"` in a future routine spec bump; until then treat it as
stale metadata. The predict timer itself (00:30) is unchanged. No change to the
predict / export / entry logic, only their fire times.

**Consequences.** Positions are opened ~5h45m earlier each day, so
trailing-stop monitoring (engine `monitor` phase, runs every minute regardless)
starts on the day's new positions ~6h sooner. A breakage anywhere in
predict→export→entry now surfaces in the 00:50 Telegram message instead of
06:40. Downside: less wall-clock slack if predict ever runs pathologically long
(>10 min) — but `mhde-crypto-predict.service` has `TimeoutStartSec=1800` and a
run that slow is itself an incident the monitor will flag (stale export at
00:50). The export and entry now run while the FX hourly :05 chain and other
:0X jobs are quieter than 06:xx, so DuckDB write-lock contention is, if
anything, slightly lower.

## ADR-028 — Post-parabolic filter v2: add short-window momentum rule (`return_5d < -0.30`)

**Status:** accepted 2026-05-14. Implemented on branch
`feat-postparabolic-add-ret5-filter`. Extends ADR-021 (Rule A); does not
replace it. Single threshold (`POSTPARABOLIC_RET5_THRESHOLD = -0.30`), no
per-model knob; same logic applies universally.

**Context.** Two confirmed live failure patterns surfaced after ADR-021
went live:

1. **SWARMSUSDT 2026-05-14** (the trigger for this ADR) — entered at the
   2026-05-13 prediction (dd90 −50.0%, ret60 +147%, ret5 −36.8%, 9/10 down
   days). Rule A did **not** fire (`ret60 = +1.47` is below the `+2.0`
   parabolic gate), so the coin made the top-6, the engine entered, and the
   position was −22% within 24 hours. The pattern is "already-crashed +
   still-falling", distinct from Rule A's "still-parabolic + just-starting-
   to-crash" target.
2. **4USDT 2026-05-12** — same family by drawdown depth but a different
   failure shape: a recent bounce on top of a 60-day decline (dd90 −43.5%,
   ret60 +60%, ret5 −1.2%). Tested separately and **not** addressed by this
   ADR (see Variant E rejection below).

A loser-characterization study of the 93 deep losses (`net_pnl_pct < −10%`)
in the 941-trade Phase-1B-winner backtest tagged 28 (30%) as the
SWARMSUSDT-class (deep dd90 + active short-window weakness + wide ATR;
worst-class avg loss −27.8%). The class is the single largest named failure
mode below Rule A's parabolic gate.

**Decision.** Add `Rule B: return_5d < POSTPARABOLIC_RET5_THRESHOLD` (=
`-0.30`), OR-combined with Rule A inside the same `should_exclude`
predicate at the same call site (`build_predictions` in
`crypto/exports/write_daily_predictions.py`). Reasons recorded:
`post_parabolic` / `short_momentum` / `post_parabolic_and_short_momentum`.
Per-input fail-open semantics: a warmup-window coin with NULL `return_5d`
is still evaluated by Rule A.

**Threshold rationale (backtest grid; window 2025-04-05 → 2026-05-07; 10d,
Policy D, top_n=6, trail_pct=0.3; 16,679 walkfold predictions).** Three
prior candidate variants were tested and rejected:

| variant | Δ Sharpe | Δ Max DD | Δ cumRet (units) | verdict |
|---|---:|---:|---:|---|
| A: ret5 < -0.20 | -4.14 | -2.54 pp | +21.6 | reject — Sharpe collapse |
| B: down_days >= 7 | -4.02 | -2.30 pp | +26.5 | reject — `down_days` doesn't discriminate (33.8% of winners vs 29.0% of deep losers fire the condition) |
| C: A OR B | -4.09 | -3.35 pp | +24.3 | reject |
| **D: ret5 < -0.30** | **+0.18** | **±0.00** | **−1.05** | **accept (this ADR)** |
| E: dd90 < -0.40 AND -1<ret60<1 | -1.96 | -23.94 pp | -22.20 | reject — over-filters 40% of universe |
| DE: D ∪ E | -1.95 | -23.94 pp | -22.29 | reject |

At `-0.30` the rule fires on 1.52% of universe (253 of 16,679), Sharpe
gains +0.18 (6.32 → 6.51), max DD is unchanged to two decimals, cumRet
drops ~2% relative, hit rate −0.16 pp. The mechanism: a tight filter on a
high-variance subset removes a small loss-prone population without
forcing Top-N to backfill from low-quality rank-7+ candidates. Wider
thresholds force more backfill and destroy Sharpe (variants A/B/C show
this empirically). Variant E demonstrated that targeting the 4USDT-class
via dd90/ret60 alone over-filters: `dd90 < -0.40` matches ~40% of the
universe in a non-bull regime; combined with the trivial ret60 band it
became a regime gate, not a tail filter.

**Why a binary rule and not a probability haircut, why a single global
threshold, why no horizon-specific tuning.** Same reasoning as ADR-021 —
the model's probability is well-calibrated; the issue is risk shape, not
forecast quality. A haircut interacts badly with Top-N (a haircut coin
still tops the field on a thin day, which is exactly when you want it
gone). Horizon-independent: `return_5d` is a 5-day window, and Phase-1B
backs the 10d horizon today; same logic if a 5d export is ever added.
No model-specific override — the SWARMSUSDT pattern doesn't depend on
which model emitted the signal.

**Schema change.** `crypto_signal_exclusions` gains a `ret5 DOUBLE`
column. Idempotent migration: `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
(in `crypto/schema.py:_CRYPTO_SIGNAL_EXCLUSIONS_MIGRATIONS`, applied
inside `create_all_tables` after the CREATE). Pre-ADR-028 rows keep
`ret5 = NULL` until the next export-day re-suppression UPSERTs them.

**4USDT-class explicitly NOT addressed.** The 4USDT-class pattern (deep
dd90 + 60d downtrend + recent bounce) is a real failure mode (14 of 93
deep losses, avg −19.3%) but none of the candidate filters caught it
without unacceptable portfolio damage. Specifically, `ret5 = -0.012` at
4USDT's entry-date is nowhere near the `-0.30` threshold; loosening to
`-0.10` reintroduces the Variant-A collateral damage. Treated as a
separate workstream — likely candidates are entry-conditional time-stop
adjustment (all 93 deep losers exit on `time`, not on the trailing stop)
or volatility-regime filtering, not another `should_exclude` rule.

**Expected impact (live).** Dry-run against `crypto_ml_features`
MAX(trade_date) = 2026-05-13 on 48 active coins: 4 exclusions — 2 from
Rule A unchanged (SKYAIUSDT, ZEREBROUSDT), 2 newly by Rule B
(DOGSUSDT, SWARMSUSDT). ≈ 4% of universe per day on current data; never
wipes the top-6 (the existing "empty predictions list" behavior in
`build_predictions` remains the same correct degradation path).

**Files of record.** `crypto/config.py` (`POSTPARABOLIC_RET5_THRESHOLD`),
`crypto/ml/postparabolic_filter.py` (signature + OR-combined rules +
three reason tokens), `crypto/ml/POSTPARABOLIC_FILTER_SPEC.md` (v2
section), `crypto/exports/write_daily_predictions.py` (passes `ret5`
through; persists in the audit row), `crypto/schema.py`
(`ret5 DOUBLE` column + migration), `tests/crypto/test_postparabolic_filter.py`
(+13 cases incl. the SWARMSUSDT and 4USDT live-incident pins),
`tests/crypto/exports/test_write_daily_predictions.py` (+4 integration
cases). No engine-repo change (the file interface stays the same — coins
are simply absent from the ranked list when excluded). No CLAUDE.md
change. No CHANGELOG.md exists in the repo.

**Pending operator action.** None required for the engine — it
consumes the unchanged JSON schema. MHDE side: merge the branch; the
next daily export at 00:40 UTC will apply the new rule and write
ret5-aware rows to `crypto_signal_exclusions`.

**Reversibility.** Set `POSTPARABOLIC_RET5_THRESHOLD = -1e9` in
`crypto/config.py` (or any value the live `return_5d` distribution never
crosses) to disable Rule B without removing code. Or pass a literal
`None` from `build_predictions` — Rule B's per-input fail-open will then
never fire. Full revert is a single commit revert.

**Strategy-baseline marker.** This change establishes a new strategy
baseline for downstream retrospective analysis. `config/monitoring.yaml`
now carries two `paper_trading_drift.strategy_baselines` entries:

  1. `2026-05-12` — KI-138 OHLCV repair (pre-baseline trades used
     corrupted prices).
  2. `2026-05-14` — this ADR (Variant D / Rule B added to the
     post-parabolic gate; strategy character changes from this date
     forward).

The drift monitor and the dashboard daily-balance table both
auto-resolve the *latest* baseline `date` as the floor for rolling-window
metrics (Check C closed win rate, Check D label hit rate; pre-baseline
trades are excluded from the denominator). The append-only list
preserves history so a future analyst can see why each baseline was
set — and so any pre-2026-05-14 P&L is correctly attributed to the v1
filter (Rule A only) rather than mixed with v2 behavior.

## ADR-029 — pipeline_execution crypto recency budget reflects T-1 prediction_date semantic (27h → 51h)

**Status:** accepted 2026-05-14. Implemented on branch
`fix-pipeline-execution-crypto-threshold`. Adjusts the threshold
constant inside `monitoring/pipeline_execution.py:RECENCY_BUDGET`;
no schema change, no writer change.

**Context.** `monitoring/pipeline_execution.py` flags a pipeline as
stale when `now - midnight(MAX(prediction_date))` exceeds a per-engine
`RECENCY_BUDGET`. The crypto entry was `timedelta(hours=27)` with the
comment "24h cycle + 3h grace". That budget is only correct if
`prediction_date` increments to *today* on each successful fire. It
does not: `crypto/ml/predict.py:score_universe` sets
`prediction_date = MAX(trade_date) FROM crypto_ml_features` — i.e. the
last completed features day, T-1 calendar. So even immediately after
the 00:30 UTC fire the age is already ~24h 30m, and right before the
next fire it is ~48h 30m. The 27h budget alerted from ~03:30 UTC
onwards every day, regardless of pipeline health.

The same T-1 semantic applies to the equity leg (ADR-015 → 75h budget
absorbs both T-1 and the Fri→Mon weekend roll). FX is unaffected
because `fx_ml_predictions` carries a true hourly `datetime_utc`, not
a calendar date.

**Decision.** Set `RECENCY_BUDGET['crypto'] = timedelta(days=2, hours=3)`
(51h = 48h cycle + 3h grace). The 3h grace mirrors equity. Source-of-
truth comment in `monitoring/pipeline_execution.py` rewritten to spell
out the T-1 cause so a future reader does not "tighten this back down".

**Alternatives considered.**
- *Switch the column the monitor reads to a run-time stamp
  (`export_date`, `created_at`).* Cleaner in spirit, but neither
  `crypto_ml_predictions` nor any sibling table currently carries a
  reliably-populated run-time stamp. `crypto_signal_exclusions.export_date`
  is itself a calendar `DATE`. Adopting this option requires (1) a
  schema change to `crypto_ml_predictions` (add `created_at TIMESTAMP
  DEFAULT CURRENT_TIMESTAMP`), (2) a writer update, (3) a back-fill
  decision for historical rows, and (4) a corresponding monitor change.
  Out of scope for a single-symptom fix; recorded as a follow-up under
  KI-141.
- *Tighten back toward 24h+grace by changing what `latest_dt` resolves
  to (e.g. add 24h on read).* Hides the T-1 semantic in the monitor
  rather than in the schema. Future readers of either side would need
  the other to make sense. Rejected.
- *Make the budget operator-configurable per engine.* No live need; the
  constant is read once at module import and tested with a pinned
  literal. Adds knobs without solving anything.

**Trade-off accepted.** At 51h the monitor catches a single missed
00:30 fire within ~3h of when the *second* day's fire would also have
to miss — i.e. it requires ~two consecutive missed runs to alert,
versus the previous (broken) one. The previous behavior was producing
zero usable signal anyway (firing every day at ~03:30 UTC even when
healthy); a less sensitive but truthful alarm is strictly better than
a sensitive but constantly-false one. A row-count alarm and the
per-pipeline monitor (ADR-026) provide complementary, faster signals
for single-day issues.

**Files of record.** `monitoring/pipeline_execution.py` (constant +
header docstring; `RECENCY_BUDGET['crypto']` literal updated, comment
rewritten to cite this ADR + KI-141),
`tests/regression/test_pipeline_execution_crypto_t1.py` (three pinned
regressions: on-time afternoon must pass, just-before-next-fire must
pass, two-missed-fires must fail). KI-141 added to `KNOWN_ISSUES.md`.
No engine-repo change. No schema change.

**Reversibility.** Single-constant revert. The pinned regression
tests fail-loud if the constant is moved back below 48h+grace; that is
the intended guardrail against accidental reversion. To genuinely
revisit (e.g. once a run-time column ships), update the tests
alongside the constant and link the new ADR.

---

## ADR-030 — Stooq freshness check is today-exact, not "within last N days"

**Status:** accepted 2026-05-14. Implemented on branch
`fix-equity-stooq-freshness`. Tightens
`ingestion/ingest_stooq.py:_tickers_needing_prices` from a 2-day
window to an exact-day match against today's UTC date. No schema
change, no orchestrator change.

**Context.** `StooqPricesIngestor` is the second equity price source
in the daily-radar ingest chain (`ingestion/orchestrator.py`,
position 2 after Polygon). Its job is to fill today's quote for
universe tickers whose prices Polygon couldn't deliver. The
`_tickers_needing_prices(conn, tickers)` method decides which
tickers actually need a Stooq fetch.

The pre-fix implementation flagged a ticker as needing prices if it
had **no row with `trade_date >= today - 2 days`** in
`prices_daily`. That window dates back to when Polygon was being
called per-ticker against `/v2/aggs/ticker/.../range/...` and
free-tier rate limits (~5 req/min) meant many universe tickers
ended a 23:15 UTC daily-radar run still without yesterday's price;
Stooq's broad sweep then patched today over the gap, letting the
next morning's `ml backfill-features` advance to T-1.

Commit `473b92a` (2026-05-09) replaced the per-ticker Polygon path
with the bulk grouped-daily endpoint
(`/v2/aggs/grouped/locale/us/market/stocks/{date}`). The grouped
endpoint reliably serves T-1 at 23:15 UTC (Polygon publishes it the
following Eastern morning, well before the next daily-radar fire),
and 403s on T-0 (Polygon hasn't published the same day's data yet).
Net effect on `prices_daily` immediately after Polygon's pass:
**every universe ticker has a T-1 row, none has T-0.**

That state silently broke the Stooq fallback. With T-1 rows present
and the threshold at `today - 2 days`, every universe ticker was
"fresh" and `_tickers_needing_prices` returned at most a handful of
ADRs missing from Polygon's grouped feed — not the universe. The
production logs caught it cleanly (KI-142):

```
Stooq: 517 rows inserted for 517/520 tickers   (2026-05-11, pre-grouped)
Stooq: 2   rows inserted for 2/3   tickers     (2026-05-12, post-grouped)
Stooq: 6   rows inserted for 6/6   tickers     (2026-05-13)
```

Downstream, `prices_daily` for T-0 had only ~4 rows (foreign ADRs
the grouped feed missed); `ml backfill-features` couldn't write
features for T-0 (no universe prices for that date); `ml predict`
fell back to `MAX(trade_date)` = T-1 in `ml_features` and scored
`prediction_date` = T-1. The shift from T-0 features to T-1 features
manifested as a one-trading-day lag in the prediction surface and a
sustained 🔴 from the new pipeline monitor's
`check_feature_pipeline` step (deployed 2026-05-12 per ADR-026).

**Decision.** Replace the 2-day window with an exact-day match:

```python
today = date.today().isoformat()
existing = {r[0] for r in conn.execute(
    "SELECT DISTINCT ticker FROM prices_daily WHERE trade_date = ?",
    [today],
).fetchall()}
return [t for t in tickers if t not in existing]
```

Rationale: the Stooq `/q/l/` endpoint, by construction, returns
*today's* quote. Aligning the freshness check with what Stooq
actually writes makes the contract honest — a ticker is "fresh"
iff it already has the row Stooq would write. Yesterday's polygon
row, however valid for other purposes, no longer counts as "having
today's price."

**Alternatives considered.**
- *Add a same-day pass to the Polygon ingestor* using the per-ticker
  `/v2/aggs/ticker/.../range/{today}/{today}` endpoint. Polygon's
  free-tier ~5 req/min means a 416-ticker universe takes ~84 minutes
  — incompatible with the 23:15 UTC daily-radar slot and the rate
  limit that motivated the grouped-daily switch in the first place.
  Rejected.
- *Move daily-radar to ~10:00 UTC the next morning* so Polygon's
  grouped endpoint serves T-0 in one call. Operationally invasive
  (breaks the `mhde-predict.timer` 00:15 UTC ordering, the dashboard
  artifact mtime checks, and the catalyst queue's same-evening
  cadence). Rejected for a freshness-filter bug.
- *Adjust the pipeline monitor to expect T-2 features* instead of
  T-1 (`expected_equity_prediction_date(now) - 1`). Hides the
  regression in the monitor rather than restoring the data
  contract; the underlying T-1 → T-2 shift in `ml_predictions`
  remains a real one-day quality regression. Rejected.
- *Keep the 2-day window but switch to per-ticker MAX(trade_date)*
  (i.e. `HAVING MAX(trade_date) >= today`). Equivalent to the
  chosen fix in steady state on weekdays and slightly more
  permissive on weekends. The exact-day form is easier to read and
  matches Stooq's data semantic 1:1; the per-ticker form adds no
  capability. Rejected as cosmetically different but no better.

**Trade-off accepted.** Stooq is now called for every universe
ticker that doesn't yet have today's row — restoring the pre-2026-05-09
HTTP load (~10 batches of 50 tickers, ~1.5s of inter-batch sleep,
~504 universe calls per nightly run). That is the load Stooq
sustained without issue from system inception through 2026-05-08.
On weekends and US-holiday weekdays, every universe ticker remains
"missing today" and is fetched; Stooq's `/q/l/` returns the most
recent close (Friday's), which `INSERT … ON CONFLICT DO NOTHING`
deduplicates against existing Friday rows — a no-op under steady
state. Polygon's `(ticker, trade_date)` PK on `prices_daily`
continues to protect Polygon-sourced rows from being overwritten
by Stooq when both sources happen to write the same day.

**Files of record.** `ingestion/ingest_stooq.py` (`_tickers_needing_prices`
rewritten, `_FRESHNESS_DAYS` constant deleted, `timedelta` import
dropped, body comment cites ADR-030 + KI-142),
`tests/equity/test_ingest_stooq.py` (3 new pinned regressions:
`test_tickers_needing_prices_returns_universe_when_only_yesterday_in_db`,
`test_ingest_fetches_today_when_universe_has_only_yesterday`,
`test_polygon_t1_does_not_short_circuit_stooq_t0`; 1 forward-pin
`test_tickers_needing_prices_skips_when_today_already_present`;
`test_polygon_prices_not_overwritten_by_stooq` re-seeded to use
today's date so it remains a meaningful guard against PK
overwrites under the new contract). `KNOWN_ISSUES.md` adds KI-142
opened-and-resolved 2026-05-14.

**Reversibility.** Single-method revert. The four pinned tests
fail-loud if the freshness check is widened past today, which is
the intended guardrail. The orchestrator-shape integration test
(`test_polygon_t1_does_not_short_circuit_stooq_t0`) double-pins
the failure mode that originally let this slip through.

---

## ADR-031 — Universe-tier sort puts primary first so the dev-mode cap displaces extended, not primary

**Status:** accepted 2026-05-14. Implemented on branch
`fix-equity-orchestrator-tier-sort`. Single-character SQL change
applied to two byte-identical call sites
(`ingestion/orchestrator.py` and `pipelines/daily_radar.py`).
No schema change, no orchestration change, no engine repo change.

**Context.** The equity ingest universe is built by selecting
`SELECT ticker FROM companies WHERE is_active = true ORDER BY
universe_tier, ticker` and slicing to a `max_symbols` cap (520 in
production). The same SELECT lives in two places: the orchestrator
itself (`ingestion/orchestrator.py:75`, called by direct CLI `python
main.py ingest`) and the daily-radar pipeline
(`pipelines/daily_radar.py:77`, called by the 23:15 UTC
`mhde-daily-analysis.service`). Daily-radar passes its own list to
the orchestrator as `tickers_override`, which short-circuits the
orchestrator's own SELECT — so daily_radar.py is the call site that
actually reaches production every night.

In SQL, `'extended'` sorts alphabetically before `'primary'`. With
the live shape of 174 extended-tier rows + 504 primary-tier rows =
678 total, the pre-fix sort produced:

- positions 0-173: 174 extended-tier tickers (alphabetical)
- positions 174-677: 504 primary-tier tickers (alphabetical)

Slicing to `max_symbols=520` therefore included all 174 extended +
the first 346 primary tickers (A → just before "ODFL"), and dropped
positions 520-672 — 153 primary tickers from "ODFL" through "XYL".
Of those 153, 99 passed the ML universe filter (≥$10B market cap,
non-ETF, sectored) — including `ORCL` ($494B), `WMT` ($450B), `XOM`
($638B), `PLTR` ($345B), `UNH` ($334B), and many others. They
silently stopped having `prices_daily` rows ingested, which silently
stopped having `ml_features` rows computed, which silently shifted
the active models' `prediction_date` from T-1 to T-2 (because
`ml_features.MAX(trade_date)` could no longer advance).

The trigger was a data event: the extended tier was first populated
on 2026-05-01 09:51 UTC and finished filling on 2026-05-02 09:02 UTC
(per `companies.created_at`). Before that the universe was 504 active
companies (all primary), which fit comfortably under the 520 cap. The
sort was latent-buggy from inception — only the introduction of the
extended tier exposed it. ML feature coverage dropped from 411 → 312
between trade_date 2026-05-01 and 2026-05-04, and stayed at ~311 for
two weeks until the per-pipeline equity monitor (deployed 2026-05-12,
ADR-026) flagged the prediction-side T-1 → T-2 lag and a read-only
investigation traced it back here.

**Decision.** Add `DESC` to the ORDER BY in both call sites:

```sql
SELECT ticker FROM companies WHERE is_active = true
ORDER BY universe_tier DESC, ticker
```

`'primary'` > `'extended'` alphabetically, so DESC puts the 504
primary-tier tickers in positions 0-503 of the sorted list. The
520-slot cap now fills as 504 primary + first 16 extended, which is
exactly the production intent ("ingest the full ML universe; use
extended slots only for incidental coverage"). Live verification
against the current `companies` snapshot confirms the cap goes from
312 → 416 ML-universe tickers (full recovery, +104 tickers
including all the displaced names).

**Alternatives considered.**
- *Two-pass selection (Option B from the investigation):* "include
  all primary tickers up to cap, then fill remaining slots with
  extended in alphabetical order." Functionally equivalent to the
  ORDER BY DESC approach in the current shape (504 primary fits
  under 520 cap with room to spare). More code; no incremental
  capability. Rejected as more complex without benefit.
- *Remove the `max_symbols` cap entirely (Option C):* The cap was
  introduced when `ingestion/ingest_prices.py` looped per-ticker
  against Polygon's rate-limited per-ticker endpoint and 504
  tickers took ~50 minutes to ingest. After the 2026-05-09
  grouped-daily switch (commit `473b92a`, ADR pending), Polygon
  serves the entire ~12k US stock list in one HTTP call regardless
  of universe size. The cap no longer rate-limits anything. Removing
  it is the cleanest long-term fix but has larger blast radius
  (every downstream consumer that assumed `len(tickers) ≤ 520` would
  see ~678 tickers). Tracked as KI-145 follow-up; deferred so the
  immediate ML-universe fix could ship as a one-character change.
- *Refactor both call sites to a shared helper* (e.g.
  `universe.get_active_tickers_for_ingest(conn, max_symbols)`).
  Eliminates the parallel-query duplication that let the bug slip
  past code review (one ORDER BY change should not need to be
  applied to two files in lock-step). Larger change touching
  multiple files; tracked as KI-144 follow-up.

**Trade-off accepted.** Under the new sort, on each daily-radar fire
all 504 primary tickers are ingested plus the first 16 extended
tickers (alphabetical). 158 extended tickers (positions 16-173 in
the post-fix sort) are NOT ingested. Per KI-122, the extended tier
contains 174 stale rows accumulated from prior universe builds —
none of which currently flow through to `ml_features` (the ≥$10B
filter excludes them). Losing extended-tier ingestion for those 158
rows therefore has no observable downstream effect today. KI-122
remains the open follow-up on extended-tier reconciliation; this
ADR doesn't claim to resolve it, only to stop it from displacing
primary-tier coverage.

**Files of record.**
- `ingestion/orchestrator.py` — line 75; ORDER BY clause now
  `universe_tier DESC, ticker`. Inline block comment cites this ADR
  + KI-143 + KI-144 (the duplication follow-up) so a future reader
  doesn't "tighten" by removing DESC without re-reading the rationale.
- `pipelines/daily_radar.py` — line 77; identical change. Inline
  comment cross-references the orchestrator ADR rationale and notes
  this is the call site that reaches production via
  `tickers_override`.
- `tests/equity/test_orchestrator_universe_sort.py` (NEW) — 5
  pinned regressions:
  1. `test_primary_tier_fills_cap_first` — behavioural pin: with
     500 primary + 174 extended seeded, the first 520 slots after
     the (fixed) sort contain all 500 primary + 20 extended.
  2. `test_full_universe_returned_when_below_cap` — edge case:
     small universe (4 tickers) returns full set regardless of
     cap.
  3. `test_inactive_tickers_excluded` — sanity pin: the
     `is_active = true` filter remains intact under the new sort.
  4. `test_orchestrator_uses_primary_first_sort` — duplication-pin
     A: reads `ingestion/orchestrator.py` source, asserts the
     fixed ORDER BY clause is present and the buggy form is not.
  5. `test_daily_radar_uses_primary_first_sort` — duplication-pin
     B: same assertions on `pipelines/daily_radar.py`. Both
     duplication-pins fail-loud if either file drifts.
- `KNOWN_ISSUES.md` — KI-143 added (opened + resolved 2026-05-14),
  KI-144 added (open follow-up: shared helper extraction), KI-122
  amended to note the related fix landed here. KI-145 added (open
  follow-up: `max_symbols` cap removal post-grouped-daily).

**Reversibility.** One-character revert per file. The two
duplication-pin tests fail-loud if either file is reverted, which is
the intended guardrail. The behavioural test fails if the sort order
changes such that primary no longer fills the cap first (e.g. if a
future change removes the `is_active` filter and includes inactive
tickers ahead of primary).

---

## ADR-032 — Validation methodology for parameters affecting portfolio drawdown

**Status:** accepted 2026-05-14. Methodology lesson; no code change in this
ADR. Derived from the `feat-trail-pct-tighten-to-0.10` workstream, where
walkfold per-fold dominance pointed to a ship that portfolio-level
simulation later rejected. Future-binding on the operator and on any
agent making spec changes that affect Policy D exit or position
concurrency parameters.

**Context.** The 2026-05-14 walkfold validation
(`data/processed/trail_pct_walkfold_validation.md`) compared crypto
`trail_pct` values 0.05 → 0.30 across six non-overlapping ~2-month folds
on the live window 2025-04-05 → 2026-05-07. `trail_pct=0.10` dominated
the deployed `0.30` on both Sharpe (per-fold mean 7.42 vs 6.35,
0.10 winning all six folds) and the walkfold's "Max DD" metric
(per-fold mean −24.30% vs −28.70%, 0.10 winning 5 of 6 folds — the
combined dominance criterion the operator was using). On that evidence the `feat-trail-pct-tighten-to-0.10`
ship workstream was opened: a full-window backtest was persisted at
trail=0.10, `PHASE1B_WINNER_RUN_ID` was repointed, and
`active_spec.json` was regenerated.

The regenerated spec exposed a disagreement the walkfold validation had
not. The spec's own `backtest_expectations.portfolio_max_dd_pct` came
back at **−37.0%** for trail=0.10, exceeding the live
`risk.max_account_drawdown_pct = 0.30` catastrophic guard. The
constrained sweep
(`data/processed/trail_pct_constrained_sweep.md`) then showed every
trail value tested (0.10, 0.15, 0.20, 0.25, 0.30) producing
portfolio_max_dd worse than the −25% safety floor:

| trail | walkfold dom. vs 0.30 | walkfold mean max_dd | portfolio max_dd |
|---:|---:|---:|---:|
| 0.10 | 5/6 | −24.30% | **−37.03%** |
| 0.15 | 5/6 | −25.96% | **−37.67%** |
| 0.20 | 4/6 | −27.53% | **−38.31%** |
| 0.25 | 3/6 | −28.18% | **−31.60%** |
| 0.30 | baseline | −28.70% | **−31.91%** |

The two metrics are not the same number measured differently — they are
different metrics. The walkfold "Max DD" is the per-fold maximum of the
running sum of per-trade net P&L fractions: no starting capital, no
position-size cap, no compounding. The portfolio simulator
(`crypto/execution/backtest/report.py:simulate_portfolio`) models the
live deployment shape — $1,000 starting capital, max 6 concurrent
positions, deploy_fraction=0.8, leverage=1.0, position size computed off
current equity each entry. Compounding amplifies tail drawdowns
asymmetrically: when equity is at a high after a winning streak,
position sizes are larger, so a regime like F2 (2025-06-05 →
2025-08-04, where trail=0.10 walkfold max_dd is −57.7%) draws down a
much larger nominal balance than the walkfold's capital-naive metric
suggests. Walkfold validation measures *signal-and-exit fit*. It does
not measure *envelope conformance* against the engine's kill switch.

**Decision.** Future spec changes on any axis where compounding can
amplify portfolio drawdown — `trail_pct`, `activation_pct`, `top_n`,
`max_concurrent`, `deploy_fraction`, or any new position-sizing logic
— require **BOTH** of the following before the spec is regenerated and
the active prediction path is repointed:

1. **Walkfold per-fold dominance over the deployed baseline in ≥ 4 of
   6 folds.** Computed by `.claude/local_scripts/backtest_trail_walkfold.py`
   (or the equivalent harness for the axis under test) over the
   current production window, on Sharpe ratio as the primary metric.
   This is the existing gate; it stays.
2. **Portfolio-simulator full-window run with `max_dd ≤ −25%`
   (a 5-percentage-point safety margin below the live
   `risk.max_account_drawdown_pct = 0.30` kill switch) AND portfolio
   Sharpe meaningfully above the deployed baseline (operator default:
   ≥ baseline + 0.10).** Computed by `simulate_portfolio(
   starting_capital=1000, max_positions=6, deploy_fraction=0.8,
   leverage=1.0)` — exactly the call `_backtest_expectations` makes
   when the spec is generated. Same params, full window, post-parabolic
   filter ON (matching live).

Gate (1) is necessary but not sufficient. Gate (2) is the catastrophic
guard. Both must pass; if either fails, **HOLD** the deployed value.

**Rationale.** Walkfold validation answers: *does this parameter fit
the signal-and-exit dynamics better in held-out windows?* The portfolio
simulator answers: *does the resulting trade stream survive the
capital envelope the engine actually enforces?* These are independent
questions, and the trail_pct=0.10 case proves they can disagree on the
metric that matters most — the catastrophic kill-switch boundary. A
parameter that wins on per-fold P&L geometry but loses on capital-
constrained simulation is a parameter that ships well in research and
trips the kill switch in production. The portfolio simulator is the
last validation step before the live envelope; it must precede the
spec repoint, not follow it.

**Alternatives considered.**

- *Walkfold-only (the status quo).* Empirically demonstrated
  insufficient by the trail_pct=0.10 case. Walkfold dominance pointed
  to a ship; portfolio simulation showed every candidate trail value
  exceeded the −30% kill switch. Without gate (2) the workstream
  would have shipped a spec whose own `backtest_expectations` admitted
  to a kill-switch-tripping drawdown — a self-inconsistent artifact.
  Rejected.
- *Live shadow trading as the second gate.* Conceptually stronger than
  simulation because it captures real fill quality, real slippage, and
  real funding behaviour. Out of scope: the engine's
  shadow-vs-live-mode harness is not yet built (Phase 3 of
  `docs/PATH_TO_LIVE_PLAN.md`). Until that infrastructure exists, the
  portfolio simulator is the best proxy for the envelope. Re-evaluate
  this ADR when Phase 3 lands; shadow trading would supersede gate
  (2), not duplicate it.
- *Portfolio-simulator-only.* Skips the per-fold robustness check.
  Full-window simulation can hide regime-specific failure modes that
  walkfold surfaces — e.g. a parameter that looks fine over the whole
  window because a benign late-window regime drowns out an awful
  mid-window one. Walkfold dominance is cheap to compute and adds a
  robustness signal the full-window simulation lacks. Rejected.

**Consequences.**

- The trail_pct=0.10 rejection is the canonical example for future
  operators: walkfold-dominant, portfolio-failing, correctly held.
  Anyone shipping a Policy D exit-parameter change should read both
  research markdowns before opening the PR.
- The pre-ship checklist for any param change affecting portfolio DD
  must now state, explicitly, both gates and the kill-switch margin.
  This belongs in `OPERATIONS.md` under spec-change procedure (filed
  as a follow-up; not in scope of this ADR).
- The walkfold validation script
  (`.claude/local_scripts/backtest_trail_walkfold.py`) currently
  computes per-fold raw-P&L max drawdown. Extending it to also run
  `simulate_portfolio` on each fold's trades would surface the
  capital-constrained metric at walkfold granularity, catching the
  envelope problem before the ship workstream starts rather than
  after. Filed as a separate KI to avoid scope creep here; the gate
  defined in this ADR remains the full-window portfolio simulation,
  which is the metric the spec's own expectations use.
- This ADR binds the methodology, not a specific parameter. Future
  axes that compound into portfolio drawdown (e.g. a new
  position-sizing policy, a leverage knob) are governed by it
  automatically; the ADR does not need to be amended per-axis.

**References.**

- `data/processed/trail_pct_walkfold_validation.md` — six-fold sweep,
  trail=0.05 → 0.30, Sharpe + walkfold max_dd + per-fold P&L. Source
  of the dominance claim.
- `data/processed/trail_pct_constrained_sweep.md` — full-window dual-
  gate sweep, walkfold dominance count + portfolio-simulator Sharpe +
  portfolio max_dd, per trail value. Source of the methodological
  finding immortalised here.
- `crypto/execution/backtest/report.py` — `simulate_portfolio`
  implementation; the function call shape that gate (2) requires.
- `data/exports/active_spec.json` — `risk.max_account_drawdown_pct`,
  the live kill-switch envelope the safety floor is set 5pp under.
- ADR-024 (knockout label validation methodology) — adjacent precedent
  for "walkfold isn't the whole gate."

## ADR-033 — Execution leverage 1x → 2x

**Status:** accepted 2026-06-01. MHDE-only change (single line in
`crypto/exports/spec_config.py`, regenerated `active_spec.json`); no
engine change required. Branch `chore/spec-leverage-2x`, draft PR
awaiting operator.

**Context.** With `sizing.leverage = 1.0`, each position's initial
margin equals its full notional. Live entries observed Binance
`-2019 "Margin is insufficient"` rejections — DOGSUSDT was the
recurring case — once prior open positions had consumed enough of the
wallet that the next sized notional could not be margined 1:1. The
engine sizes positions against *total* wallet (`position_size_max_pct`
of wallet) but does **not** consult `availableBalance` at placement
(`engine/execution/executor.py` computes `raw_qty =
position_size_usd / current_price` with only `min_qty` / `minNotional`
filter checks), so the rejection surfaces only at the venue.

**Decision.** Raise execution leverage to **2.0** in the spec's single
source of truth, `crypto/exports/spec_config.py:SIZING["leverage"]`,
and regenerate `active_spec.json` via `crypto export-spec` (hash
recomputed by `compute_spec_hash`; the JSON is gitignored and
regenerates on the host at deploy). At fixed notional, 2x halves the
initial margin required per position (~40% wallet utilisation at the
current concurrency envelope), clearing the `-2019` rejections with
large headroom. **Notional is unchanged, so exposure and realised
P&L are unchanged** — this is a margin-efficiency knob, not a
risk-gearing knob. Isolated-margin liquidation moves from effectively
unreachable (1x) to roughly **−50%** adverse, still far clear of the
Policy D exit behaviour and the engine's `-5%`-class stop geometry, so
the change buys margin headroom without bringing liquidation into
contact with normal exits.

**3x considered and rejected.** The engine validator pins an allowed
set `_VALID_LEVERAGE = {1.0, 2.0}` (`engine/spec/validator.py`), and
`INTERFACE.md §2` documents `leverage ∈ {1.0, 2.0}` as a *locked
decision*. A spec with `leverage = 3.0` would fail `load_spec` →
`SpecLoadError` → the entry handler aborts for **every** symbol, which
is strictly worse than the single-symbol `-2019` it was meant to
relieve. 3x would therefore be a coordinated two-repo change (widen the
allowlist + the JSON-schema-adjacent contract + INTERFACE.md) for
headroom the 2x utilisation figure shows is not needed.

**Scope boundaries.**

- **Backtest sim leverage left at 1x.** `write_active_spec.py:120`
  (`simulate_portfolio(..., leverage=1.0)`) drives only
  `backtest_expectations`; it is deliberately decoupled from execution
  leverage. Execution leverage at fixed notional does not change the
  P&L path a backtest measures, so the published expectations remain a
  faithful 1x baseline. (Confirmed: regenerating the spec produced **no
  `backtest_expectations` churn** — only `sizing.leverage`,
  `spec_hash`, `generated_at`, and the `generated_by_mhde_commit`
  stamp changed.)
- **Does not add the available-margin check.** The missing
  `availableBalance`-aware sizing in the engine remains the true root
  cause of `-2019`; 2x only reduces how often the gap bites. That check
  is a separate engine-side lever, out of scope here.

**References.**

- `crypto/exports/spec_config.py` — `SIZING["leverage"]`, the single
  source of truth changed by this ADR.
- `crypto-trading-engine/engine/spec/validator.py` — `_VALID_LEVERAGE`
  allowlist that bounds the contract at 2.0.
- `crypto-trading-engine/docs/INTERFACE.md §2` — the locked
  `leverage ∈ {1.0, 2.0}` decision.
- `crypto-trading-engine/engine/execution/executor.py` — sizing /
  placement path lacking the available-margin check.
