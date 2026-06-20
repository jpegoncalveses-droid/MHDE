# MHDE Architecture

What the system does, top-down.

This document describes the **post-Session-0** state. For the deployment
topology (services, sockets, secrets, host details), read
`INFRASTRUCTURE.md`. For the runbook, read `OPERATIONS.md`. For the
table-by-table data layout, read `DATABASE_SCHEMA.md`.

---

## What the system is

MHDE (Market Hypothesis Discovery Engine) runs **three independent
prediction engines** plus a daily equity-ingest / scoring path inherited
from the original engine. Everything writes to a single DuckDB file at
`data/mhde.duckdb` and surfaces through a Streamlit dashboard at
`https://mhde.duckdns.org`.

```
                      ┌────────────────────────────────────┐
                      │       data/mhde.duckdb             │
                      │  (single writer; lock-retry'd)     │
                      └──────────────────┬─────────────────┘
                                         │
   ┌─────────────────────────────────────┼─────────────────────────────────────┐
   │                                     │                                     │
   ▼                                     ▼                                     ▼
┌──────────┐         ┌─────────────────────────────────┐                ┌──────────┐
│ ML       │         │ Equity ingest + scoring         │                │ Streamlit│
│ predict  │         │ (daily_radar @ 23:15 Mon-Fri)   │                │ dashboard│
│ @ 21:00  │         │   ↓                             │                │  (always)│
└──────────┘         │ prediction-vs-actual            │                └──────────┘
                     │ enrich-root-causes              │
┌──────────┐         │ priority-refresh-queue          │                ┌──────────┐
│ Crypto   │         │ daily-catalyst-queue            │                │ FX bot   │
│ predict  │         │   (OpenAI --no-mock)            │                │ (always) │
│ @ 00:30  │         └─────────────────────────────────┘                └──────────┘
└──────────┘
                     ┌─────────────────────────────────┐
┌──────────┐         │ Weekly retrain timers           │
│ FX       │         │   equity Sun 21:30              │
│ predict  │         │   crypto Sun 23:00              │
│ @ :05    │         │   fx     Sat 22:00              │
└──────────┘         └─────────────────────────────────┘
```

The three engines are independent: each has its own ingestion adapters,
its own feature/label/predict modules, its own model artifacts under
`models/saved/{,crypto/,fx/}`, and its own dashboard tab.

---

## Engine 1 — Equity ML

**Schedule:** `mhde-predict.service` daily 21:00 UTC.
**Source code:** `ml/`, orchestrator `pipelines/ml_prediction_pipeline.py`.

```
                                  data/mhde.duckdb
prices_daily ←───────── ingestion/ingest_yahoo_historical.py
   ▲                    ingestion/ingest_prices.py (Polygon, AV)
   │                    ingestion/ingest_stooq.py
   │                    (writes happen earlier via daily-analysis @23:15)
   │
   ├──→ ml/labels.py ─────→ ml_labels
   │     (ml backfill-labels)
   │
   ├──→ ml/features.py ───→ ml_features
   │     (ml backfill-features; called by predict pipeline as Stage 1)
   │
   └──→ ml/predict.py ────→ ml_predictions   ┐
         (ml predict)                        │ score_universe writes new rows
                                             │ fill_outcomes updates past rows
                                             ┘
```

**Runtime invariant.** The 21:00 predict service reads what the 23:15
daily-analysis run wrote *the previous evening*. If `prices_daily` is
older than 2 trading days (`pipelines/freshness.py:check_equity_freshness`),
predict logs `DATA STALE` and skips — no row is written to
`ml_predictions`.

**Two parallel feature systems** — this catches people:

| Table | Built by | Built when | Used by |
|---|---|---|---|
| `features` | `features/feature_builder.py` | inside `daily-radar` Stage `features` | Legacy `scoring/scorecard.py` (still wired in but dormant in current workflow). Long-form, one row per (ticker, feature_name). |
| `ml_features` | `ml/features.py:compute_features` | `main.py ml backfill-features` (called by `mhde-predict.service` ExecStart **before** `ml predict`) | The ML engine. Wide form, one row per (ticker, trade_date) with all 32 features as columns. |

`mhde-daily-analysis.service` (Mon-Fri 23:15) refreshes `prices_daily`
and the legacy `features` table. It does **not** refresh `ml_features`.
`mhde-predict.service` (daily 21:00) chains `ml backfill-features` →
`ml predict` so `ml_features` is brought up to date inside the predict
service itself. Manual debugging of "why is the dashboard showing
stale ML predictions" should look at the latest `MAX(trade_date)` in
`ml_features`, not `prices_daily`. The two can drift if `ml backfill-features`
hasn't run since the last price ingest.

