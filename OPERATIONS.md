# MHDE Operations

Runbook. Procedures for deploying, debugging, and recovering. See
`INFRASTRUCTURE.md` for the static topology (services, ports, secrets);
this doc covers what to *do*.

---

## Daily smoke (start here when something feels off)

```bash
# 1. All MHDE services running?
systemctl --user list-units --type=service --state=running | grep mhde
sudo systemctl list-units --type=service --state=running | grep mhde

# 2. Latest data per engine (freshness)
venv/bin/python .claude/local_scripts/test_dashboard_queries.py 2>&1 | tail -20
# Or: venv/bin/python -m pipelines.health_check

# 3. Public dashboard reachable
curl -sI https://mhde.duckdns.org/ | head -3

# 4. Disk + DB size
df -h /home/jpcg
du -sh /home/jpcg/MHDE/data/mhde.duckdb

# 5. Last firings
systemctl --user list-timers | grep mhde
sudo systemctl list-timers | grep mhde
```

If any of those fail, jump to the relevant section below.

---

## Manually running a pipeline

All commands run from `/home/jpcg/MHDE` with `venv/bin/python`. Never
activate the venv (per project policy).

### Equity ML

```bash
# Score today (assumes daily-analysis already populated prices_daily
# AND ml backfill-features has run for the latest trade_date)
venv/bin/python main.py ml predict
# Force a date
venv/bin/python main.py ml predict --date 2026-05-06
# Skip outcome backfill (faster; dev only)
venv/bin/python main.py ml predict --skip-outcomes

# Backfill features for a missing day before predict can run
venv/bin/python main.py ml backfill-features
# Backfill labels (forward returns) — runs on past data
venv/bin/python main.py ml backfill-labels

# Walk-forward retrain (writes a new ml_model_runs row)
venv/bin/python main.py ml train --label label_10d_5pct --horizon 10d --threshold 0.05
```

#### Two feature systems — read this when the dashboard "shows stale ML predictions"

The equity stack has **two parallel feature systems** that don't share
state. ARCHITECTURE.md has the full table; the operational summary:

| Table | Refreshed by | Runs at | Used by |
|---|---|---|---|
| `prices_daily` | `mhde-daily-analysis.service` (`daily-radar` stage `ingestion`) | Mon-Fri 23:15 | every downstream stage |
| `features` (legacy) | `daily-radar` stage `features` (uses `features/feature_builder.py`) | Mon-Fri 23:15 (right after ingestion) | legacy `scoring/scorecard.py` |
| `ml_features` | `main.py ml backfill-features` (uses `ml/features.py`) | inside `mhde-predict.service` ExecStart, daily 21:00 | the ML engine (`ml/predict.py`) |
| `ml_predictions` | `main.py ml predict` (uses `ml/predict.py`) | `mhde-predict.service`, daily 21:00 (after `ml backfill-features`) | dashboard "ML predictions" tab |

Schedule order each weekday:
```
21:00 — mhde-predict.service:
            ml backfill-features (writes ml_features for new prices)
            ml predict           (writes ml_predictions for the latest ml_features.trade_date)
23:15 — mhde-daily-analysis.service:
            daily-radar (ingest prices, write `features`/scores)
            missed prediction-vs-actual / enrich-root-causes / priority-refresh-queue / catalyst-queue
```

**Implication.** The 21:00 predict run reads the previous evening's
prices and writes ML predictions a day behind real-time. That's
expected, not stale.

**Debugging "dashboard shows stale ML predictions":**
1. Check `MAX(prediction_date)` in `ml_predictions` — the dashboard
   shows this as the dropdown's max.
2. Check `MAX(trade_date)` in `ml_features` — predict picks this as
   the prediction_date when none is given.
3. If `ml_features` is older than expected: was `ml backfill-features`
   run since the last price ingest? Was the latest `mhde-predict.service`
   firing successful? `journalctl -u mhde-predict --since "1 day ago"`.
4. If both are stale: was `mhde-daily-analysis.service` healthy? See
   "Stale data" below.

