# MHDE Infrastructure

Single source of truth for deployment topology. Read this before debugging anything that touches services, networking, or scheduled jobs.

---

## VPS

| Field | Value |
|---|---|
| Hostname | `atsrp-vps-01` |
| OS | Ubuntu 24.04.4 LTS (kernel 6.8.0-101-generic) |
| User | `jpcg` |
| Public IPv4 | `178.104.192.164` (eth0) |
| Public IPv6 | `2a01:4f8:1c18:4bf3::1` |
| Tailscale IPv4 | `100.127.108.22` (tailnet `jpegoncalves.es@`) |

Mobile clients on tailnet: `joss-s25-ultra` (Samsung S25 Ultra).

---

## Repo & runtime

| Field | Value |
|---|---|
| Repo root | `/home/jpcg/MHDE` |
| Python venv | `/home/jpcg/MHDE/venv` |
| DuckDB file | `/home/jpcg/MHDE/data/mhde.duckdb` |

**Always** invoke Python via `venv/bin/python` directly. Never `source venv/bin/activate`.

> Note: `data/mhde.db` is a stale empty file (0 bytes, 2026-05-02) created by an early typo. The real DB is `mhde.duckdb`.

---

## Public dashboard

URL: **https://mhde.duckdns.org**

Network path:
```
Mobile/Browser
  → DuckDNS (mhde.duckdns.org → 178.104.192.164)
  → nginx (Docker, container homeboard-nginx-1, image nginx:alpine, host ports 80/443)
  → Unix socket /tmp/mhde-relay/streamlit.sock
  → mhde-streamlit-relay.service (user systemd, jpcg)
  → 127.0.0.1:8501
  → mhde-streamlit.service (user systemd, runs Streamlit on dashboard/app.py)
```

The `https://mhde.duckdns.org/review/` subpath was retired 2026-05-07
(Session 0). The Flask review server (`review/server.py`),
`mhde-review-server.service`, and `mhde-bridge-relay.service` all moved
to `legacy/`; the nginx route was removed. Requests to `/review/` now
fall through to Streamlit and currently return 502 — a clean 404 is a
follow-up.

TLS: Let's Encrypt via certbot. Certs at `/home/jpcg/homeboard/certbot/conf/live/mhde.duckdns.org/`.

nginx is part of the **homeboard** docker-compose project at `/home/jpcg/homeboard/`.
nginx config: `/home/jpcg/homeboard/nginx/nginx.conf` (mounted read-only into the container).

Dashboard auth: `MHDE_DASHBOARD_PASSWORD_HASH` (sha256) and `MHDE_DASHBOARD_AUTH_ENABLED=true` are baked into `mhde-streamlit.service` `Environment=` directives — not in `.env`.

---

## systemd services

### System-level (`/etc/systemd/system/`, run by root)

| Unit | Schedule (UTC) | Purpose |
|---|---|---|
| `mhde-predict.service` + `.timer` | daily 21:00 | Equity ML scoring (only — relies on `mhde-daily-analysis.service` for prior price/feature refresh) |
| `mhde-retrain.service` + `.timer` | weekly Sun 21:30 | Equity ML weekly retrain |
| `mhde-crypto-predict.service` + `.timer` | daily 00:30 | Crypto ML — chained: backfill-prices → backfill-funding → backfill-oi → backfill-labels → backfill-features → predict |
| `mhde-crypto-retrain.service` + `.timer` | weekly Sun 23:00 | Crypto ML weekly retrain |
| `mhde-fx-predict.service` + `.timer` | hourly :05 | FX ML — chained: refresh-prices → backfill-features → backfill-labels → predict |
| `mhde-fx-retrain.service` + `.timer` | weekly Sat 22:00 | FX ML weekly retrain |
| `mhde-fx-bot.service` | always-on (Restart=always) | FX Telegram bot (long-polling) |

### User-level (`~/.config/systemd/user/`, run by `jpcg`)

