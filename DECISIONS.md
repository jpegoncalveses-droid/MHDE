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
