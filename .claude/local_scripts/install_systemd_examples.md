# Installing MHDE systemd Services

## Prerequisites

- VPS running Linux with systemd
- Python virtualenv at `/path/to/MHDE/venv`
- Caddy installed (for HTTPS reverse proxy)
- DuckDNS subdomain configured (see DNS setup below)

## DNS Setup

Point `mhde.duckdns.org` to the VPS public IP `95.62.230.192` in the DuckDNS dashboard.
This is a public IP — do not include the DuckDNS token in any config file.

## Install the Review Server Service

```bash
# Edit paths and user in the example file first
cp .claude/local_scripts/review_server.service.example /etc/systemd/system/mhde-review-server.service
nano /etc/systemd/system/mhde-review-server.service

systemctl daemon-reload
systemctl enable mhde-review-server
systemctl start mhde-review-server
journalctl -u mhde-review-server -f
```

Required environment variables (set in `.env` or the service `Environment=` block):
- `REVIEW_UI_USERNAME` — HTTP Basic Auth username
- `REVIEW_UI_PASSWORD` — HTTP Basic Auth password (never log or echo this)

## Install the Daily Queue Timer

```bash
cp .claude/local_scripts/daily_catalyst_queue.service.example /etc/systemd/system/mhde-daily-catalyst.service
cp .claude/local_scripts/daily_catalyst_queue.timer.example   /etc/systemd/system/mhde-daily-catalyst.timer
nano /etc/systemd/system/mhde-daily-catalyst.service  # edit paths and user

systemctl daemon-reload
systemctl enable mhde-daily-catalyst.timer
systemctl start mhde-daily-catalyst.timer
systemctl list-timers mhde-daily-catalyst.timer
```

Required environment variables for the runner:
- `OPENAI_API_KEY` — required; runner will abort if missing
- `DAILY_QUEUE_N` — number of candidates to sample (default: 50)
- `DAILY_QUEUE_SCORE_MIN` — minimum score filter (default: 40)
- `DAILY_QUEUE_SCORE_MAX` — maximum score filter (default: 44.9)
- `DAILY_QUEUE_RPM_LIMIT` — OpenAI requests/minute limit (default: 3)
- `DAILY_QUEUE_SEND_EMAIL` — set to `true` to email digest (default: false)
- `DAILY_CATALYST_EMAIL_TO` — digest recipient email
- `DAILY_CATALYST_REVIEW_URL` — dashboard URL for digest links (e.g. https://mhde.duckdns.org)

## Configure Caddy (HTTPS)

```bash
# Install Caddy: https://caddyserver.com/docs/install
cp .claude/local_scripts/review_server_caddy_example.txt /etc/caddy/Caddyfile
systemctl reload caddy
```

Caddy obtains a Let's Encrypt certificate automatically for `mhde.duckdns.org`.
The review server is never directly exposed; all traffic goes through Caddy.

## Firewall Recommendations

Allow only ports 80 and 443 inbound (HTTP/HTTPS). Port 8765 should be blocked externally:

```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw deny 8765/tcp
ufw enable
```

## Verify

```bash
# Check review server is running
systemctl status mhde-review-server

# Check HTTPS works
curl -sf -u admin:password https://mhde.duckdns.org/ | grep -i shadow

# Check timer is scheduled
systemctl list-timers mhde-daily-catalyst.timer
```
