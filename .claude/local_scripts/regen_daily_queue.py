"""Regenerate daily_catalyst_queue.md from existing enriched JSONL without LLM calls."""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import duckdb
from missed.catalyst_classifier import revalidate_enrichments
from missed.catalyst_shadow_scorer import compute_shadow_scores
from missed.catalyst_queue import generate_queue_report

JSONL_PATH = "data/processed/daily_catalyst_queue_enriched.jsonl"
DB_PATH = "data/mhde.duckdb"
OUTPUT_DIR = "data/processed"

conn = duckdb.connect(DB_PATH, read_only=True)

# Load existing enrichments
with open(JSONL_PATH) as f:
    raw_records = [json.loads(l) for l in f if l.strip()]
print(f"Loaded {len(raw_records)} enrichments from JSONL")

# Re-apply sufficiency + sentiment rules
revalidated = revalidate_enrichments(raw_records)

# Query latest scores for shadow projection
try:
    score_rows_raw = conn.execute(
        "SELECT ticker, total_score, catalyst_score, risk_penalty, tier, run_id FROM scores"
    ).fetchall()
except Exception as e:
    print(f"Warning: could not load scores: {e}")
    score_rows_raw = []
score_cols = ("ticker", "total_score", "catalyst_score", "risk_penalty", "tier", "run_id")
score_rows = [dict(zip(score_cols, r)) for r in score_rows_raw]
print(f"Loaded {len(score_rows)} score rows")

# Shadow scoring
shadow_rows = compute_shadow_scores(revalidated, score_rows)
shadow_by_ticker = {r["ticker"]: r for r in shadow_rows}

# Score lookup for original tier/score
latest_score: dict[str, dict] = {}
for r in score_rows:
    t = r["ticker"]
    if t not in latest_score or r["run_id"] > latest_score[t]["run_id"]:
        latest_score[t] = r

# Build queue entries from revalidated (no sample_meta available for constructed_url)
seen: set[str] = set()
queue_entries: list[dict] = []
for r in revalidated:
    ticker = r.get("ticker", "")
    if ticker in seen:
        continue
    seen.add(ticker)

    shadow = shadow_by_ticker.get(ticker, {})
    score_meta = latest_score.get(ticker, {})

    orig_score = shadow.get("original_total") or float(score_meta.get("total_score") or 0.0)
    shadow_score = shadow.get("shadow_total", orig_score)
    orig_tier = shadow.get("original_tier") or score_meta.get("tier", "")
    shadow_tier = shadow.get("shadow_tier", orig_tier)

    entry = {
        "ticker": ticker,
        "event_date": r.get("event_date", ""),
        "filing_form_type": None,
        "constructed_url": None,
        "catalyst_type": r.get("catalyst_type", ""),
        "materiality": r.get("materiality", ""),
        "sentiment": r.get("sentiment", ""),
        "confidence": r.get("confidence", 0.0),
        "evidence_quote": r.get("evidence_quote", ""),
        "validation_status": r.get("validation_status", ""),
        "quote_validation_pass": r.get("quote_validation_pass", True),
        "final_should_affect_score": bool(r.get("should_affect_score", False)),
        "original_score": orig_score,
        "original_tier": orig_tier,
        "llm_adjustment": shadow.get("llm_adjustment", 0.0),
        "shadow_score": shadow_score,
        "shadow_tier": shadow_tier,
        "tier_move": shadow.get("tier_move", ""),
        "invalid_reason": r.get("invalid_reason"),
    }
    queue_entries.append(entry)

# Sort: Reject→C crossings first, then shadow_score DESC
def _sort_key(e):
    has_crossing = 1 if (e.get("tier_move") and "→C" in e["tier_move"]) else 0
    return (-has_crossing, -e["shadow_score"])

queue_entries.sort(key=_sort_key)

promoted = [e for e in queue_entries if e["final_should_affect_score"]]
crossings = [e for e in promoted if e.get("tier_move") and "→C" in e["tier_move"]]
weak_rejected = [e for e in queue_entries
                 if e["validation_status"] in ("weak_evidence", "invalid_quote", "neutral_sentiment")]

metadata = {
    "sampled": len(raw_records),
    "classified": len(raw_records),
    "valid_actionable": len(promoted),
    "tier_crossings": len(crossings),
    "score_min": 40.0,
    "score_max": 44.9,
    "provider": "openai (cached)",
}

HISTORY_ROOT = "data/processed/catalyst_queue_history"

print(f"Queue entries: {len(queue_entries)}")
print(f"Promoted: {len(promoted)}, Crossings: {len(crossings)}, Weak/Rejected: {len(weak_rejected)}")

md_path, csv_path, jsonl_path = generate_queue_report(
    queue_entries, revalidated, OUTPUT_DIR,
    run_metadata=metadata,
    history_root=HISTORY_ROOT,
)
print(f"\nReport:  {md_path}")
print(f"CSV:     {csv_path}")
print(f"JSONL:   {jsonl_path}")

# Generate and print history summary
from missed.catalyst_history import generate_history_summary
summary = generate_history_summary(HISTORY_ROOT)
summary_path = os.path.join(HISTORY_ROOT, "history_summary.md")
with open(summary_path, "w") as f:
    f.write(summary)
print(f"Summary: {summary_path}")

conn.close()
