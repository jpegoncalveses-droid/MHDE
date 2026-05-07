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
