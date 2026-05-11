# Database Schema

All persistent state lives in a single DuckDB file: `/home/jpcg/MHDE/data/mhde.duckdb`.
DuckDB allows one writer at a time — see `INFRASTRUCTURE.md` "Gotchas"
for the lock-retry behavior baked into `storage/db.py`.

The schema is split across four sources:

| File | Contents |
|---|---|
| `storage/schema.sql` | Equity-side ingestion / scoring / outcomes tables (the original engine). Loaded via `storage.db:init_schema`. |
| `storage/migrations.py` | Versioned ALTERs and new-table additions on top of `schema.sql`. Versions 1-8 as of 2026-05-07. |
| `ml/schema.py` | Equity ML tables (4). Loaded via `ml.schema:create_all_tables`. |
| `crypto/schema.py` | Crypto ML tables (8). Loaded via `crypto.schema:create_all_tables`. |
| `fx/schema.py` | FX ML tables (9). Loaded via `fx.schema:create_all_tables`. |

A live DB query (`SELECT table_name FROM information_schema.tables WHERE
table_schema='main'`) returns ~52 tables. The tables below are grouped by
the engine that owns them.

---

## Equity ML — `ml_*`

Defined in `ml/schema.py`. All four tables key on `(ticker, trade_date)`
or `(ticker, prediction_date, model_id, horizon)`.

### `ml_labels`

Forward-return labels for each ticker × trade_date. Generated nightly so
predict can join in past data for outcome filling.

| Column | Type | Notes |
|---|---|---|
| `ticker` | VARCHAR | PK |
| `trade_date` | DATE | PK |
| `close_price` | DOUBLE | |
| `fwd_return_5d`, `fwd_return_10d`, `fwd_return_20d` | DOUBLE | Forward return over the window. |
| `fwd_max_return_5d/10d/20d` | DOUBLE | Window high / reference - 1. |
| `fwd_max_drawdown_5d/10d/20d` | DOUBLE | Window low / reference - 1. |
| `label_5d_3pct`, `label_5d_5pct` | BOOLEAN | Hit-rate label: did fwd_max_return ≥ X within window. |
| `label_10d_5pct`, `label_10d_8pct` | BOOLEAN | |
| `label_20d_5pct`, `label_20d_8pct`, `label_20d_10pct`, `label_20d_15pct` | BOOLEAN | |

**Writer:** `ml/labels.py` (called by `ml backfill-labels`).
**Reader:** `ml/train.py`, `ml/predict.py:fill_outcomes`.

### `ml_features`

32 numeric features for each ticker × trade_date. Inputs to `ml_predictions`.

PK: `(ticker, trade_date)`. Columns include returns over multiple horizons,
RSI, drawdown from 52w high, MA distances, Bollinger position, realized
vol, ATR, relative volume, beta, VIX level, yield curve, filing counts
(8K, Form 4), days since last 10-Q, log market cap, P/B. Full list in
`ml/schema.py:SCHEMA_ML_FEATURES`.

**Writer:** `ml/features.py:build_features` (called by `ml backfill-features`).
**Reader:** `ml/train.py`, `ml/predict.py`.

### `ml_predictions`

One row per (ticker, prediction_date, model_id, horizon). Outcomes are
filled in later by `fill_outcomes` once the forward window closes.

| Column | Type | Notes |
|---|---|---|
| `ticker` | VARCHAR | PK |
| `prediction_date` | DATE | PK |
| `model_id` | VARCHAR | PK; references `ml_model_runs.model_id`. |
| `horizon` | VARCHAR | PK; e.g. "5d", "10d", "20d". |
| `predicted_probability` | DOUBLE | XGBoost `predict_proba` output. |
| `prediction_threshold` | DOUBLE | The decision threshold for the active model. |
| `sector` | VARCHAR | Snapshot at prediction time (from `companies`). |
| `market_cap_bucket` | VARCHAR | Snapshot at prediction time. |
| `actual_max_return` | DOUBLE | Filled in by `fill_outcomes`. |
| `actual_max_drawdown` | DOUBLE | Filled in by `fill_outcomes`. |
| `actual_hit` | BOOLEAN | Filled in: did fwd return clear the threshold? |
| `outcome_filled_at` | TIMESTAMP | NULL until backfill runs. |

**Writer:** `ml/predict.py:score_universe` (writes), `ml/predict.py:fill_outcomes` (updates outcome columns).
**Reader:** `dashboard/services/queries.py`, `health/ml_checks.py`.

### `ml_model_runs`

