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
