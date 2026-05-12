# Pipeline monitoring

A second observability layer that posts **one Telegram message per pipeline**
showing every step's outcome — 🟢 (ok) / 🔴 (failed) / ⚪ (skipped because an
earlier step failed) — plus a continuous monitor that alerts only on red. It
complements the daily health-check (*"is the system running at all"*) and the
Session-6 `pipeline-execution` drift monitor (*"are the row counts roughly
right"*) by answering *"did **today's** run actually produce the right outputs,
all the way through to engine entry"*.

Built after KI-138 (ADR-026): a regression where every script exited 0, the
crypto predictions export froze, the engine rejected the stale file, and no
positions were placed — invisible for ~24h to the existing monitors. Every
check here is **outcome-based** (reads the DB / a file), never exit-code-based.

Source: `monitoring/pipeline_monitor/` (`core.py`, `checks/{crypto,equity,fx}.py`,
`daily_runner.py`, `continuous_runner.py`). Telegram path reuses
`monitoring/alert.py` (`send_text`) → `fx.bot.telegram_bot.send_message`.
`MONITORING_DRY_RUN=true` suppresses the real send.

## Message format

```
🟢/🔴 <Pipeline> Pipeline <YYYY-MM-DD> <HH:MM UTC>
🟢/🔴/⚪ <step name> — <detail>
...
```

The header is 🔴 iff any step is 🔴. The daily monitors send this message
**every run, green or red** (a daily green heartbeat). The rendered message is
also printed to **stdout** (so it lands in the systemd journal, and is visible
on a manual or `MONITORING_DRY_RUN=true` invocation) — the continuous monitor
prints it too even when all green; only the Telegram *send* is suppressed.
Exit status: 0 if green, 1 if any step red.

## What each monitor checks

### Crypto pipeline — `main.py monitor crypto-pipeline`, daily 06:40 UTC

Fires after the 00:30 predict chain, the 06:15 prediction export, and the
~06:30 engine entry. Date conventions: under cap-at-today-1 (ADR-022) OHLCV /
features / `prediction_date` are all `today-1`; the export's `export_date` is
`today` and `features_as_of_date` is `today-1` (ADR-025).

| # | Step | Green when |
|---|---|---|
| 1 | OHLCV ingestion | `MAX(trade_date)` in `crypto_prices_daily` ≥ today-1 |
| 2 | Data-quality guard | no `systemic_corruption` row in `crypto_data_quality_reports` for the last 2 days (per-symbol warnings don't block) |
| 3 | Funding / OI ingestion | `MAX(funding_time)` in `crypto_funding_rates` and `MAX(trade_date)` in `crypto_open_interest` are ≥ today-1 |
| 4 | Feature pipeline | rows exist in `crypto_ml_features` for `MAX(trade_date)` ≥ today-1 |
| 5 | Model predictions | rows exist in `crypto_ml_predictions` (active model) for `prediction_date` ≥ today-1 |
| 6 | Outcome tagging | no matured prediction (active model, forward window closed ≥ 2 days ago) still has `actual_hit` NULL |
| 7 | Export predictions | `data/exports/predictions_latest.json` resolves to a file whose `export_date == today` and that has ≥ 1 prediction |
| 8 | Engine ingest | the crypto-trading-engine ran an `entry` phase today and the most recent one succeeded (`engine_runs`, read-only) |
| 9 | Engine entry / positions | ≥ 1 position with `entry_date == today` in the engine `positions` table — or 0 *and* the book is already at `max_concurrent` (from `active_spec.json`) |

Step 7 is the one that catches the KI-138 shape (stale `predictions_latest.json`).
Steps 8–9 read the engine DuckDB read-only (`CRYPTO_ENGINE_DB_PATH`, ADR-020); if
it's unreachable they go red, they never block the rest of the message.

### Equity pipeline — `main.py monitor equity-pipeline`, daily 01:00 UTC

Fires after `mhde-predict.service` (00:15 UTC) has chained `ml backfill-features`
→ `ml predict` over the prices ingested by the previous evening's
`mhde-daily-analysis` (23:15 Mon-Fri). "Expected date" = the most recent closed
market day (`pipelines.market_calendar.expected_equity_prediction_date`,
weekend-rolling).

| # | Step | Green when |
|---|---|---|
| 1 | Equity data ingestion | `MAX(trade_date)` in `prices_daily` ≥ the most recent closed market day |
| 2 | Feature pipeline | rows exist in `ml_features` for `MAX(trade_date)` ≥ that day |
| 3 | Model predictions | rows exist in `ml_predictions` (active model) for `prediction_date` ≥ that day |
| 4 | Dashboard data refresh | the daily-analysis output `data/processed/prediction_vs_actual_rows.csv` was modified within the last 4 days (tolerates a weekend + a market holiday) |

### FX pipeline — `main.py monitor fx-pipeline`, daily 12:10 UTC

A once-a-day snapshot (FX itself runs hourly at :05). Freshness reuses
`pipelines.freshness.check_fx_freshness` (forex-closed-window aware, KI-128).

| # | Step | Green when |
|---|---|---|
| 1 | FX bar ingestion | newest `fx_prices_hourly` bar within the live 2-hour threshold (or, while forex is closed, at/after the Friday 21:00 close-floor) |
| 2 | Signal generation | `MAX(datetime_utc)` in `fx_signals` ≥ `MAX(datetime_utc)` in `fx_prices_hourly` |

### Continuous monitor — `main.py monitor continuous`, every 30 min

**Silent when all green; sends one message (showing all checks) when any is red.**
Independent checks — no cascade.

| Step | Red when |
|---|---|
| FX hourly bar freshness | newest `fx_prices_hourly` bar is stale (same logic as the FX-pipeline step 1) |
| Crypto engine monitor timer | the engine's per-minute `monitor` phase hasn't had a success in > 15 min (engine looks down) |
| Crypto engine entry timer | it's past 08:00 UTC and the engine has no successful `entry` run today |

The engine `reconcile` timer (23:00 UTC) is **not** checked — it's disabled
pending RECONCILE-001; flip `CHECK_ENGINE_RECONCILE` in `continuous_runner.py`
when it's re-enabled. There is deliberate overlap with the
`paper-trading-drift` monitor's engine-liveness arm (ADR-020) — both are cheap
and read-only.

## Systemd units

| Unit | Schedule (UTC) | Command |
|---|---|---|
| `mhde-crypto-pipeline-monitor.{service,timer}` | daily 06:40 | `monitor crypto-pipeline` |
| `mhde-equity-pipeline-monitor.{service,timer}` | daily 01:00 | `monitor equity-pipeline` |
| `mhde-fx-pipeline-monitor.{service,timer}` | daily 12:10 | `monitor fx-pipeline` |
| `mhde-continuous-monitor.{service,timer}` | every 30 min (`*:0/30`) | `monitor continuous` |

Install: copy to `~/.config/systemd/user/` (or `/etc/systemd/system/`),
`daemon-reload`, `enable --now` the `.timer`s. The crypto and continuous units
need `CRYPTO_ENGINE_DB_PATH` (already in the unit files). Logs:
`data/logs/pipeline_monitor_{crypto,equity,fx,continuous}.log`.

## Limitations (v1) — see KI-139

- **No auto-remediation.** The monitor reports; the operator acts.
- **No dashboard view.** Telegram only in v1.
- **Equity "dashboard data refresh" is coarse** — a 4-day mtime tolerance on
  one daily-analysis output file. A 3-day-stale dashboard on a normal week is
  not flagged by this step alone (the health-check / `pipeline-execution`
  monitor would still catch a longer outage).
- **"0 positions opened today" → red with a note**, not a precise diagnosis —
  the engine DB carries no machine-readable "why 0" field. It's softened to
  green only when the book is already at `max_concurrent`.
- **Engine `reconcile` timer not checked** (RECONCILE-001).