**Manual end-to-end refresh** (when you need fresh predictions outside
the systemd schedule, e.g. after a retrain):
```bash
# Full ingest — slow because of SEC; skip with --skip-sec-fundamentals
venv/bin/python main.py run daily-radar --skip-sec-fundamentals    # ~7 min

# Then bring ml_features and ml_predictions up to date
venv/bin/python main.py ml backfill-features                       # ~5 min
venv/bin/python main.py ml predict --skip-outcomes                 # ~2s
```

### Crypto ML

The full chain matches what `mhde-crypto-predict.service` runs:

```bash
venv/bin/python main.py crypto backfill-prices
venv/bin/python main.py crypto backfill-funding
venv/bin/python main.py crypto backfill-oi
venv/bin/python main.py crypto backfill-labels
venv/bin/python main.py crypto backfill-features
venv/bin/python main.py crypto predict

# Or just the predict step if data is fresh
venv/bin/python main.py crypto predict
```

### FX ML

```bash
# Full chain (matches mhde-fx-predict.service)
venv/bin/python main.py fx refresh-prices
venv/bin/python main.py fx backfill-features
venv/bin/python main.py fx backfill-labels
venv/bin/python main.py fx predict --no-alert     # NOTE: --no-alert in dev

# With Telegram alert (will hit production chat)
venv/bin/python main.py fx predict
```

### Daily-analysis (equity ingest path)

```bash
# Whole chain via the same wrapper systemd uses
.claude/local_scripts/run_mhde_daily_analysis.sh

# Or just the radar step
venv/bin/python main.py run daily-radar
```

### Health check

```bash
venv/bin/python main.py system health-check
```

---

## Recovery procedures

### DuckDB lock contention

**Symptom.** A pipeline fails immediately with
`duckdb.IOException: Could not set lock on file`.

**Cause.** Another process holds the writer lock — usually
`mhde-daily-analysis.service` (writes for ~30+ minutes nightly).

**Detection.**
```bash
fuser /home/jpcg/MHDE/data/mhde.duckdb 2>/dev/null
lsof /home/jpcg/MHDE/data/mhde.duckdb 2>/dev/null
```

**Recovery.**
- Hourly services (`mhde-fx-predict`) auto-retry with 30s/60s/120s
  backoff via `storage/db.py:_connect_with_lock_retry`. Just wait for
  the next firing.
- For a manual run that needs to push through, wait for
  daily-analysis to finish (`journalctl --user -u
  mhde-daily-analysis -f`) then retry.
- Never `kill -9` a writer — the DB may be left in a recovering state.
  Use `systemctl stop` if absolutely needed.

### Stale data — pipeline skips with `DATA STALE`

**Symptom.** Log line `DATA STALE — skipping equity prediction.` (or
crypto / FX equivalent).

**Threshold per engine** (see `pipelines/freshness.py`):
- equity: `prices_daily` more than 2 trading days old.
- crypto: `crypto_prices_daily` more than 1 calendar day old.
- FX: warning only — predict still runs (intentional).

**Recovery.**

```bash
# Check what's actually there
venv/bin/python -c "
import duckdb
c = duckdb.connect('data/mhde.duckdb', read_only=True)
print('equity:', c.execute('SELECT MAX(trade_date) FROM prices_daily').fetchone())
print('crypto:', c.execute('SELECT MAX(trade_date) FROM crypto_prices_daily').fetchone())
print('fx    :', c.execute('SELECT MAX(datetime_utc) FROM fx_prices_hourly').fetchone())
"

# Equity — force ingestion outside daily-analysis schedule
venv/bin/python main.py run daily-radar --skip-fundamentals  # if SEC step is slow

# Crypto — re-run the chain
for cmd in backfill-prices backfill-funding backfill-oi backfill-labels backfill-features; do
    venv/bin/python main.py crypto $cmd
done

# FX — refresh from Dukascopy
venv/bin/python main.py fx refresh-prices
```

If ingestion itself is failing, check the source-specific section
below.

