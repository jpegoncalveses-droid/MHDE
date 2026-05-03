# MHDE System Service Deployment

Run these commands from the VPS terminal. Each section is idempotent.

## Part 2 + 3 — Install review server and bridge relay as system services

```bash
# Stop and disable the user-level services (avoid conflict)
systemctl --user stop mhde-review-server mhde-bridge-relay 2>/dev/null || true
systemctl --user disable mhde-review-server mhde-bridge-relay 2>/dev/null || true

# Copy unit files
sudo cp /home/jpcg/MHDE/.claude/local_scripts/systemd_deploy/mhde-review-server.service /etc/systemd/system/
sudo cp /home/jpcg/MHDE/.claude/local_scripts/systemd_deploy/mhde-bridge-relay.service /etc/systemd/system/

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now mhde-review-server mhde-bridge-relay

# Verify
sudo systemctl status mhde-review-server --no-pager
sudo systemctl status mhde-bridge-relay --no-pager
ls -la /tmp/mhde-relay/proxy.sock
source /home/jpcg/MHDE/.env && curl -si -u "$REVIEW_UI_USERNAME:$REVIEW_UI_PASSWORD" http://127.0.0.1:8765/ | head -5
```

## Part 4 — Install daily queue timer

```bash
sudo cp /home/jpcg/MHDE/.claude/local_scripts/systemd_deploy/mhde-daily-catalyst-queue.service /etc/systemd/system/
sudo cp /home/jpcg/MHDE/.claude/local_scripts/systemd_deploy/mhde-daily-catalyst-queue.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now mhde-daily-catalyst-queue.timer

# Confirm timer is scheduled
systemctl list-timers | grep mhde

# Dry-run smoke test (uses cache, no new OpenAI calls):
cd /home/jpcg/MHDE
source .env
venv/bin/python main.py missed daily-catalyst-queue \
    --n 5 --score-min 40 --score-max 44.9 --no-mock \
    --provider openai --model gpt-4o-mini \
    --cache-path data/processed/daily_catalyst_queue_openai_cache_v3.jsonl \
    --history-root data/processed/catalyst_queue_history \
    --html \
    2>&1 | tail -20
```

## Part 5 — Email digest setup

Add these to `/home/jpcg/MHDE/.env`:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=atsrp.notifications@gmail.com
SMTP_PASSWORD=<gmail_app_password>
DAILY_CATALYST_EMAIL_TO=atsrp.notifications@gmail.com
DAILY_CATALYST_REVIEW_URL=https://mhde.duckdns.org
DAILY_QUEUE_SEND_EMAIL=true
```

Note: use `SMTP_USERNAME` (not `SMTP_USER`) — that is what the code reads.

Then test (uses cache):
```bash
cd /home/jpcg/MHDE && source .env
venv/bin/python main.py missed daily-catalyst-queue \
    --n 5 --score-min 40 --score-max 44.9 --no-mock \
    --provider openai --model gpt-4o-mini \
    --cache-path data/processed/daily_catalyst_queue_openai_cache_v3.jsonl \
    --history-root data/processed/catalyst_queue_history \
    --html --send-email \
    2>&1 | tail -20
ls -la data/processed/daily_catalyst_digest.*
```