| Unit | Type | Purpose |
|---|---|---|
| `mhde-streamlit.service` | always-on | Streamlit dashboard on `127.0.0.1:8501` |
| `mhde-streamlit-relay.service` | always-on | Unix-socket forwarder → 8501 |
| `mhde-health-check.service` + `.timer` | enabled | Periodic system health check (`main.py system health-check`) |
| `mhde-daily-analysis.service` + `.timer` | enabled, Mon-Fri 23:15 | Equity daily ingest + features + radar (the equity ingest path — predict at 21:00 reads what this writes) |
| `mhde-daily-catalyst-queue.service` + `.timer` | disabled (kept dormant) | Standalone catalyst queue runner — the `daily-analysis` script invokes the same CLI inline. |
| `mhde-review-server.service`, `mhde-bridge-relay.service` | **disabled** 2026-05-07 (Session 0) | Flask catalyst-review UI + its unix-socket forwarder. Code moved to `legacy/review/`. nginx `/review/` route removed. Do not re-enable. |
| `mhde-predict.service` + `.timer`, `mhde-retrain.service` + `.timer` | **disabled** as of 2026-05-06 | Legacy duplicates of system-level units. Both used to fire at 21:00 alongside the system timers, causing duplicate predict runs and DuckDB write contention. Files are kept on disk but `disable --now`'d — do **not** re-enable. |

### Restart cheat sheet
```bash
# User services
systemctl --user restart mhde-streamlit
systemctl --user restart mhde-streamlit-relay

# System services
sudo systemctl restart mhde-fx-predict
sudo systemctl restart mhde-fx-bot
sudo systemctl restart mhde-predict        # equity daily
sudo systemctl restart mhde-crypto-predict

# nginx (docker compose)
cd /home/jpcg/homeboard && docker compose restart nginx
```

### Reading logs
- System services: `journalctl -u <unit> --since "1 hour ago"` (needs `sudo` or membership in `systemd-journal`/`adm` group)
- User services: `journalctl --user -u <unit> --since "1 hour ago"`
- File logs: `/home/jpcg/MHDE/data/logs/`

---

## Logs

All in `/home/jpcg/MHDE/data/logs/`. Files written by system-level services are owned by `root`.

| File | Source |
|---|---|
| `equity_predict.log` | `mhde-predict.service` (equity) |
| `ml_predict.log` | Equity ML legacy alias |
| `crypto_predict.log` | `mhde-crypto-predict.service` |
| `fx_predict.log` | `mhde-fx-predict.service` |
| `fx_bot.log` | `mhde-fx-bot.service` |
| `review_server.log` | Catalyst review UI — service retired 2026-05-07; old log retained for reference. |
| `server.log` | Streamlit (legacy/manual runs) |
| `daily_radar_manual_*.log` | Manual daily-radar runs |

---

## Secrets

### `/home/jpcg/MHDE/.env` (mode 600, never committed)

| Variable | Used by |
|---|---|
| `ALPHA_VANTAGE_API_KEY` | Equity prices |
| `POLYGON_API_KEY` | Equity prices |
| `FRED_API_KEY` | FX/macro (BoE rate, ECB rate, EUR/USD, GBP/USD) |
| `TWELVEDATA_API_KEY` | FX hourly bars (production fetcher post-2026-05-08 cutover). Free tier 800 calls/day; we use 24. See `OPERATIONS.md` "TwelveData (FX)". |
| `NVIDIA_API_KEY`, `MHDE_LLM_PROVIDER`, `MHDE_NVIDIA_MODEL` | LLM analysis |
| `OPENAI_API_KEY` | LLM analysis (fallback) |
| `REVIEW_UI_USERNAME`, `REVIEW_UI_PASSWORD` | Review server basic auth |

### Telegram credentials — **NOT in MHDE/.env**

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are loaded from **`/home/jpcg/ATSRP/.env`** by `notifications/telegram.py`. If absent, Telegram alerts and the FX bot fail with a clear error. Do not duplicate them into MHDE/.env without removing the ATSRP-loader fallback.

### Dashboard auth

`MHDE_DASHBOARD_PASSWORD_HASH`, `MHDE_DASHBOARD_AUTH_ENABLED`, `MHDE_DB_PATH` are set as `Environment=` lines inside `~/.config/systemd/user/mhde-streamlit.service`. Update there, then `systemctl --user daemon-reload && systemctl --user restart mhde-streamlit`.

---

## Data sources & refresh

| Engine | Source | Module |
|---|---|---|
| Equity | Yahoo Finance | `ingestion/ingest_yahoo_historical.py` |
| Equity | Alpha Vantage | `ingestion/ingest_prices.py` |
| Equity | Polygon | `ingestion/ingest_prices.py` |
| Equity | Stooq | `ingestion/ingest_stooq*.py` |
| Equity | SEC, FDA, CFTC, FINRA, GDELT, Earnings, StockTwits, Sector ETFs | `ingestion/ingest_*.py` |
| Crypto | Binance (REST/historical) | `crypto/ingestion/` |
| FX | TwelveData REST (hourly bars) | `fx/data/refresh.py` → `fx/data/refresh_twelvedata.py` (post-2026-05-08 cutover; ADR-013) |
| FX | FRED macro (BoE/ECB/EUR-USD/GBP-USD) | `fx/data/macro.py` |