### Active model file missing

**Symptom.** Predict logs `model file not found at models/saved/...`
or fails to load joblib.

**Detection.**
```bash
venv/bin/python -c "
import duckdb
c = duckdb.connect('data/mhde.duckdb', read_only=True)
for tbl in ('ml_model_runs', 'crypto_ml_model_runs', 'fx_ml_model_runs'):
    rows = c.execute(f'SELECT model_id, model_path, is_active FROM {tbl} WHERE is_active').fetchall()
    print(tbl, rows)
"
ls models/saved/ models/saved/crypto/ models/saved/fx/ 2>/dev/null
```

**Recovery.**
- Re-train each affected horizon. As of Session 7 all three engines'
  train commands auto-deactivate the prior `is_active=TRUE` row for
  the same `(horizon, target_threshold/target_pips)` tuple before
  inserting the new one — no manual UPDATE needed:

  ```bash
  # Equity (one per active horizon)
  venv/bin/python main.py ml train --label label_5d_3pct  --horizon 5d  --threshold 0.03
  venv/bin/python main.py ml train --label label_10d_5pct --horizon 10d --threshold 0.05
  venv/bin/python main.py ml train --label label_20d_5pct --horizon 20d --threshold 0.05
  # Crypto / FX use their own retrain commands which loop over horizons.
  venv/bin/python main.py crypto retrain
  venv/bin/python main.py fx retrain
  ```

  Each equity train is ~10s wall-clock; crypto/fx retrains chain over
  multiple horizons and take longer.

- After retraining, run the smoke monitor to confirm:

  ```bash
  MONITORING_DRY_RUN=true venv/bin/python main.py monitor smoke
  # exit 0 = OK
  ```

- If for some reason a stale `is_active=TRUE` row survives (e.g., on
  an older DB before Session 7), the regression test
  `tests/regression/test_schema_consistency.py::test_active_model_paths_resolve`
  will fail and tell you exactly which path doesn't resolve. Fix with
  a manual `UPDATE *_model_runs SET is_active=false WHERE model_id = …`.

### Telegram alerts not arriving (FX bot side)

**Symptom.** FX predict logs say signal generated, but no Telegram
message.

**Checks (in order):**

1. `fx_alert_state.alerts_enabled` is TRUE:
   ```bash
   venv/bin/python -c "
   import duckdb
   c = duckdb.connect('data/mhde.duckdb', read_only=True)
   print(c.execute('SELECT * FROM fx_alert_state').fetchone())
   "
   ```
2. Within 4h cooldown? `last_buy_alert_at` / `last_sell_alert_at`
   compared to "now".
3. Position-aware suppression — `fx_position` matches the signal
   direction.
4. `mhde-fx-bot.service` is running (the bot also handles outbound
   sends in some configurations).