**Outcome filling.** `ml/predict.py:fill_outcomes` walks rows where
`outcome_filled_at IS NULL` and the forward window has closed (using
trading-day arithmetic, not calendar days). Updates `actual_max_return`,
`actual_max_drawdown`, `actual_hit`, `outcome_filled_at`.

**Active model resolution.** `predict.py` picks the row in
`ml_model_runs` where `is_active=TRUE` matches `(horizon, threshold)`,
loads `model_path` (a joblib under `models/saved/`), and calls
`predict_proba`. Threshold per row is stored in
`ml_predictions.prediction_threshold`.

**Retrain.** `mhde-retrain.service` weekly Sun 21:30 runs `ml train`,
which executes walk-forward CV over the last several years and writes a
new `ml_model_runs` row. Promotion to `is_active=TRUE` is manual today
(see `KNOWN_ISSUES.md` for the gate that's planned).

---

## Engine 2 — Crypto ML

**Schedule:** `mhde-crypto-predict.service` daily 00:30 UTC.
**Source code:** `crypto/`, orchestrator
`pipelines/crypto_prediction_pipeline.py`.

The unit chains six commands in one ExecStart:

```
crypto backfill-prices  → crypto_prices_daily      (Binance)
crypto backfill-funding → crypto_funding_rates     (Binance)
crypto backfill-oi      → crypto_open_interest     (Binance)
crypto backfill-labels  → crypto_ml_labels
crypto backfill-features→ crypto_ml_features
crypto predict          → crypto_ml_predictions    + fill_outcomes
```

The chain is sequential because each step's output is the next step's
input. `TimeoutStartSec=1800` (30 min) gives headroom for Binance rate
limits.

**Universe.** `crypto build-universe` (manual / on-demand) builds
`crypto_universe` ranked by 30-day average daily volume; `predict` only
scores rows where `is_active=TRUE`.

**Cross-coin features.** Several crypto features reference BTC
(`return_vs_btc_1d/5d/10d`, `beta_to_btc_30d`, `btc_dominance`,
`btc_return_7d`, `btc_vol_30d`). The predict pipeline expects BTC's row
in `crypto_prices_daily` to be ingested; missing BTC data = degraded
crypto features.

**Freshness.** `check_crypto_freshness` requires the latest
`crypto_prices_daily.trade_date` to be within 1 calendar day of today.

**Retrain.** `mhde-crypto-retrain.service` weekly Sun 23:00.

---

## Engine 3 — FX ML

**Schedule:** `mhde-fx-predict.service` hourly at :05 UTC.
**Source code:** `fx/`, orchestrator `pipelines/fx_prediction_pipeline.py`.

The unit chains four commands per firing:

```
fx refresh-prices       → fx_prices_hourly      (TwelveData REST, post-cutover 2026-05-08)
fx backfill-features    → fx_ml_features
fx backfill-labels      → fx_ml_labels
fx predict              → fx_ml_predictions     + fx_signals + Telegram
```

This is the only engine that runs hourly and the only engine with an
**interactive** component (the Telegram bot, `mhde-fx-bot.service`).

**Single time-series.** Unlike equity/crypto (multi-ticker), FX models
one pair (GBP/EUR). All tables are keyed on `datetime_utc` instead of
`(ticker, date)`.

**4 active models.** Direction × horizon: `up_24h`, `down_24h`, `up_48h`,
`down_48h`. Each writes one row per bar to `fx_ml_predictions`.

**Signal generation.** `fx/ml/signals.py:generate_signal` reads the four
probabilities and emits a signal of type `BUY_GBP` / `SELL_GBP` /
`WAIT` based on thresholds in `fx/config.py`
(`SIGNAL_BUY_THRESHOLD`, `SIGNAL_SELL_THRESHOLD`, `SIGNAL_COUNTER_MAX`).
The signal is recorded in `fx_signals` whether or not Telegram is
notified.

**Telegram routing.** `fx/ml/signals.py:send_telegram_alert` delegates
to `fx/bot/telegram_bot.py:send_signal_alert`, which:

1. Reads `fx_alert_state.alerts_enabled` (kill switch).
2. Reads `fx_alert_state.last_buy_alert_at` / `last_sell_alert_at` and
   suppresses if within a 4h cooldown.
3. Reads `fx_position.position` and suppresses BUY_GBP if already long
   GBP (and vice versa).
4. Sends if all gates pass; updates `fx_alert_state.last_*_alert_at`.
5. Updates `fx_signals.telegram_sent / telegram_sent_at`.

**Freshness.** `check_fx_freshness` requires `fx_prices_hourly` to be
within 2 hours of "now". Stale logs a warning but **does not skip** —
predict writes anyway (this is by design; an old bar is still a valid
prediction surface, just lower confidence).

**Bot service.** `mhde-fx-bot.service` (always-on, Restart=always) runs
`main.py fx bot` — a long-polling Telegram client that handles
`/setposition`, `/clearposition`, `/alertson`, `/alertsoff`, `/status`.

**Retrain.** `mhde-fx-retrain.service` weekly Sat 22:00.

---

## The daily-analysis path (legacy MHDE engine, still active)

**Schedule:** `mhde-daily-analysis.service` Mon-Fri 23:15 UTC.
**Wrapper:** `.claude/local_scripts/run_mhde_daily_analysis.sh`.

This is the original equity ingest + scoring + reporting path. It
predates the per-engine ML rebuild but still runs because:

1. The 21:00 ML predict reads `prices_daily` populated here.
2. The catalyst queue (LLM root-cause enrichment) runs here daily with
   `--no-mock --provider openai`.

The wrapper script chains five steps:

```
main.py run daily-radar
      ↓ ingestion/* (Yahoo, Polygon, Alpha Vantage, Stooq, SEC, FDA,
      ↓             CFTC, FINRA, GDELT, earnings, StockTwits)
      ↓ → prices_daily, filings, fundamentals_*, events, macro_series, ...
      ↓ features/feature_builder.py
      ↓ → features (long-form)
      ↓ scoring/scorecard.py:compute_scores
      ↓ → scores, hypotheses, rejections, candidate_outcomes
      ↓ outputs/daily_radar_<date>.{json,md}

main.py missed prediction-vs-actual
      ↓ data/processed/prediction_vs_actual_rows.csv

main.py missed enrich-root-causes --input <CSV>
      ↓ data/processed/prediction_vs_actual_enriched_rows.csv

main.py priority-refresh-queue --enriched-csv <CSV>
      ↓ data/processed/priority_refresh_queue.csv

main.py missed daily-catalyst-queue --no-mock --provider openai --html
      ↓ → llm_runs (OpenAI call audit)
      ↓ data/processed/daily_catalyst_queue_*.{html,csv,jsonl}
      ↓ data/processed/catalyst_queue_history/
```

Step 5 is the only remaining LLM-dependent step in the daily flow. The
catalyst queue picks candidates with scores 40-44.9 (the threshold band
where the model is uncertain), runs them through OpenAI for catalyst
analysis, and writes the result for review.

A duplicate `mhde-daily-catalyst-queue.timer` exists but is **disabled**
— the daily-analysis script invokes the same CLI inline.

---

## Dashboard (Streamlit)

**Service:** `mhde-streamlit.service` (always-on).
**Source:** `dashboard/app.py` plus components under `dashboard/`.
**Public URL:** `https://mhde.duckdns.org` (via nginx → unix socket →
streamlit-relay → 127.0.0.1:8501).

Single-page multi-tab app. The 19 legacy multi-page-app pages are now
in `legacy/dashboard/pages/_legacy/`; their content was rewritten as
tabs in `app.py` for the engines that survived (ML / crypto / FX), plus
a **Paper Trading** tab that reads the crypto-trading-engine
DuckDB read-only via `CRYPTO_ENGINE_DB_PATH` (ADR-020). Paper Trading is
the first/default tab (paper-tab-overhaul). It shows a daily-balance strip,
engine-liveness metrics, then **Today's positions** — the cohort opened
today that reached the market (open first, then closed by exit desc),
rendered as a `Symbol|Entry|Exit|Opened $|PnL $|PnL %` table (open rows show
an unrealized live mark from `price_snapshots`) plus one price chart per row
(dynamic N): price + entry + the state-dependent exit reference (flat
activation line until armed, else the stepwise Policy-D trail stop; SPEC §3.2),
each series downsampled to ≤400 points preserving global min/max. Closed
`exit_price`/`realized_pnl` come from the engine's `positions` columns
(EXIT-PRICE-001, "uncomputable (KI-136)" only when no exit fill was recorded).
Also a rejected-entries list and a drift-monitor status banner re-running
`monitoring/paper_trading_drift.py` (cached 60 s).

```
dashboard/app.py
├── auth.py                  password gate (sha256) + signed-cookie session (MHDE_DASHBOARD_*)
├── components/              tables, badges, charts, filters, candidate cards
├── services/queries.py      DuckDB read-only connection per page render
├── services/actions.py      write-side helpers for outcome review
└── services/learning_stats.py
```

**Connection model.** Per-page DuckDB read-only connection (no
module-level cache). This is deliberate — the previous module-level
pattern caused stale reads when the underlying file rotated; see
`KNOWN_ISSUES.md`.

**Auth.** `MHDE_DASHBOARD_AUTH_ENABLED=true` and
`MHDE_DASHBOARD_PASSWORD_HASH` are baked into the systemd unit
`Environment=` lines, not `.env`. Dev / smoke runs disable auth via
`MHDE_DASHBOARD_AUTH_ENABLED=false`. On successful login a signed, expiring
cookie (`mhde_auth`: HMAC over `subject|expiry`, never the password) is set so
hard refresh / PWA reopen keep the session; validated server-side every load.
Fail-closed: with `MHDE_DASHBOARD_COOKIE_SECRET` unset the cookie path is inert
and the prompt returns on every refresh (legacy behaviour).

**No auto-refresh.** Streamlit doesn't poll. The FX tab has an explicit
↻ Refresh button + "Data as of bar" caption; the other tabs require a
manual page reload.

---

## Health check

**Service:** `mhde-health-check.service` + `.timer` (user-level, enabled).
**Entry point:** `main.py system health-check` →
`pipelines/health_check.py` → `health/checks.py:run_all_checks`.

Per-check rows are written to the `health_checks` table with severity
(low/medium/high) and status. Any `high` severity failure triggers a
Telegram alert via `notifications/telegram.py`.

Checks include (`health/`):
- Schema check (every required table exists).
- Database accessibility.
- Per-engine freshness (calls `pipelines/freshness.py:check_all`).
- Universe quality (active reporters in `companies`).
- ML model file existence (`models/saved/` joblibs match active
  `*_model_runs.model_path`).

---

## Monitors (Session 6)

`monitoring/` is a second observability layer that runs more often
than the daily health check and fires Telegram alerts on detected
anomalies. Each monitor is a Python module returning a `MonitorResult`
dataclass; one `cli.group monitor` in `main.py` dispatches subcommands
to each runner; one systemd `.service`/`.timer` pair per monitor lives
under `systemd/mhde-monitor-*`.

| Monitor | Cadence | Module | Detects |
|---|---|---|---|
| `dashboard-consistency` | every 6h | `monitoring/dashboard_consistency.py` | dashboard query layer ↔ direct DB count drift |
| `pipeline-execution` | hourly | `monitoring/pipeline_execution.py` | per-engine recency + row count vs 14d rolling avg |
| `config-drift` | daily | `monitoring/config_drift.py` | repo systemd files ↔ deployed copies |
| `model-performance` | daily | `monitoring/model_performance.py` | rolling 7d precision vs walk-forward baseline |
| `data-quality` | daily | `monitoring/data_quality.py` | per-engine ticker / symbol / bar coverage vs 14d avg |
| `smoke` | hourly | `monitoring/smoke_test.py` | DB opens, every active joblib loads, dashboard query returns |
| `paper-trading-drift` | every 15 min | `monitoring/paper_trading_drift.py` | reads the engine DuckDB read-only (`CRYPTO_ENGINE_DB_PATH`, see ADR-020): engine liveness, stuck `*_pending` positions, closed-trade win rate + label hit rate (`crypto_ml_labels`) vs the Phase 1B walkfold bands (sample-gated). P&L-band/DD arms deferred — KI-136. |

(Other monitors added since the Session 6 snapshot — `streamlit-freshness`,
`dashboard-synthetic`, `cross-artifact`, `phase0-calibration` — are catalogued
in full in `OPERATIONS.md` § Monitors.)

**Pipeline monitor (2026-05-12, ADR-026).** A second layer in
`monitoring/pipeline_monitor/` posts **one Telegram message per pipeline** —
crypto / equity / fx — enumerating every step's outcome 🟢 / 🔴 / ⚪ (the
header is 🔴 if any step is 🔴; ⚪ = skipped because an earlier step failed,
since the pipelines are sequential), plus a continuous (every-30-min) monitor
(`monitor continuous`) that's silent unless an FX bar is stale or the crypto
engine timers stalled. Every check is **outcome-based** (reads the DB / a file:
"`MAX(trade_date)` advanced", "rows exist for the expected date",
"`actual_hit` non-NULL for matured predictions", "`predictions_latest.json`'s
`export_date == today`", "the engine ran `entry` today and opened positions")
— never exit-code-based, because the regression that motivated it (KI-138) had
every script exit 0. The crypto monitor and the continuous monitor read the
crypto-trading-engine DuckDB read-only (`CRYPTO_ENGINE_DB_PATH`, ADR-020). New
units `mhde-{crypto,equity,fx}-pipeline-monitor` (daily 00:50 / 01:00 / 12:10
UTC — the crypto one fires right after the 00:30 predict → 00:40 export →
~00:45 engine entry chain, ADR-027) and `mhde-continuous-monitor` (`*:0/30`).
CLI:
`main.py monitor {crypto,equity,fx}-pipeline` and `main.py monitor continuous`.
Full reference: `docs/PIPELINE_MONITORING.md`.

Telegram routing: `monitoring/alert.py:send_alert` (anomaly monitors) and
`monitoring/alert.py:send_text` (pipeline monitor, which posts every run) both
bottom out in `fx.bot.telegram_bot.send_message`. The `MONITORING_DRY_RUN=true`
env var suppresses real sends — useful for local testing and
manual one-off runs.

How the monitors complement the health check: health-check is daily
and answers *"is the system running at all"*. Monitors run more often
and answer *"is the running system producing the right outputs"*.
Smoke and pipeline-execution have caught real production issues that
the daily health check would have surfaced 24h later
(see `KNOWN_ISSUES.md` resolved-archive: KI-009, KI-010).

---

## Cross-cutting infrastructure

### `pipelines/freshness.py`
Called at the top of every prediction pipeline. Three policies:
equity 2 trading days, crypto 1 calendar day, FX 2 hours. Equity and
crypto **skip** the predict if stale. FX **logs but continues** —
intentional, because partial bars are still useful.

### `notifications/telegram.py`
Receives `telegram_bot_token` / `telegram_chat_id` via the `cfg` dict from
`storage/config.py`, which loads them from the host env file
`~/.config/mhde/telegram.env` (see `load_env_file()`). The FX bot
(`fx/bot/telegram_bot.py`) reads the same file. Documented in
`INFRASTRUCTURE.md` "Telegram credentials".

### `storage/db.py`
`get_connection()` wraps `duckdb.connect()` with lock-retry: 30s, 60s,
120s back-off when "Could not set lock" is raised. This prevents the
hourly FX firing from crashing when daily-radar is mid-run.

### `pipelines/health_check.py` ↔ `health/`
Health check orchestration (`pipelines/health_check.py`) calls the
individual check functions in `health/`. Outputs to `health_checks`
table and to a structured log message.

### LLM stack (`llm/`)
Used by `missed/catalyst_queue.py`. Provider-agnostic wrapper around
OpenAI / NVIDIA NIM. Records every call in `llm_runs` with input/output
hashes and estimated cost.

---

## Dependency on ATSRP (external)

`/home/jpcg/ATSRP/` is **partially optional** post-2026-05-08 cutover:

1. ~~`fx/data/refresh.py` shells out to ATSRP for Dukascopy bi5 hourly bars.~~
   **Removed in ADR-013 cutover (2026-05-08).** FX bars now come from
   TwelveData via in-process REST calls; no subprocess dependency.
2. ~~`notifications/telegram.py` loads `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID` from `/home/jpcg/ATSRP/.env`.~~ **Migrated off ATSRP on
   2026-06-20 (shelving step 1):** creds now load from `~/.config/mhde/telegram.env`
   via `storage/config.py::load_env_file()`. No remaining MHDE→ATSRP creds dependency.

The systemd services for the old ATSRP FX engine itself are disabled
(per Session 0 ADR-003). Telegram credentials are now the only remaining
ATSRP integration point; one more migration would let ATSRP be deleted.

---

## Things that are NOT in production today

These exist in the code (or in `legacy/`) but are not wired into any
running pipeline:

- The catalyst review server (`legacy/review/server.py`) and its UI at
  `https://mhde.duckdns.org/review/` (retired Session 0).
- The shadow ranker / promotion gate / governance scaffolding under
  `legacy/{models,governance}/` (was meant for a model-promotion
  pipeline that didn't ship).
- The learning loop (`legacy/learning/` — calibration, feedback,
  experiments). Never wired in.
- The backtest framework (`legacy/backtest/`) — stub.
- The `weekly-review` CLI (`legacy/pipelines/weekly_review.py` +
  `legacy/reports/weekly_review.py`).

See `legacy/README.md` for the full list.