One row per training run (per horizon × threshold). `is_active=TRUE`
flags the model that `predict` will use.

PK: `model_id` (string like `label_10d_5pct_20260505_092031`).

Columns: train/test date ranges, sample counts, precision/recall/F1/AUC at
threshold, base rate, lift over base, feature importance JSON, joblib
path under `models/saved/`, `is_active` flag.

**Writer:** `ml/train.py:train_walk_forward`.
**Reader:** `ml/predict.py` (loads active model), dashboard "ML predictions" tab.

---

## Crypto ML — `crypto_*`

Defined in `crypto/schema.py`. Engine has 8 tables: 4 inputs (prices,
funding, open interest, universe) + 4 ML (labels, features, predictions,
model runs).

### `crypto_prices_daily`

Daily OHLCV from Binance.

PK: `(symbol, trade_date)`. Columns: open, high, low, close, volume,
trades, taker_buy_volume, source (default 'binance'), created_at.

**Writer:** `crypto/ingestion/binance_client.py` (called by `crypto backfill-prices`).
**Reader:** `crypto/ml/labels.py`, `crypto/ml/features.py`, `crypto/ml/predict.py`.

### `crypto_funding_rates`

Perpetual funding rates from Binance, 8h cadence.

PK: `(symbol, funding_time)`. Columns: funding_rate, mark_price, created_at.

**Writer:** `crypto/ingestion/` (called by `crypto backfill-funding`).
**Reader:** `crypto/ml/features.py` (drives funding_rate_current/avg/zscore features).

### `crypto_open_interest`

Daily aggregate open interest in contracts and notional value.

PK: `(symbol, trade_date)`. Columns: open_interest, open_interest_value, created_at.

**Writer:** `crypto/ingestion/` (called by `crypto backfill-oi`).
**Reader:** `crypto/ml/features.py` (drives oi_change_1d/3d/7d, oi_price_divergence_3d).

### `crypto_universe`

The active set of symbols the predict pipeline scores. Ranked by 30-day
average daily volume.

PK: `symbol`. Columns: base_asset, avg_daily_volume_30d, rank_by_volume,
is_active, added_date, removed_date, created_at.

**Writer:** `crypto build-universe` CLI.
**Reader:** `crypto/ml/predict.py` (filters to is_active=TRUE).

### `crypto_ml_labels`

Forward returns and hit-rate labels at 1d/3d/5d/10d horizons. Threshold
levels are wider than equity (5/10/15/20%) reflecting crypto vol. All the
`fwd_*` and `label_Nd_Xpct` columns are **close-based** (forward daily
closes).

**Knockout (triple-barrier) columns** (added 2026-05-11, ADR-023, populated
by a forward-walk pass in `compute_labels` — see `crypto/ml/knockout_label.py`
and `crypto/ml/KNOCKOUT_LABEL_SPEC.md`): `label_{5d,10d}_knockout BOOLEAN` —
True iff over the next N trading days the **intraday high** reaches
`close·(1 + KNOCKOUT_TP)` before the intraday low reaches `close·(1 + KNOCKOUT_SL)`
(TP=+0.10, SL=−0.05; same-bar both-touch resolves SL-first; "neither" → False);
`knockout_outcome_{5d,10d} VARCHAR` ∈ `'tp'|'sl'|'neither'`;
`knockout_resolve_day_{5d,10d} INTEGER` — 1-indexed forward bar a barrier was
touched (NULL for `'neither'`). The legacy `label_Nd_10pct` columns are kept
alongside for backward compat. Idempotent `ADD COLUMN IF NOT EXISTS`
migrations in `crypto/schema.py:_CRYPTO_ML_LABELS_MIGRATIONS`.

PK: `(symbol, trade_date)`. Full column list in `crypto/schema.py:SCHEMA_CRYPTO_ML_LABELS`.

**Writer:** `crypto/ml/labels.py` (called by `crypto backfill-labels`).
**Reader:** `crypto/ml/train.py`, `crypto/ml/predict.py:fill_outcomes` (legacy columns; the knockout columns become a training target in phase 2).

### `crypto_ml_features`

35 features per symbol × trade_date. Includes spot features (returns,
RSI, vol, MA distances, Bollinger), cross-coin features (`return_vs_btc_*`,
`beta_to_btc_30d`), funding-rate features, OI features, and BTC market
features (dominance, return, vol).

PK: `(symbol, trade_date)`.

**Writer:** `crypto/ml/features.py` (called by `crypto backfill-features`).
**Reader:** `crypto/ml/train.py`, `crypto/ml/predict.py`.