5. Credentials reachable: `cat /home/jpcg/ATSRP/.env | grep TELEGRAM_`
   (don't echo the values to logs).

**Toggle alerts via the bot:** in Telegram, send `/alertson` or
`/alertsoff` to the bot.

### Dashboard returns stale data

**Symptom.** Dashboard shows old predictions; database has newer rows.

**Cause.** Streamlit's session caching, browser cache, or PWA cache.

**Recovery.**
```bash
# Force a Streamlit restart
systemctl --user restart mhde-streamlit
# Or just the relay if 502s
systemctl --user restart mhde-streamlit-relay
```

On the client: hard-refresh (Ctrl-F5). On mobile PWA: force-close from
recents and reopen; if still stale, clear site data in Chrome.

### nginx 502 on `/`

```bash
# Check the unix socket exists and is owned correctly
ls -la /tmp/mhde-relay/streamlit.sock

# Restart in order
systemctl --user restart mhde-streamlit
systemctl --user restart mhde-streamlit-relay
sudo docker exec homeboard-nginx-1 nginx -s reload  # last resort
```

---

## Deploy procedures

### Pulling a code change to the VPS

```bash
cd /home/jpcg/MHDE
git fetch origin
git pull --ff-only origin master   # never force, never merge
```

### Restarting after a code change

The decision matrix (which services need to restart):

| Files changed | Restart |
|---|---|
| `dashboard/*` | `systemctl --user restart mhde-streamlit` |
| `fx/bot/*` | `sudo systemctl restart mhde-fx-bot` |
| `pipelines/freshness.py`, `storage/db.py` | (no restart) — picked up next firing |
| `pipelines/{ml,crypto,fx}_prediction_pipeline.py` | (no restart) — picked up next firing |
| `ml/*`, `crypto/*`, `fx/*` (non-bot) | (no restart) — picked up next firing |
| `systemd/*.service` or `.timer` | Copy to `/etc/systemd/system/` (sudo) or `~/.config/systemd/user/`, then `daemon-reload` and `restart`. |
| `.env` | Restart any service that reads it (not the systemd unit env vars). |

### Deploying a new systemd unit

```bash
# System-level
sudo cp systemd/mhde-NEW.service /etc/systemd/system/
sudo cp systemd/mhde-NEW.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mhde-NEW.timer
sudo systemctl status mhde-NEW.timer

# User-level (no User= / Group= lines! see INFRASTRUCTURE.md gotchas)
cp systemd/mhde-NEW.service ~/.config/systemd/user/
cp systemd/mhde-NEW.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mhde-NEW.timer
```

### Updating dashboard auth credentials

Auth env vars live as `Environment=` lines in
`~/.config/systemd/user/mhde-streamlit.service`. To rotate:

```bash
NEW_HASH=$(echo -n "newpassword" | sha256sum | awk '{print $1}')
# Edit ~/.config/systemd/user/mhde-streamlit.service:
#   Environment=MHDE_DASHBOARD_PASSWORD_HASH=<NEW_HASH>
systemctl --user daemon-reload
systemctl --user restart mhde-streamlit
```

---

## Inspecting prediction history

### What did the equity model predict for ticker X on date Y?

```bash
venv/bin/python -c "
import duckdb
c = duckdb.connect('data/mhde.duckdb', read_only=True)
rows = c.execute('''
SELECT prediction_date, model_id, horizon, predicted_probability,
       actual_max_return, actual_hit, outcome_filled_at
FROM ml_predictions
WHERE ticker = ? AND prediction_date = ?
ORDER BY model_id, horizon
''', ['AAPL', '2026-05-06']).fetchall()
for r in rows: print(r)
"
```

### What's the rolling precision per horizon?

```bash
venv/bin/python -c "
import duckdb
c = duckdb.connect('data/mhde.duckdb', read_only=True)
rows = c.execute('''
SELECT horizon,
       COUNT(*) AS n_filled,
       SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS n_hit,
       AVG(predicted_probability) AS avg_prob
FROM ml_predictions
WHERE outcome_filled_at IS NOT NULL
  AND prediction_date >= CURRENT_DATE - INTERVAL 30 DAY
GROUP BY horizon
ORDER BY horizon
''').fetchall()
for r in rows: print(r)
"
```

### Why did predict skip a day?

```bash
# The freshness skip is logged — check the engine's log
tail -100 data/logs/equity_predict.log | grep -i "stale\|skip"
journalctl --user -u mhde-fx-predict --since "1 day ago" | grep -i "stale"
```

### Why did the FX bot not fire an alert?

```bash
# Latest signal generated
venv/bin/python -c "
import duckdb
c = duckdb.connect('data/mhde.duckdb', read_only=True)
print(c.execute('SELECT * FROM fx_signals ORDER BY datetime_utc DESC LIMIT 5').fetchdf())
print('---')
print(c.execute('SELECT * FROM fx_alert_state').fetchone())
print('---')
print(c.execute('SELECT * FROM fx_position').fetchone())
"
```

If `signal_type` is set but `telegram_sent=FALSE`, it was suppressed by
the bot — by alerts_enabled, cooldown, or position match (in that order).

---

## Source-specific ingestion debugging

### Yahoo Finance (equity)

`ingestion/ingest_yahoo_historical.py`. Hits the public Yahoo endpoint
directly — no API key. Rate-limit aware. If failures spike, check
`tail -50 data/logs/server.log` and look for HTTP 429 or 5xx.

### Polygon (equity)

`ingestion/ingest_prices.py`. Needs `POLYGON_API_KEY`. Free tier has
strict rate limits (~5 calls/min). Check call counts in `source_runs`:

```sql
SELECT use_case, status, records_attempted, records_inserted, error_message
FROM source_runs
WHERE source_name = 'polygon'
ORDER BY started_at DESC LIMIT 10;
```

### Alpha Vantage (equity)

`ingestion/ingest_prices.py`. `data/processed/alpha_vantage_daily_usage.json`
tracks the daily 25-call ceiling. Don't exceed it in dev runs.

### Binance (crypto)

`crypto/ingestion/binance_client.py`. Public API; no key needed for
most endpoints. Watch for HTTP 451 (geographic block) or 429.

### Dukascopy (FX)

`fx/data/refresh.py` shells out to `/home/jpcg/ATSRP/`. Their bi5
fetcher is brittle — if it returns 0 bars, retry; if it consistently
fails, the upstream is most likely 404'ing for a recent hour (data
sometimes lags by 30-60 min).

