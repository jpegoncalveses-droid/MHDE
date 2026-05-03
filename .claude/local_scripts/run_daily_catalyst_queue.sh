#!/usr/bin/env bash
# Daily catalyst queue runner — sources .env, validates required secrets, logs output.
# Intended for use with the systemd timer (daily_catalyst_queue.timer).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Load .env if present (never echo values)
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Validate required secrets — fail clearly, never echo values
: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set (in environment or .env)}"

mkdir -p data/logs
LOG="data/logs/daily_catalyst_queue_$(date +%Y-%m-%d).log"

# Optional email flag
SEND_EMAIL_FLAG=""
if [ "${DAILY_QUEUE_SEND_EMAIL:-false}" = "true" ]; then
    SEND_EMAIL_FLAG="--send-email"
fi

venv/bin/python main.py missed daily-catalyst-queue \
    --n "${DAILY_QUEUE_N:-50}" \
    --score-min "${DAILY_QUEUE_SCORE_MIN:-40}" \
    --score-max "${DAILY_QUEUE_SCORE_MAX:-44.9}" \
    --max-events-per-ticker 1 \
    --no-mock \
    --provider openai \
    --model gpt-4o-mini \
    --rpm-limit "${DAILY_QUEUE_RPM_LIMIT:-3}" \
    --cache-path data/processed/daily_catalyst_queue_openai_cache_v3.jsonl \
    --history-root data/processed/catalyst_queue_history \
    --html \
    ${SEND_EMAIL_FLAG} \
    2>&1 | tee -a "$LOG"