### `crypto_ml_predictions`

Schema parallels `ml_predictions` (no `sector` column; crypto uses
`market_cap_bucket` only).

PK: `(symbol, prediction_date, model_id, horizon)`.

**Writer:** `crypto/ml/predict.py:score_universe`, `fill_outcomes`.
**Reader:** dashboard "Crypto predictions" tab.

### `crypto_ml_model_runs`

Same shape as `ml_model_runs`. Models stored under `models/saved/crypto/`.

**Additional column (crypto-only).** `promotion_status VARCHAR` — set
by the retrain validation gate (`crypto/ml/validation_gate.py`).
Values: `'pending'` (just trained, gate not yet run), `'promoted'`
(gate passed, model is active), `'promotion_blocked'` (gate failed,
model left `is_active=false`, prior active stays live), or `NULL`
(rows predating the gate; treated as promoted for backward
compatibility). See ADR-019.

### `crypto_signal_exclusions`

Audit log of coins suppressed by the post-parabolic exclusion filter
(`crypto/ml/postparabolic_filter.py`) at prediction-export time. One
row per `(export_date, symbol, model_id)`; UPSERTed so a re-run of the
export for the same date is idempotent. `dd90` = `drawdown_from_90d_high`,
`ret60` = `return_60d` (the feature values that tripped the gate);
`raw_probability` = the model's calibrated probability before
suppression; `reason` = stable token (`'post_parabolic'`).

PK: `(export_date, symbol, model_id)`. DDL in `crypto/schema.py:SCHEMA_CRYPTO_SIGNAL_EXCLUSIONS`.

**Writer:** `crypto/exports/write_daily_predictions.py:build_predictions`.
**Reader:** none yet (dashboard expander is a deferred phase-2 item).
See ADR-021.

### `crypto_data_quality_reports`

Exceptions log written by the OHLCV plausibility / volume-cliff guard
(`pipelines/data_quality_guard.py`, via `crypto check-data-quality`).
One row per flagged `(date, symbol, check_name)`; UPSERTed (a re-run for
the same date is idempotent); clean days write nothing. `check_name` is
`'volume_cliff'` / `'range_collapse'` / `'trade_count_cliff'` (severity
`'warn'`, `symbol` = the coin, `expected` = trailing-20-day median,
`observed` = today's value), or `'systemic_corruption'` (severity
`'critical'`, `symbol` = `'__systemic__'`, `expected` =
`SYSTEMIC_FLAG_RATIO`, `observed` = flagged fraction of the evaluable
universe). A systemic row means the daily pipeline was blocked that day.

PK: `(date, symbol, check_name)`. DDL in `crypto/schema.py:SCHEMA_CRYPTO_DATA_QUALITY_REPORTS`.

**Writer:** `pipelines/data_quality_guard.py:persist_report` (called by the
`crypto check-data-quality` CLI command).
**Reader:** none yet (dashboard view is a deferred phase-2 item).
See ADR-022.

---

## FX ML — `fx_*`

Defined in `fx/schema.py`. 9 tables. Time-series cadence is **hourly**
(not daily), and the engine has additional tables for signal/position
state because FX runs an interactive Telegram bot.

### `fx_prices_hourly`

GBP/EUR OHLC at hourly resolution from Dukascopy bi5 (via subprocess
into `/home/jpcg/ATSRP/`). Single time-series — no `symbol` column.

PK: `datetime_utc`. Columns: date, weekday, hour_utc, gbpeur_open/high/low/close,
tick_count, data_quality (default 'good'), created_at.

**Writer:** `fx/data/refresh.py` (called by `fx refresh-prices`).
**Reader:** `fx/ml/features.py`, `fx/ml/labels.py`, `fx/ml/predict.py`.

### `fx_macro`

Macro indicators from FRED: BoE rate, ECB rate, EUR/USD, GBP/USD.

PK: `(indicator, observation_date)`. Columns: value, source (default 'fred'), created_at.

**Writer:** `fx/data/macro.py` (called by `fx backfill-macro`).
**Reader:** `fx/ml/features.py` (drives boe_rate, ecb_rate, rate_differential, eurusd/gbpusd_return_24h).

### `fx_ml_labels`

Forward pip excursions over 24h and 48h windows; hit-rate labels at
20pip and 30pip thresholds in both directions.

PK: `datetime_utc`. Full column list in `fx/schema.py:SCHEMA_FX_ML_LABELS`.