### FRED (FX macro + equity macro)

`fx/data/macro.py` and `ingestion/ingest_macro.py`. Needs `FRED_API_KEY`.
Series IDs are hardcoded; if FRED renames a series, the corresponding
column will go NULL until updated.

### SEC EDGAR (filings + fundamentals)

`ingestion/ingest_sec.py`. Polite scraping with `MHDE/1.0` user agent.
SEC enforces ~10 req/sec — adapter has its own throttle.

### GDELT (events)

`ingestion/ingest_gdelt.py`. CSV.zip files updated every 15 min by
GDELT. If a daily file is missing, the adapter logs and moves on (no
hard failure).

---

## When to escalate vs wait

| Issue | Wait? | Escalate? |
|---|---|---|
| Hourly FX firing fails once | Yes — auto-retries | If 3+ consecutive failures |
| Daily-analysis takes 90+ min | Yes — first time after a long gap | If 3+ hours, check Yahoo / Polygon throttle |
| Dashboard 502 transient | Restart streamlit-relay | If persists after restart |
| Telegram alert delayed 30s | Yes | Never |
| `mhde-fx-bot.service` keeps restarting | No | Check `journalctl --user -u mhde-fx-bot` for stack trace; likely token issue |
| DuckDB file size grows >2 GB/week | Yes | If >5 GB total, check for runaway INSERTs (probably `llm_runs` or `features`) |
| Dashboard shows different number than `SELECT` | No | Real bug — file in `KNOWN_ISSUES.md` |

---

## Monitors (Session 6)

Six runtime monitors fire Telegram alerts on detected anomalies. Source
under `monitoring/`, dispatched via `main.py monitor <name>`. Schedules
are systemd-driven from `systemd/mhde-monitor-*.{service,timer}`.

### Monitor catalog

| Monitor | Schedule (UTC) | Module | What it checks |
|---|---|---|---|
| `dashboard-consistency` | every 6h at :30 | `monitoring/dashboard_consistency.py` | Dashboard query layer matches direct DB query (counts, latest-date rows). |
| `pipeline-execution` | hourly at :40 | `monitoring/pipeline_execution.py` | Each engine has a fresh-enough latest write + row count ≥ 50% of 14-day rolling avg. |
| `config-drift` | daily 12:15 | `monitoring/config_drift.py` | Repo `systemd/*` ↔ deployed copies under `/etc/systemd/system` and `~/.config/systemd/user`. |
| `model-performance` | daily 13:15 | `monitoring/model_performance.py` | Rolling 7d precision per active model ≥ 0.8× walk-forward baseline. |
| `data-quality` | daily 02:00 | `monitoring/data_quality.py` | Per engine: distinct ticker / symbol count on latest day ≥ 80% of 14-day avg. |
| `smoke` | hourly at :50 | `monitoring/smoke_test.py` | DB opens, every active joblib loads, `dashboard.services.queries.get_overview_stats` returns. |

