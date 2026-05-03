# MHDE Operating Manual

This manual is written for an operator who is new to the MHDE codebase. It covers the daily workflow, service management, pipeline operations, and common maintenance tasks.

---

## Daily Workflow

The normal operating cycle on a trading day:

1. **Open the review server** at `https://mhde.duckdns.org` (HTTP Basic Auth; credentials in `.env` as `REVIEW_UI_USERNAME` / `REVIEW_UI_PASSWORD`).

2. **Check `/today`** — shows a summary of the most recent pipeline run: how many candidates were scored, tier breakdown, and any run warnings.

3. **Check `/candidates`** — the catalyst queue for human review. Each card shows the ticker, thesis, score, and LLM-evaluated catalyst events. Use the review controls to mark items as useful, weak, or false positive.

4. **Check `/moves`** — stocks that moved significantly since the last run. Used to identify missed spikes and validate prior hypotheses.

5. **Check `/learning`** — prediction-vs-actual accuracy metrics. Shows precision, recall, and average forward return broken down by tier and time window. Use this to judge whether the shadow scoring model is improving.

6. **Check `/ops`** — system health: API key presence (not values), last run timestamps, source run outcomes, and any ingestion errors.

7. **Check `/runs`** — historical run index. Click any date entry to see the full pipeline run report for that day.

The automation timer fires at **23:15 UTC on weekdays** (after US market close). On a healthy day, no manual intervention is needed.

---

## Restart Services

The review server and bridge relay are systemd system services (not user services). To restart:

```bash
sudo systemctl restart mhde-review-server
sudo systemctl restart mhde-bridge-relay
```

To check status:

```bash
sudo systemctl status mhde-review-server --no-pager
sudo systemctl status mhde-bridge-relay --no-pager
```

To check the relay socket:

```bash
ls -la /tmp/mhde-relay/proxy.sock
```

---

## Run the Pipeline Manually

To trigger a full daily pipeline run outside the automated timer:

```bash
cd /home/jpcg/MHDE
source .env
venv/bin/python main.py run daily-radar
```

This runs ingestion, feature building, scoring, hypothesis generation, missed spike detection, and forward return population.

---

## Skip Ingestion (Dry Run)

To run the pipeline using whatever data is already in DuckDB (no API calls, no rate limit consumption):

```bash
MHDE_DAILY_SKIP_INGESTION=true .claude/local_scripts/run_mhde_daily_analysis.sh
```

This is useful for re-scoring after a config change without hitting external APIs.

---

## Recover from a Failed Run

1. Find the log file for the failing date:

```bash
ls data/logs/daily_analysis_*.log
```

2. Inspect the log:

```bash
tail -100 data/logs/daily_analysis_YYYY-MM-DD.log
```

3. Identify which stage failed (ingestion, features, scoring, etc.).

4. Re-run only the relevant step. For example, to re-run just the catalyst queue:

```bash
cd /home/jpcg/MHDE && source .env
venv/bin/python main.py missed daily-catalyst-queue \
    --n 10 --no-mock \
    --provider openai --model gpt-4o-mini \
    --cache-path data/processed/daily_catalyst_queue_openai_cache_v3.jsonl \
    --history-root data/processed/catalyst_queue_history \
    --html
```

5. To re-run only forward return population:

```bash
venv/bin/python main.py outcomes populate-forward-returns
```

6. If the full run needs to be retried cleanly, set `MHDE_DAILY_SKIP_INGESTION=true` to avoid redundant API calls if ingestion already completed.

---

## Add a Data Source

1. Create an ingestor adapter in `ingestion/`. Follow the pattern in `ingestion/base_ingestor.py`. The adapter should write records to DuckDB and record a row in `source_runs` with status `ok` or `error`.

2. Wire the new ingestor into the daily pipeline in `pipelines/daily_radar.py` (or the relevant orchestrator).

3. Add a configuration entry in `config/sources.yaml` with a status field (`active`, `experimental`, or `stub`) and a description.

4. If the source requires an API key, read it from the environment (never hardcode it). Document the variable name in `config/sources.yaml` as a comment.

