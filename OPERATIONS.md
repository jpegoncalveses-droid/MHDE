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

#### Retrain validation gate

Every `crypto retrain` run inserts the new model with
`is_active=false, promotion_status='pending'`, then
`crypto/ml/validation_gate.py::validate_promotion` runs against the
previous active row for the same horizon.

- **Pass:** old model demoted (`is_active=false`); new model promoted
  (`is_active=true, promotion_status='promoted'`).
- **Fail:** new model stays `is_active=false,
  promotion_status='promotion_blocked'`; old model stays active; a
  critical-severity Telegram alert fires.

Either way one structured JSON line is emitted to stdout
(`event="retrain_validation"`) with the comparison metrics and
`duration_sec`.

**Inspecting blocked promotions.**

```sql
-- All blocked promotions ever
SELECT model_id, horizon, created_at, promotion_status,
       precision_at_threshold, auc_roc
FROM crypto_ml_model_runs
WHERE promotion_status = 'promotion_blocked'
ORDER BY created_at DESC;

-- Latest active + blocked per horizon (quick sanity view)
SELECT horizon, model_id, is_active, promotion_status,
       precision_at_threshold, created_at
FROM crypto_ml_model_runs
WHERE horizon IN ('5d', '10d')
ORDER BY horizon, created_at DESC
LIMIT 10;
```

**Manual override (force-promote a blocked model after operator review).**

Use this only after confirming the blocked model is genuinely good
(e.g., the gate fired on a borderline hit-rate dip that you've
reviewed and accepted).

```bash
# 1. Inspect the blocked model and compare metrics
#    (write a quick script rather than an inline python -c per CLAUDE.md)
venv/bin/python .claude/local_scripts/inspect_blocked_promotion.py <MODEL_ID>

# 2. Demote the currently-active row for the horizon
#    (adjust horizon as needed: 5d or 10d)
venv/bin/python main.py crypto db-exec \
  "UPDATE crypto_ml_model_runs SET is_active = false WHERE horizon = '10d' AND is_active = true"

# 3. Promote the blocked row
venv/bin/python main.py crypto db-exec \
  "UPDATE crypto_ml_model_runs SET is_active = true, promotion_status = 'promoted' WHERE model_id = '<MODEL_ID>'"
```

If `crypto db-exec` is not available, open a Python script in
`.claude/local_scripts/` using `duckdb.connect('data/mhde.duckdb')`
and run the two UPDATEs there — never use inline `python -c` blocks
per CLAUDE.md.

**Telegram alert.** Critical-severity. Format:
`[!!] Promotion blocked for <model_id>` followed by a JSON-formatted
comparison dict (old hit rate, new hit rate, threshold). Acknowledge
by reviewing the comparison and either re-training (preferred) or
manually promoting (escape valve above). See ADR-019 for the full
design rationale and escape valve guidance.

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
| `dashboard/*` | `systemctl --user restart mhde-streamlit` (mandatory — see below) |
| `fx/bot/*` | `sudo systemctl restart mhde-fx-bot` |
| `pipelines/freshness.py`, `storage/db.py` | (no restart) — picked up next firing |
| `pipelines/{ml,crypto,fx}_prediction_pipeline.py` | (no restart) — picked up next firing |
| `ml/*`, `crypto/*`, `fx/*` (non-bot) | (no restart) — picked up next firing |
| `systemd/*.service` or `.timer` | Copy to `/etc/systemd/system/` (sudo) or `~/.config/systemd/user/`, then `daemon-reload` and `restart`. |
| `.env` | Restart any service that reads it (not the systemd unit env vars). |

#### Streamlit does NOT auto-reload in this deployment

`mhde-streamlit.service` runs Streamlit without `--server.runOnSave`
(omitted to keep the dashboard from reloading mid-render). That means
**any change under `dashboard/`** — query helpers, format functions,
the page itself — sits on disk unloaded until the process restarts.
A restart is mandatory after every dashboard code change:

```bash
systemctl --user restart mhde-streamlit
# Verify the new PID has a recent ActiveEnterTimestamp
systemctl --user show mhde-streamlit -p ActiveEnterTimestamp --value
```