Schedules are staggered to avoid the FX hourly :05 firing window and the
nightly daily-analysis 23:15 lock window.

### Manual invocation

```bash
# Always invoke via the monitor CLI group
venv/bin/python main.py monitor smoke

# Dry-run mode — compute the alert payload but never call Telegram
MONITORING_DRY_RUN=true venv/bin/python main.py monitor smoke

# All six in sequence (useful for end-to-end exercising on a fresh deploy)
for m in dashboard-consistency pipeline-execution config-drift \
         model-performance data-quality smoke; do
    MONITORING_DRY_RUN=true venv/bin/python main.py monitor "$m"
done
```

Exit code 0 = ok, 1 = warn or fail. The systemd unit interprets non-zero
as "service failed" — check `journalctl --user -u mhde-monitor-X.service`
for the alert payload that was logged.

### Deploying the monitors

Deployed 2026-05-07 at **system level** (`/etc/systemd/system/`),
parallel to the per-engine predict services. System-level was
chosen over user-level so the monitors run regardless of user
session — same reliability tier as `mhde-fx-bot` and the predict
services.

The unit files have `User=jpcg` so they execute as the same user
the predict services run as.

To redeploy (e.g., after editing a unit file in `systemd/`):

```bash
cd /home/jpcg/MHDE
sudo cp systemd/mhde-monitor-*.service systemd/mhde-monitor-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
    mhde-monitor-dashboard.timer \
    mhde-monitor-pipeline.timer \
    mhde-monitor-config-drift.timer \
    mhde-monitor-model-perf.timer \
    mhde-monitor-data-quality.timer \
    mhde-monitor-smoke.timer
systemctl list-timers --all | grep mhde-monitor
```

To trigger a one-off run (useful for verifying a fix):

```bash
sudo systemctl start mhde-monitor-smoke.service
sudo journalctl -u mhde-monitor-smoke.service -n 30 --no-pager
```

After enabling, watch `data/logs/monitor_*.log` for the first scheduled
firing and confirm the result is OK before relying on the alert
discipline.

### Tuning thresholds

Constants live at the top of each `monitoring/*.py` file:

| Constant | File | Default | Notes |
|---|---|---|---|
| `RECENCY_BUDGET` | `pipeline_execution.py` | 27h equity/crypto, 2h FX | Grace window after schedule before flagging |
| `DEGRADATION_THRESHOLD` | `model_performance.py` | 0.80 | Raise to 0.85 to alert sooner; lower if false-positives are noisy |
| `COVERAGE_FLOOR` | `data_quality.py` | 0.80 | Per-engine ratio cutoff vs 14d avg |

### What an alert looks like

Every alert has the form:

```
[!] MHDE monitor: <name>

<title>

<body — bullet list of specific failures>

Metrics:
  <key>: <value>
  ...
```

Severity prefix: `[i]` info, `[!]` warn, `[!!]` critical.

### Overlap with existing health check

The existing `mhde-health-check.service` (`pipelines/health_check.py`)
runs once daily and covers schema / DB reachability / SEC / freshness
basics. The Session 6 monitors are intentionally orthogonal:

- `health-check` = "is the system running at all"
- monitors = "is the running system producing the right outputs"

There is one overlap area worth being explicit about:
`monitoring/smoke_test.py` checks active joblib loadability, which is
adjacent to `health/ml_checks.py:check_trained_models`. The smoke
check runs hourly while the health check runs daily — keep both. The
hourly cadence on smoke means we catch a missing joblib within an hour
of it disappearing (see KI-009 for an instance where this was already
useful).

### Suppressing alerts (planned outages, retraining)

Set `MONITORING_DRY_RUN=true` in the systemd Environment temporarily,
or `systemctl --user stop mhde-monitor-X.timer`. Re-enable after the
window closes.