---

## Three prediction engines

### 1. Equity — `pipelines/ml_prediction_pipeline.py`
- Schedule: **daily 21:00 UTC** (`mhde-predict.timer`)
- Input: `prices_daily` (multi-source) and feature tables
- Output: `ml_predictions`
- Manual run: `venv/bin/python main.py ml predict`
- Health: `tail data/logs/equity_predict.log`

### 2. Crypto — `pipelines/crypto_prediction_pipeline.py`
- Schedule: **daily 00:30 UTC** (`mhde-crypto-predict.timer`)
- Service runs 6 chained steps: `crypto backfill-prices` → `backfill-funding` → `backfill-oi` → `backfill-labels` → `backfill-features` → `predict`
- Output: `crypto_ml_predictions`
- Manual run (full chain): each command above in order; or just `venv/bin/python main.py crypto predict` if data is already fresh
- Health: `tail data/logs/crypto_predict.log`

### 3. FX — `pipelines/fx_prediction_pipeline.py`
- Schedule: **hourly at :05 UTC** (`mhde-fx-predict.timer`)
- Tables: `fx_prices_hourly` (input), `fx_ml_predictions`, `fx_signals`, `fx_position`
- Telegram: position-aware alert suppression in `fx/bot/telegram_bot.py:send_signal_alert`
- Manual run: `venv/bin/python main.py fx predict`
- Health: `tail data/logs/fx_predict.log`

Freshness guard: `pipelines/freshness.py` is called at the top of every prediction pipeline. If input prices are stale beyond the engine threshold (FX = 2h, equity/crypto = 1 day), the predictor logs `DATA STALE` and skips. Check both `fx_predict.log` and the journal when predictions appear missing.

---

## Reverse-proxy details

- nginx Docker container: `homeboard-nginx-1` (image `nginx:alpine`, ports `80→80`, `443→443`)
- Compose project root: `/home/jpcg/homeboard/`
- nginx config (read-only mount): `/home/jpcg/homeboard/nginx/nginx.conf`
- Routes served by this nginx:
  - `casa110.duckdns.org` → static homeboard PWA at `/usr/share/nginx/html` (mounted from `/home/jpcg/homeboard/frontend/dist`)
  - `mhde.duckdns.org/` → `unix:/tmp/mhde-relay/streamlit.sock` (Streamlit)
  - `mhde.duckdns.org/_stcore/stream` → same socket with WebSocket upgrade headers

Caddy is **not** the active reverse proxy. The `/etc/caddy/Caddyfile` is a stale historical artifact — ignore it.

---

## Gotchas / known footguns

- **User-level systemd units must NOT declare `User=` or `Group=`.** They already run as the user; declaring these causes exit code 216/GROUP "Failed to determine supplementary groups: Operation not permitted" on every firing — silently, with no entry in the journal beyond the startup failure. Two units historically tripped on this (`mhde-daily-analysis.service`, `mhde-daily-catalyst-queue.service`) — both fixed 2026-05-06. If you `cp` a unit between `/etc/systemd/system/` and `~/.config/systemd/user/`, strip those lines.
- **System-level vs user-level duplicates can fire at the same instant.** The legacy user-level `mhde-predict.timer`/`mhde-retrain.timer` were disabled 2026-05-06 because both were firing in lockstep with the system-level versions, causing duplicate predict runs and DuckDB write contention.
- **DuckDB allows only one process to hold the write lock at a time.** A long-running `daily-radar` blocks the dashboard's read-only connection too. If a hourly FX timer fires while a daily-radar is running, the FX run will fail to acquire the lock; it'll retry on the next firing.
- **Streamlit doesn't auto-poll.** A page left open does not refresh on its own — the FX tab has a `↻ Refresh` button + `Data as of bar` caption to make staleness obvious.
- **PWAs cache aggressively on mobile.** After restarting Streamlit, force-close the PWA from recent apps and reopen; if still stale, clear site data in Chrome settings.

---

## Quick health audit

```bash
# 1. All MHDE services up?
systemctl --user list-units --type=service --state=running | grep mhde
sudo systemctl list-units --type=service --state=running | grep mhde

# 2. Latest data in DB
venv/bin/python .claude/local_scripts/check_predictions_freshness.py

# 3. Public dashboard reachable?
curl -sI https://mhde.duckdns.org/ | head -3

# 4. Disk + DB size
df -h /home/jpcg
du -sh /home/jpcg/MHDE/data/mhde.duckdb
```