The `monitoring/streamlit_freshness` monitor (added 2026-05-09) flags
when the running process predates the latest commit on master by more
than 4 hours so this gap is caught even if the operator forgets the
restart. See the trust ladder below: a code commit is L0, not L5 —
"the user-visible artifact matches" only holds after the restart.

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

## Cross-scope systemd traps

Three layers of systemd are involved in this deployment and they
don't all talk to each other. The traps below cost real time on
2026-05-09; each is documented so the next session doesn't pay
the cost again.

### `systemctl --user` from a system-level service fails without D-Bus

A system-level systemd unit (under `/etc/systemd/system/` with
`User=jpcg`) does NOT inherit a per-user D-Bus session. Calling
`systemctl --user show <some-user-service> -p ActiveEnterTimestamp`
from inside that unit's `ExecStart` fails with:

```
Failed to connect to bus: No medium found
```

This bit `monitoring/streamlit_freshness` on its first deploy:
the monitor is a system-level service that needs to know when the
user-level `mhde-streamlit.service` started. The fix was to read
`/proc/<PID>/stat` field 22 directly via a `pgrep -f 'streamlit
run dashboard'` lookup — works from system or user context with
no D-Bus dependency.

**Rule of thumb.** If a system-level service needs to inspect
state from a user-level service, use kernel-level interfaces
(`/proc`, file mtimes, sockets) rather than `systemctl --user`.
Crossing the boundary the other way (user-level invoking
`systemctl` on system services) is fine: a user shell normally
inherits enough environment to reach the system manager.

To enable lingering and get a persistent user manager — which
WOULD let `systemctl --user` work from system-level services —
run `loginctl enable-linger jpcg`. We deliberately don't, because
it changes the security model for every other user-level service,
and the `/proc` workaround is cleaner for the one monitor that
needs cross-scope visibility.

### `User=jpcg` in a user-level unit silently breaks the unit

Documented separately in `INFRASTRUCTURE.md` and enforced by
`scripts/pre-commit.sh`. A user-level unit (under
`~/.config/systemd/user/`) that contains `User=` or `Group=`
loads but produces no useful output and no error. The hook warns
on staged user-level units that contain those lines.

### A unit fragment in `/etc/systemd/system/` doesn't restart on edit

The deploy step is `daemon-reload` followed by `restart`. Skipping
the reload leaves systemd serving the old fragment from its
in-memory cache. The repo's `Deploy procedures` section above
spells out the right sequence; this trap is here so it's findable
when an operator notices "I edited the unit but it's still using
the old timer schedule".

---

## Trust ladder

A change isn't "fixed" because the code is committed. It's fixed when
the user-visible artifact reflects the change. We codify that here as
six levels; ADR-016 is the long-form rationale.

| Level | Predicate | How you verify |
|---|---|---|
| **L0** | Code committed | `git log` shows the commit on the working branch |
| **L1** | Tests pass | `make test` green; pre-commit hook OK |
| **L2** | Database state correct | A direct SQL probe against the production DB returns the expected rows / values |
| **L3** | Service / pipeline produces expected output | `journalctl -u mhde-…` shows the unit ran cleanly and wrote what L2 confirms |
| **L4** | Dashboard renders correctly | A request to `http://127.0.0.1:8501` returns 200 AND the rendered page runs the latest code (process start ≥ commit time — see `monitoring/streamlit_freshness`) |
| **L5** | User-visible artifact matches expectation | The CSV the user downloads / the Telegram message they receive / the report they read agrees with L2's truth |

L5 verification is part of the universal exit criteria (see
`HARDENING_PLAN.md`). For dashboard changes specifically, L5 means a
fresh CSV pulled by the user — not a fresh CSV pulled by Claude
Code from the same query helpers. The latter passes through the
running Streamlit process; if Streamlit is stale, the artifact lies
even though every other layer is correct. The 2026-05-09 equity
maturity-date fix demonstrated this gap: L0-L4 all green for hours
before someone noticed the user's CSV still showed blanks.

The four monitors in `monitoring/{dashboard_consistency,
streamlit_freshness, dashboard_synthetic, cross_artifact}` between
them aim to convert "L5 silently fails" into a Telegram alert within
an hour.

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
strict rate limits (~5 calls/min) but the ingestor has been tuned to
fit inside that budget — see below.