**Writer:** `fx/ml/labels.py` (called by `fx backfill-labels`).
**Reader:** `fx/ml/train.py`, `fx/ml/predict.py:fill_outcomes`.

### `fx_ml_features`

44 features per hourly bar. Returns at multiple horizons (1h/4h/8h/24h/5d/20d),
RSI 14h/48h, MA distances, Bollinger position, candle anatomy
(body/wick percentages), realized vol, ATR pips, hour-of-day sin/cos,
session flags (London open, NY open, overlap, Asian), distance from
daily high/low, consecutive up/down hours, macro features.

PK: `datetime_utc`.

**Writer:** `fx/ml/features.py` (called by `fx backfill-features`).
**Reader:** `fx/ml/train.py`, `fx/ml/predict.py`.

### `fx_ml_predictions`

One row per (datetime_utc, model_id, direction, horizon). Direction is
"up" or "down"; horizon is "24h" or "48h" → 4 active models.

PK: `(datetime_utc, model_id, direction, horizon)`. Columns:
predicted_probability, prediction_threshold, actual_max_pips,
actual_hit, outcome_filled_at.

**Writer:** `fx/ml/predict.py:score_bar`, `fill_outcomes`.
**Reader:** `fx/ml/signals.py:generate_signal`, dashboard "FX" tab.

### `fx_ml_model_runs`

Same shape as the equity/crypto equivalents. `target_pips` instead of
`target_threshold`. Models in `models/saved/fx/`.

### `fx_signals`

Generated buy/sell signals (after threshold + suppression logic in
`fx/ml/signals.py`). Records what was decided, not just what was
predicted.

PK: `(datetime_utc, signal_type)`. Columns: signal_type (BUY_GBP /
SELL_GBP), prob_up_24h, prob_down_24h, prob_up_48h, prob_down_48h,
gbpeur_price, telegram_sent, telegram_sent_at, outcome_pips_24h,
outcome_pips_48h.

**Writer:** `fx/ml/signals.py:generate_signal`, `fx/bot/telegram_bot.py`.
**Reader:** dashboard "FX" tab.

### `fx_position`

