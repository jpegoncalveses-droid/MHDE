#!/usr/bin/env bash
# Full MHDE daily analysis pipeline.
# Runs: daily-radar → prediction-vs-actual → enrich-root-causes → priority-refresh-queue → daily-catalyst-queue → (optional) email
set -euo pipefail

MHDE_DIR="/home/jpcg/MHDE"
cd "$MHDE_DIR"

# Load .env if present (never echo values)
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

PYTHON="$MHDE_DIR/venv/bin/python"
mkdir -p "$MHDE_DIR/data/logs"
LOG_FILE="$MHDE_DIR/data/logs/daily_analysis_$(date +%Y-%m-%d).log"

log() {
    echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"
}

# ── Step a: daily-radar ──────────────────────────────────────────────────────
log "=== Step a: daily-radar ==="
SKIP_INGESTION_FLAG=""
if [ "${MHDE_DAILY_SKIP_INGESTION:-false}" = "true" ]; then
    SKIP_INGESTION_FLAG="--skip-ingestion"
fi
"$PYTHON" main.py run daily-radar $SKIP_INGESTION_FLAG 2>&1 | tee -a "$LOG_FILE"

# ── Step b: prediction-vs-actual ─────────────────────────────────────────────
log "=== Step b: prediction-vs-actual ==="
"$PYTHON" main.py missed prediction-vs-actual \
    --lookback-days "${MHDE_PVA_LOOKBACK_DAYS:-30}" \
    2>&1 | tee -a "$LOG_FILE"

# ── Step c: enrich-root-causes ───────────────────────────────────────────────
log "=== Step c: enrich-root-causes ==="
"$PYTHON" main.py missed enrich-root-causes \
    --input data/processed/prediction_vs_actual_rows.csv \
    --output-dir data/processed \
    2>&1 | tee -a "$LOG_FILE"

# ── Step d: priority-refresh-queue ───────────────────────────────────────────
log "=== Step d: priority-refresh-queue ==="
# Note: this CLI is registered under the `data` group (see main.py
# `data_priority_refresh_queue_cmd`), not at the top level. KI-008.
"$PYTHON" main.py data priority-refresh-queue \
    --enriched-csv data/processed/prediction_vs_actual_enriched_rows.csv \
    2>&1 | tee -a "$LOG_FILE"

# ── Step e: daily-catalyst-queue ─────────────────────────────────────────────
log "=== Step e: daily-catalyst-queue ==="
"$PYTHON" main.py missed daily-catalyst-queue \
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
    2>&1 | tee -a "$LOG_FILE"

# ── Step f: optional email (non-fatal) ───────────────────────────────────────
if [ "${MHDE_SEND_EMAIL:-false}" = "true" ] && [ -n "${SMTP_HOST:-}" ]; then
    log "=== Step f: sending email digest ==="
    "$PYTHON" main.py missed daily-catalyst-queue \
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
        --report-only --send-email \
        2>&1 | tee -a "$LOG_FILE" || log "email failed (non-fatal)"
else
    log "email skipped (MHDE_SEND_EMAIL not true or SMTP_HOST missing)"
fi

log "=== MHDE daily analysis complete ==="
