#!/usr/bin/env bash
set -euo pipefail

: "${DUCKDNS_DOMAIN:?Missing DUCKDNS_DOMAIN — set it in .env or environment}"
: "${DUCKDNS_TOKEN:?Missing DUCKDNS_TOKEN — set it in .env or environment}"

curl -fsS "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip="
echo

# ── Usage ──────────────────────────────────────────────────────────────────────
# 1. chmod +x deploy/dashboard/duckdns-update.sh
# 2. Set DUCKDNS_DOMAIN and DUCKDNS_TOKEN in deploy/dashboard/.env
# 3. Source the .env or export vars, then run this script.
#
# For automatic updates (cron):
#   echo "*/5 * * * * /opt/mhde/deploy/dashboard/duckdns-update.sh" | crontab -
