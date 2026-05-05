"""Diagnostic: identify tickers with 1d return >= 5% and check MHDE coverage."""
import csv
import datetime
import json
import os
import sys

import duckdb

DB_PATH = "data/mhde.duckdb"
HISTORY_ROOT = "data/processed/catalyst_queue_history"
OUTPUT_DIR = "data/processed"
MOVES_PATH = os.path.join(OUTPUT_DIR, "prediction_vs_actual_rows.csv")
ENRICHED_PATH = os.path.join(OUTPUT_DIR, "prediction_vs_actual_enriched_rows.csv")
THRESHOLD = 5.0  # 1d return percent

conn = duckdb.connect(DB_PATH, read_only=True)

# ── 1. Latest run timestamp and price date ─────────────────────────────────
dates = sorted(
    [d for d in os.listdir(HISTORY_ROOT) if d[:4].isdigit()], reverse=True
)
latest_run_date = dates[0] if dates else "—"
run_meta_path = os.path.join(HISTORY_ROOT, latest_run_date, "run_metadata.json")
run_ts = "—"
if os.path.exists(run_meta_path):
    with open(run_meta_path) as f:
        meta = json.load(f)
    run_ts = meta.get("run_started_at") or meta.get("run_at") or str(meta)[:80]

latest_price_date = conn.execute(
    "SELECT MAX(trade_date) FROM prices_daily"
).fetchone()[0]

print(f"Latest run: {latest_run_date}  (metadata ts: {run_ts})")
print(f"Latest price date in prices_daily: {latest_price_date}")
print()

# ── 2. Tickers with 1d return >= threshold ────────────────────────────────
movers = conn.execute("""
    WITH deduped AS (
        SELECT ticker, trade_date,
               COALESCE(adjusted_close, close) AS price,
               ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY created_at DESC NULLS LAST) AS rn
        FROM prices_daily
    ),
    clean AS (SELECT ticker, trade_date, price FROM deduped WHERE rn = 1),
    ranked AS (
        SELECT ticker, trade_date, price,
               ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS seq
        FROM clean
    )
    SELECT
        t1.ticker,
        t1.price  AS latest_close,
        t2.price  AS prev_close,
        t1.trade_date AS latest_date,
        (t1.price - t2.price) / t2.price * 100 AS ret_1d
    FROM ranked t1
    JOIN ranked t2 ON t1.ticker = t2.ticker AND t2.seq = 2
    WHERE t1.seq = 1
      AND (t1.price - t2.price) / t2.price * 100 >= ?
    ORDER BY ret_1d DESC
""", [THRESHOLD]).fetchall()

if not movers:
    print(f"No tickers with 1d return >= {THRESHOLD}% found.")
    conn.close()
    sys.exit(0)

print(f"Tickers with 1d return >= {THRESHOLD}%: {len(movers)}")
print()

# ── 3. Load reference sets ────────────────────────────────────────────────
# Latest catalyst queue (promoted = candidates)
candidates_set: set[str] = set()
queue_path = os.path.join(HISTORY_ROOT, latest_run_date, "daily_catalyst_queue.csv")
if os.path.exists(queue_path):
    with open(queue_path, newline="") as f:
        for row in csv.DictReader(f):
            promo_val = str(row.get("final_should_affect_score", "")).strip().lower()
            if promo_val in ("true", "1", "yes"):
                candidates_set.add(row["ticker"])

# /moves = prediction_vs_actual_rows.csv tickers
moves_set: set[str] = set()
if os.path.exists(MOVES_PATH):
    with open(MOVES_PATH, newline="") as f:
        for row in csv.DictReader(f):
            moves_set.add(row.get("ticker", ""))

# /learning = enriched rows
learning_set: set[str] = set()
if os.path.exists(ENRICHED_PATH):
    with open(ENRICHED_PATH, newline="") as f:
        for row in csv.DictReader(f):
            learning_set.add(row.get("ticker", ""))

# ── 4. Per-ticker details ─────────────────────────────────────────────────
today = str(datetime.date.today())

for ticker, latest_close, prev_close, latest_date, ret_1d in movers:
    # Universe info
    comp = conn.execute(
        "SELECT universe_tier, is_active FROM companies WHERE ticker = ?", [ticker]
    ).fetchone()
    uni_tier = comp[0] if comp else "NOT IN DB"
    is_active = comp[1] if comp else None

    # Latest score
    score_row = conn.execute(
        "SELECT as_of_date, total_score, tier FROM scores WHERE ticker = ? ORDER BY as_of_date DESC LIMIT 1",
        [ticker]
    ).fetchone()
    score_info = f"{score_row[1]:.1f}/{score_row[2]}" if score_row else "—"

    # Staleness: is latest_date the most recent in our DB?
    stale = str(latest_date) != str(latest_price_date)

    in_candidates = ticker in candidates_set
    in_moves = ticker in moves_set
    in_learning = ticker in learning_set

    print(f"{'─'*60}")
    print(f"  {ticker:8s}  +{ret_1d:.1f}%  close=${latest_close:.2f}  prev=${prev_close:.2f}  date={latest_date}")
    print(f"           universe={uni_tier}  active={is_active}  score={score_info}  stale={stale}")
    print(f"           candidates={'✓' if in_candidates else '✗'}  moves={'✓' if in_moves else '✗'}  learning={'✓' if in_learning else '✗'}")

    # Diagnose if not in /moves
    if not in_moves:
        reasons = []
        if comp is None:
            reasons.append("outside universe (not in companies table)")
        elif not is_active:
            reasons.append("inactive ticker")
        elif stale:
            reasons.append(f"stale price (latest={latest_date}, db_max={latest_price_date})")
        elif ret_1d < 5.0:
            reasons.append("below 5% threshold (edge case)")
        else:
            # Score check
            if not score_row:
                reasons.append("no score in scores table")
            else:
                reasons.append(f"not flagged as missed move — score={score_row[1]:.1f}, tier={score_row[2]} — check missed move detector window/threshold")
        print(f"           MISSING from /moves: {'; '.join(reasons)}")

print(f"{'─'*60}")
print()
print(f"Summary: {len(movers)} movers >= {THRESHOLD}%")
print(f"  In /candidates: {sum(1 for t,*_ in movers if t in candidates_set)}")
print(f"  In /moves:      {sum(1 for t,*_ in movers if t in moves_set)}")
print(f"  In /learning:   {sum(1 for t,*_ in movers if t in learning_set)}")
print(f"  Missing /moves: {sum(1 for t,*_ in movers if t not in moves_set)}")

conn.close()