5. Test with a single-ticker smoke run before enabling for the full universe.

---

## Secrets Hygiene

- `.env` is git-ignored. Never commit it.
- All API keys come from environment variables only, loaded via `source .env` or the systemd `EnvironmentFile=` directive.
- The `/ops` route in the review server shows which keys are present (boolean), never their values.
- If you accidentally commit a secret, rotate the key immediately before doing anything else.

Required environment variables:

| Variable | Purpose |
|---|---|
| `POLYGON_API_KEY` | Polygon.io prices and ticker details |
| `ALPHA_VANTAGE_API_KEY` | Fundamentals and earnings |
| `FRED_API_KEY` | Macro series from FRED |
| `REVIEW_UI_USERNAME` | HTTP Basic Auth username for review server |
| `REVIEW_UI_PASSWORD` | HTTP Basic Auth password for review server |

---

## Email Digest

The daily catalyst digest can be sent by email after each automated run. To enable:

1. Add to `.env`:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=<gmail-app-password>
DAILY_CATALYST_EMAIL_TO=recipient@example.com
DAILY_CATALYST_REVIEW_URL=https://mhde.duckdns.org
DAILY_QUEUE_SEND_EMAIL=true
```

Note: use `SMTP_USERNAME`, not `SMTP_USER` — that is what the code reads.

2. The automated timer script picks up `DAILY_QUEUE_SEND_EMAIL=true` and passes `--send-email` to the catalyst queue command automatically.

3. To test the email manually (uses the LLM cache, no new API calls):

```bash
cd /home/jpcg/MHDE && source .env
venv/bin/python main.py missed daily-catalyst-queue \
    --n 5 --no-mock \
    --provider openai --model gpt-4o-mini \
    --cache-path data/processed/daily_catalyst_queue_openai_cache_v3.jsonl \
    --history-root data/processed/catalyst_queue_history \
    --html --send-email
```

---

## Promote a Scoring Signal

This is the full governance workflow for moving an experimental signal from shadow testing into the scoring model.

### Step 1 — Gather evidence

Run the system with the feature flag disabled (default) for at least 20 trading days. Use `/learning` to review prediction-vs-actual metrics.

### Step 2 — Propose the signal

```bash
venv/bin/python main.py learn propose-signal \
    --signal-name earnings_surprise_boost \
    --evidence-period "2026-01-01 to 2026-04-30" \
    --sample-size 180 \
    --precision 0.61 \
    --recall 0.55 \
    --avg-return 0.082 \
    --rollback-criteria "precision < 0.50 over 10 consecutive days"
```

This writes a proposal entry to `data/processed/signal_governance_audit.jsonl`.

### Step 3 — Review and approve

Review the proposal metrics in the audit log, then approve:

```bash
venv/bin/python main.py learn approve-signal --proposal-id <id>
```

### Step 4 — Enable the feature flag

Edit `config/settings.yaml` and set the flag to `true`:

```yaml
feature_flags:
  earnings_surprise_boost: true
```

The flag takes effect on the next pipeline run. The production score will now reflect the new signal.

### Step 5 — Monitor

Watch `/learning` daily for at least 10 trading days after enabling.

### Step 6 — Rollback if performance drops

If precision drops below the rollback threshold:

```bash
venv/bin/python main.py learn rollback-signal --proposal-id <id> --reason "precision fell to 0.47 over 11 days"
```

Then set the flag back to `false` in `config/settings.yaml`.

---

## Common CLI Commands

| Command | Purpose |
|---|---|
| `venv/bin/python main.py health` | System health check |
| `venv/bin/python main.py run daily-radar` | Full pipeline run |
| `venv/bin/python main.py backtest smoke` | Quick backtest smoke test |
| `venv/bin/python main.py missed review-server` | Start review server manually |
| `venv/bin/python main.py outcomes populate-forward-returns` | Populate forward returns |
| `venv/bin/python main.py missed enrich-root-causes` | Run root cause enrichment |

Always use `venv/bin/python` directly. Never `source venv/bin/activate`.