Current position state (used by the Telegram bot for position-aware
alert suppression — don't send BUY_GBP if already long GBP).

Columns: position (string), entry_rate, entry_date, updated_at.
Single-row table by convention.

**Writer:** `fx set-position` CLI; `fx/bot/telegram_bot.py` on confirmed fills.
**Reader:** `fx/bot/telegram_bot.py:send_signal_alert` (suppression gate).

### `fx_alert_state`

Single-row table (id=1) tracking alert global enable/disable + last
send timestamps for per-direction 4h cooldown.

**Writer:** `fx/bot/telegram_bot.py`.
**Reader:** `fx/bot/telegram_bot.py:send_signal_alert`.

---

## Equity ingestion / scoring / outcomes — defined in `storage/schema.sql`

These tables predate the ML rebuild. They power `daily_radar` (the
equity ingestion + scoring path that runs Mon-Fri 23:15 via
`mhde-daily-analysis.service`) and the existing review / outcome flow.

### Reference / catalog

| Table | Purpose |
|---|---|
| `schema_version` | Migration ledger. Each `storage/migrations.py` step inserts a row. |
| `companies` | Universe master record per ticker — name, exchange, sector, industry, ETF/fund/ADR flags, market cap, universe_tier, SEC reporter status, last filing date. PK: `ticker`. |
| `source_runs` | One row per ingestion source × run — status, started_at/finished_at, records_attempted/inserted/failed, error_message. Written by every adapter under `ingestion/`. |

### Time series

| Table | Purpose |
|---|---|
| `prices_daily` | Daily OHLCV. Multi-source: Polygon, Yahoo, Alpha Vantage, Stooq. UNIQUE `(ticker, trade_date)`. Writers: every `ingestion/ingest_*` adapter. Readers: `ml/labels.py`, `ml/features.py`, `ml/predict.py`, `outcomes/tracker.py`. |
| `macro_series` | FRED macro series (VIX, yield curve, etc.). UNIQUE `(series_id, as_of_date)`. |
| `short_interest` | FINRA short interest by ticker × settlement_date. |
| `events` | Earnings, splits, dividends, GDELT news events. |
| `earnings_estimates` | Estimated vs reported EPS / revenue, with surprise %. UNIQUE `(ticker, fiscal_date, source)`. (migration v7) |

### Filings & fundamentals

| Table | Purpose |
|---|---|
| `filings` | SEC filings (8-K, 10-K, 10-Q, Form 4, etc.). Written by `ingestion/ingest_sec.py`. |
| `fundamentals_raw` | Raw XBRL concept facts. Written by `ingestion/ingest_sec_xbrl.py`. |
| `fundamentals_features` | Engineered fundamentals (revenue_growth_yoy, net_margin, dilution_rate, P/E proxy, P/S proxy, data_freshness_days). UNIQUE `(ticker, as_of_date)`. |

### Scoring (legacy MHDE engine, still wired into `daily_radar`)

| Table | Purpose |
|---|---|
| `features` | Long-form per-feature evidence rows (run_id × ticker × feature_group × feature_name). Written by `features/feature_builder.py`. |
| `scores` | Per-(run_id, ticker) component scores (cheap/quality/catalyst/momentum/sentiment/risk_penalty), `total_score`, `tier` (A/B/C/Reject). Written by `scoring/scorecard.py`. |
| `hypotheses` | Per-candidate thesis text + evidence JSON + status. Powers the dashboard candidate list. |
| `rejections` | Per-(run_id, ticker) reasons + risk_flags + missing_data. |
| `candidate_outcomes` | Outcome record per candidate — forward returns at multiple horizons, drawdowns, hit-rate flags, review_status. UNIQUE `(run_id, ticker)`. Updated by `outcomes/tracker.py:update_forward_returns`. |
| `candidate_reviews` | Manual review of candidates with usefulness/thesis/evidence scores and false_positive_reason. UNIQUE `(run_id, ticker)`. |
| `review_notes` | Free-text annotations on a hypothesis or candidate. |
| `move_episodes` | Tracks multi-day momentum runs. Written by missed-opportunity attribution. (migration v8) |

### Pipeline / governance

| Table | Purpose |
|---|---|
| `pipeline_runs` | One row per `daily_radar` execution: counts of sources, candidates per tier, hypotheses, alerts, status. (migration v4) |
| `model_runs` | Per-model-training metadata (legacy, predates `ml_model_runs`). |
| `backtest_runs` | Backtest results (legacy). |
| `llm_runs` | LLM call audit log: provider, model, prompt_version, input/output hashes, tokens, cost. Written by `llm/runner.py` and `missed/catalyst_queue.py`. |
| `alerts` | Outbound alert audit (Telegram, email). |
| `dashboard_actions` | User actions taken in the dashboard (action_type, target_table, target_id, payload, performed_by). |
| `health_checks` | Health-check results (run_id, check_name, status, severity, message). Written by `health/checks.py` (called by `mhde-health-check.service`). |
| `scorecard_experiments` | Proposed scorecard changes with backtest results and approval state (legacy governance, dormant). |
| `promotion_gate_results` | Pre-deployment gate outcomes for model promotion (legacy governance, dormant). |

### Missed-opportunity pipeline

These three feed the daily-analysis catalyst flow.

| Table | Purpose |
|---|---|
| `missed_opportunity_events` | Detected large moves the engine didn't flag. Written by missed-opportunity detector. |
| `missed_opportunity_investigations` | Per-event root cause investigation, optional NVIDIA/OpenAI enrichment. |
| `missed_opportunity_root_causes` | Decomposed root-cause rows (one event → many root_cause records). |

---

## Cross-cutting notes

- **Time conventions.** Equity uses `DATE` keyed by `trade_date`. Crypto
  also uses `DATE`. FX uses `TIMESTAMP` keyed by `datetime_utc`. The FX
  `fwd_*` columns are in **pips** (not percent). All other engines use
  percent or fractional returns.
- **Outcomes filling.** All three engines have a `fill_outcomes` step
  inside their predict module. The window must match the label window
  (one of the recurring bugs documented in `KNOWN_ISSUES.md`).
- **Active model resolution.** Each engine reads the latest
  `is_active=TRUE` row from its `*_model_runs` table to decide which
  joblib to load. The joblib path is in `model_path` and lives under
  `models/saved/{equity,crypto,fx}/`.
- **Migrations are append-only.** `storage/migrations.py:_CURRENT_VERSION`
  must be bumped whenever a new ALTER or CREATE TABLE goes in. Each
  step inserts a `schema_version` row so the migration is idempotent.
- **Single-row tables.** `fx_alert_state` (id=1 with CHECK constraint).
  `fx_position` is conventionally single-row but not constrained — the
  bot truncates and reinserts on update.
- **Reader/writer audit.** A grep-based reader/writer audit lives in
  `.claude/local_scripts/` and can be re-run against new schema additions
  during code review.