**Architecture (post-2026-05-09 fix, see KI-120 in archive).** Primary
path uses Polygon's **grouped daily** endpoint:

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=true&apiKey=...
```

One HTTP call per date returns OHLCV for ~12,000 US tickers (~1s
observed). The ingestor filters the result to our universe and
inserts into `prices_daily` with `ON CONFLICT DO NOTHING` (idempotent).
Per-ticker fallback (the older
`/v2/aggs/ticker/{ticker}/range/1/day/...` endpoint) runs for the
rare universe ticker missing from the grouped feed, capped per date
to keep API budget bounded (`DEFAULT_FALLBACK_LIMIT=10`). Anything
beyond the cap falls through to the orchestrator's downstream
Stooq / Yahoo stages.

**Call budget per nightly run.** `DEFAULT_LOOKBACK_DAYS=7` ⇒ 7
grouped calls plus up to ~10 fallback calls per date with missing
universe tickers. Worst case ≤ ~80 calls; typical ~10. With 13s
throttle between calls (`DEFAULT_THROTTLE_S=13`) and an automatic
65s retry after 429, the orchestrator's polygon stage runs in
1-2 minutes total.

**Pre-fix history.** Before 2026-05-09 the ingestor looped per-ticker
against the single-ticker aggregates endpoint, which on ~520 universe
tickers exceeded the 5/min limit by orders of magnitude and caused
multi-day ingestion thinning (KI-120). Per-ticker is now the bounded
fallback, not the primary path.

Check call counts in `source_runs`:

```sql
SELECT use_case, status, records_attempted, records_inserted, error_message
FROM source_runs
WHERE source_name = 'polygon'
ORDER BY started_at DESC LIMIT 10;
```

**Backfill recipe** (when prices_daily is thin for specific dates):
```
venv/bin/python .claude/local_scripts/equity_backfill_prices.py
```
Edit the `TARGET_DATES` list at the top of the script. Idempotent.

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

### TwelveData (FX) — production fetcher post-cutover

`fx/data/refresh.py` is the production GBP/EUR 1h bar fetcher; it
delegates to the TwelveData implementation in
`fx/data/refresh_twelvedata.py`. Cutover landed 2026-05-08
(DECISIONS.md ADR-013). Writes to `fx_prices_hourly`, the table all
FX readers consume (predict / features / labels / freshness / dashboard).
The pre-cutover Dukascopy/ATSRP-subprocess path is gone.

Setup:
1. Get a free key at https://twelvedata.com (800 calls/day; we use 24).
2. Add `TWELVEDATA_API_KEY=...` to `/home/jpcg/MHDE/.env` (mode 600, never committed).
3. Confirm `mhde-fx-predict.service` is the post-cutover unit: one
   `fx refresh-prices` ExecStart, no parallel `…-twelvedata` line.
   `journalctl -u mhde-fx-predict | grep TwelveData` after the next firing
   should show `Inserted TwelveData bar into fx_prices_hourly for …`.

Manual run (one-off):
```bash
venv/bin/python main.py fx refresh-prices
```

Cleanup pending (~2026-05-15, after 1-week stability buffer):
- Drop `fx_prices_hourly_twelvedata` and
  `fx_prices_hourly_twelvedata_backfill` tables.
- Remove `fx refresh-prices-twelvedata` and `fx compare-sources` CLI
  subcommands from `main.py`.
- Delete `fx/data/compare_sources.py` and `tests/fx/test_compare_sources.py`.
- Drop `SCHEMA_FX_PRICES_HOURLY_TWELVEDATA` from `fx/schema.py`.

Rollback path during the buffer: `git revert` the cutover commit. The
240 historical bars filled at cutover stay in `fx_prices_hourly`;
subsequent firings would reattach to the old Dukascopy/ATSRP code path.
ATSRP is still on disk for this purpose (also still serves Telegram
credentials per ADR-003).

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

## Monitors (Session 6 + extensions)

Eleven runtime monitors fire Telegram alerts on detected anomalies. Source
under `monitoring/`, dispatched via `main.py monitor <name>`. Schedules
are systemd-driven from `systemd/mhde-monitor-*.{service,timer}`.

### Monitor catalog

| Monitor | Schedule (UTC) | Module | What it checks |
|---|---|---|---|
| `dashboard-consistency` | every 6h at :30 | `monitoring/dashboard_consistency.py` | Dashboard query layer matches direct DB query (counts, latest-date rows, per-engine × per-horizon column completeness). |
| `pipeline-execution` | hourly at :40 | `monitoring/pipeline_execution.py` | Each engine has a fresh-enough latest write + row count ≥ 50% of 14-day rolling avg, both filtered to `is_active=true` model_ids. |
| `config-drift` | daily 12:15 | `monitoring/config_drift.py` | Repo `systemd/*` ↔ deployed copies under `/etc/systemd/system` and `~/.config/systemd/user`. |
| `model-performance` | daily 13:15 | `monitoring/model_performance.py` | Rolling 7d precision per active model ≥ 0.8× walk-forward baseline. |
| `data-quality` | daily 02:00 | `monitoring/data_quality.py` | Per engine: distinct ticker / symbol count on latest day ≥ 80% of 14-day avg. |
| `smoke` | hourly at :50 | `monitoring/smoke_test.py` | DB opens, every active joblib loads, `dashboard.services.queries.get_overview_stats` returns. |
| `streamlit-freshness` | hourly at :35 | `monitoring/streamlit_freshness.py` | Running Streamlit process start time vs latest commit on master; warns if process predates commit by > 4h. |
| `dashboard-synthetic` | hourly at :40 | `monitoring/dashboard_synthetic.py` | HTTP liveness on `/_stcore/health` + each `get_*_predictions` helper returns non-empty without raising. |
| `cross-artifact` | daily 06:30 | `monitoring/cross_artifact.py` | Daily Telegram health-check formatter strings agree with direct DB queries. |
| `phase0-calibration` | weekly Sun 06:00 | `monitoring/phase0_calibration.py` | Phase 0 interim drift (lift, precision-ratio, calibration buckets), sample-rate slowdown, and one-shot 200-sample-reached notification. See `docs/PATH_TO_LIVE_PLAN.md` § Phase 0. |
| `paper-trading-drift` | every 15 min | `monitoring/paper_trading_drift.py` | Reads the engine DuckDB read-only (`CRYPTO_ENGINE_DB_PATH`): (A) engine liveness — monitor-phase fresh, entry-phase ran today; (B) no position stuck `*_pending` > 10 min; (C) closed-trade win rate 14d vs `[0.74, 0.99]`; (D) label hit rate (`crypto_ml_labels.label_10d_10pct`) 14d vs `[0.32, 0.62]`. C/D sample-gated at 20 trades. P&L-band/DD/monthly arms deferred (KI-136). See ADR-020. |

Schedules are staggered to avoid the FX hourly :05 firing window and the
nightly daily-analysis 23:15 lock window.

### Manual invocation

```bash
# Always invoke via the monitor CLI group
venv/bin/python main.py monitor smoke

# Dry-run mode — compute the alert payload but never call Telegram
MONITORING_DRY_RUN=true venv/bin/python main.py monitor smoke

# All eleven in sequence (useful for end-to-end exercising on a fresh deploy)
for m in dashboard-consistency pipeline-execution config-drift \
         model-performance data-quality smoke streamlit-freshness \
         dashboard-synthetic cross-artifact phase0-calibration \
         paper-trading-drift; do
    MONITORING_DRY_RUN=true venv/bin/python main.py monitor "$m"
done
```

### Phase 0 calibration evaluation runbook

The formal Phase 0 go/no-go is the markdown report:

```bash
# Default: every is_active=true crypto model, save to data/reports/
venv/bin/python main.py crypto phase0-report

# One model only
venv/bin/python main.py crypto phase0-report --model-id crypto_5d_ab428f75

# Custom save path
venv/bin/python main.py crypto phase0-report --out /tmp/phase0.md

# Stdout only — no file save
venv/bin/python main.py crypto phase0-report --out -
```

Verdict per model: `PASS` / `FAIL` / `INTERIM` (the latter when sample
size < 200; all four metrics are still computed and shown so the
operator can see trajectory). The four criteria, per
`docs/PATH_TO_LIVE_PLAN.md` § Phase 0:

1. Top-N hit rate within ±25% of walk-forward `precision_at_threshold`.
2. Lift ≥ 1.3× over `base_rate` over rolling 30 days.
3. No run of ≥ 3 consecutive same-direction reliability buckets > 10pp
   off the bucket midpoint (definition (a) absolute; relative
   week-over-week drift is KI-126).
4. ≥ 200 filled outcomes.

The weekly `phase0-calibration` monitor uses tighter "yellow flag"
thresholds (lift < 1.5×, precision-ratio < 0.85) than the formal hard
gates so drift surfaces before week 6.

The one-shot "200-sample reached" notification fires once per model
via `phase0_milestones` (engine, model_id, milestone="200_reached").
Resetting that row will re-fire the notification — useful when
intentionally re-running Phase 0 after a model retrain.

Exit code 0 = ok, 1 = warn or fail. The systemd unit interprets non-zero
as "service failed" — check `journalctl --user -u mhde-monitor-X.service`
for the alert payload that was logged.

### Paper-trading drift monitor runbook

Reads the crypto-trading-engine's DuckDB **read-only** (path from
`CRYPTO_ENGINE_DB_PATH`, defaulting to
`/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`) plus
MHDE's `crypto_ml_labels`. See ADR-020 for why monitoring is allowed to
read the engine DB directly.

Manual run / dry-run:
```bash
MONITORING_DRY_RUN=true venv/bin/python main.py monitor paper-trading-drift 2>&1
# point at a different engine DB:
CRYPTO_ENGINE_DB_PATH=/path/to/trading_engine.duckdb \
  MONITORING_DRY_RUN=true venv/bin/python main.py monitor paper-trading-drift
```

Interpreting an alert (the Telegram body lists every check, OK lines
included):

| Finding | Meaning | First action |
|---|---|---|
| `engine: last 'monitor' cycle N min ago` (warn ≥ 5, crit ≥ 20) | The engine's per-minute monitor phase has stalled. | `systemctl --user status trading-engine-monitor.timer` on the VPS; check the engine's logs under `/home/jpcg/crypto-trading-engine/data/logs/`. |
| `engine: no successful 'entry' run today` (warn, only after 08:30 UTC) | Today's daily entry phase didn't fire (or failed). | Check `trading-engine-entry.timer` / engine logs; a missed entry day means no new positions today. |
| `stuck positions: SYM in entry_pending/exit_pending for N min` (warn ≥ 10, crit ≥ 30) | A position's limit order is resting unfilled or an exit hasn't completed. | Look up the symbol on Binance demo; the engine's RECONCILE-001 logic auto-resolves stale `entry_pending` at 24h, but a 10-min flag is an early warning — inspect manually. |
| `closed-trade win rate X% (… outside walkfold band)` (warn outside `[0.74, 0.99]`, crit < 0.60) | Realised post-cost trade win rate over the last 14 days has drifted from the Phase 1B expectation (~87% median). | Pull the closed trades from the engine DB, eyeball the losers' exit reasons; if the trailing-stop logic is misbehaving this is where it shows. Suppressed until 20 closed trades accumulate. |
| `label hit rate X% (… outside walkfold band)` (warn outside `[0.32, 0.62]`, crit outside `[0.20, 0.75]`) | The model's top-K picks are clearing +10%/10d at a rate far from the ~42.5% walkfold median — i.e. the *signal* (not the execution) has drifted. | Cross-check against `monitor phase0-calibration`; a real label-hit drop should also show there. Suppressed until 20 settled-label positions accumulate. |
| `closed-trade win rate: insufficient sample (N/20)` / `label hit rate: insufficient sample (N/20 …)` | Not enough data yet — informational, not an alert. | None. Expected for the first ~3–4 trading days of paper trading. |

NOTE: the P&L-band, drawdown-breach, and monthly-return arms are **not
implemented yet** (KI-136 / "Gap 2.5") — they're blocked on the
engine's `daily_pnl` table filling, which is blocked on engine-side
RECONCILE-001. Don't expect P&L drift coverage from this monitor until
then.

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
    mhde-monitor-smoke.timer \
    mhde-monitor-paper-trading-drift.timer
systemctl list-timers --all | grep mhde-monitor
```

(`mhde-monitor-paper-trading-drift` needs the engine DuckDB readable —
its `.service` sets `Environment=CRYPTO_ENGINE_DB_PATH=…`; adjust if
the engine repo lives elsewhere.)

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


## Engine exports — `data/exports/active_spec.json` + daily predictions

**Contract:** `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md`.
**Producer module:** `crypto/exports/`.
**Decision record:** ADR-017 in `DECISIONS.md`.

### When to run `crypto export-spec` (rare)

After every Phase 1B re-run that changes the winner config:

1. Run the new sensitivity grid; identify the new winner row.
2. Edit `crypto/exports/spec_config.py:PHASE1B_WINNER_RUN_ID` to the
   new `run_id`.
3. Commit (`feat(exports): Phase 1B winner update — <reason>`).
4. Run `venv/bin/python main.py crypto export-spec` to regenerate
   `data/exports/active_spec.json`.
5. Engine picks up the change on its next entry phase (hash mismatch
   triggers reload + Telegram alert + `spec_history` insert).

### Daily predictions timer

`mhde-crypto-export-predictions.timer` fires at 06:15 UTC daily,
7 days/week. The service runs `venv/bin/python main.py crypto
export-predictions`, which:

1. Resolves the active 10d model from `crypto_ml_model_runs`.
2. **Preflight (staleness-only, KI-129 corrected)**: requires
   `MAX(trade_date) FROM crypto_ml_features == today UTC`.
3. Re-scores all active universe symbols that have features for
   today, ranks 1..N, writes
   `data/exports/predictions_YYYY-MM-DD.json` (atomic) and replaces
   `predictions_latest.json` symlink.

If preflight fails (stale features), the script exits non-zero and
writes nothing. The engine's own validator sees `predictions_latest.
json` pointing at yesterday's file (`export_date != today`), alerts
via Telegram, and skips the entry phase per INTERFACE.md §5.3.

**Note on `n_predictions`.** The output reflects whatever active
universe symbols are predictable on the export date. Newly-added
universe entries that are still in the 60-day features warmup window
are silently absent. `n_predictions` will rise as warmup symbols age
in. INTERFACE.md §3 does not mandate `n_predictions == universe_size`.

### First-time deployment on the VPS

```bash
sudo cp /home/jpcg/MHDE/systemd/mhde-crypto-export-predictions.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mhde-crypto-export-predictions.timer
systemctl status mhde-crypto-export-predictions.timer
```

After enabling: tail the service log on the next firing to confirm
output:

```bash
journalctl -u mhde-crypto-export-predictions -f
```

### Recovery — `predictions_latest.json` missing or stale

If the engine pages: `[ENGINE] Predictions stale or missing`.

1. Check `journalctl -u mhde-crypto-export-predictions -n 50` for the
   most recent failure message.
2. Common cause: `mhde-crypto-predict.service` failed earlier in the
   day, so `crypto_ml_features` is missing today's row. Run
   `journalctl -u mhde-crypto-predict -n 50` to confirm.
3. Fix the upstream issue (e.g., re-run `crypto backfill-prices` /
   `crypto backfill-features`).
4. Once features for today exist, re-run manually:
   `venv/bin/python main.py crypto export-predictions`.
5. Verify with: `cat data/exports/predictions_latest.json | head -10`.

### Recovery — `active_spec.json` missing

This file is rarely regenerated. If missing:

1. Confirm `data/exports/` exists. If not: `mkdir -p data/exports`.
2. Run `venv/bin/python main.py crypto export-spec`.
3. Verify the file is valid by running a small script under
   `.claude/local_scripts/` that loads the JSON and re-computes the
   hash via `crypto.exports.hashing.compute_spec_hash`. Project
   rules forbid inline `python -c` invocations.

### What NOT to do

- Don't `git add data/exports/`. The directory is gitignored;
  commits would be daily noise.
- Don't edit `data/exports/active_spec.json` directly. The
  `spec_hash` field protects against tampering — engine validation
  will fail. Always regenerate via `crypto export-spec`.
- Don't change `crypto/exports/hashing.py` without coordinating an
  engine-repo commit. The cross-repo parity test in
  `tests/crypto/exports/test_hashing.py` will catch any drift the
  next time both repos are present in the same environment.
