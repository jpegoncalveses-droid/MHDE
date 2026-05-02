"""Shadow scoring experiment: measure how validated LLM catalysts would shift scores.

No production scores are written. This is a read-only analysis artifact.

Adjustment rules (conservative):
  high   + bullish  → +5
  medium + bullish  → +3
  high   + bearish  → -5
  medium + bearish  → -3
  low/none materiality or neutral/mixed sentiment → 0
  confidence < 0.5 → 0
  Per-ticker cap: ±5 total
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime, timezone

from scoring.tiers import assign_tier

_CAP = 5.0

_ADJUSTMENTS: dict[tuple[str, str], float] = {
    ("high", "bullish"): 5.0,
    ("medium", "bullish"): 3.0,
    ("high", "bearish"): -5.0,
    ("medium", "bearish"): -3.0,
}

_REPORT_MD = "catalyst_shadow_score_report.md"
_REPORT_CSV = "catalyst_shadow_score_rows.csv"


def _is_actionable(rec: dict) -> bool:
    if "[SKIP]" in (rec.get("reasoning_short") or ""):
        return False
    if not rec.get("should_affect_score"):
        return False
    if rec.get("validation_status") not in ("valid",):
        return False
    if not rec.get("quote_validation_pass", True):
        return False
    if (rec.get("confidence") or 0.0) < 0.5:
        return False
    return True


def _raw_adjustment(materiality: str, sentiment: str) -> float:
    return _ADJUSTMENTS.get((materiality, sentiment), 0.0)


def _safe_cell(text: str, max_len: int = 80) -> str:
    """Truncate, strip newlines, and escape pipes for use in a markdown table cell."""
    t = (text or "")[:max_len].replace("\n", " ").replace("|", "\\|")
    return t


def compute_shadow_scores(
    enrichments: list[dict],
    scores: list[dict],
) -> list[dict]:
    """Compute shadow score rows — one row per ticker present in scores.

    enrichments: list of enrichment dicts (from JSONL or revalidate_enrichments)
    scores: list of score dicts from the DB (not mutated)

    Returns list of shadow row dicts, one per ticker that has at least one
    enrichment (actionable or not) mapped against a known score.
    """
    # Build score lookup by ticker — use highest total_score if multiple runs
    score_by_ticker: dict[str, dict] = {}
    for s in scores:
        t = s["ticker"]
        if t not in score_by_ticker or s["total_score"] > score_by_ticker[t]["total_score"]:
            score_by_ticker[t] = s

    # Accumulate raw adjustments per ticker from actionable enrichments
    adjustments_by_ticker: dict[str, float] = defaultdict(float)
    actionable_meta: dict[str, dict] = {}  # ticker → last actionable enrichment fields

    for rec in enrichments:
        ticker = rec.get("ticker", "")
        if ticker not in score_by_ticker:
            continue
        if not _is_actionable(rec):
            continue
        mat = rec.get("materiality", "")
        sent = rec.get("sentiment", "")
        conf = rec.get("confidence", 0.0) or 0.0
        adj = _raw_adjustment(mat, sent)
        adjustments_by_ticker[ticker] += adj
        actionable_meta[ticker] = {
            "event_date": rec.get("event_date", ""),
            "catalyst_type": rec.get("catalyst_type", "unknown"),
            "materiality": mat,
            "sentiment": sent,
            "confidence": conf,
            "validation_status": rec.get("validation_status", "valid"),
            "quote_validation_pass": rec.get("quote_validation_pass", True),
            "should_affect_score": rec.get("should_affect_score", False),
            "evidence_quote": rec.get("evidence_quote", ""),
        }

    # Apply per-ticker cap
    capped: dict[str, float] = {}
    for ticker, raw in adjustments_by_ticker.items():
        if raw > _CAP:
            capped[ticker] = _CAP
        elif raw < -_CAP:
            capped[ticker] = -_CAP
        else:
            capped[ticker] = raw

    # Build result rows — one per ticker in scores that appears in enrichments
    rows: list[dict] = []
    seen_tickers: set[str] = set()

    for rec in enrichments:
        ticker = rec.get("ticker", "")
        if ticker not in score_by_ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)

        s = score_by_ticker[ticker]
        orig_total = float(s["total_score"])
        orig_tier = s["tier"]
        adj = capped.get(ticker, 0.0)
        shadow_total = orig_total + adj

        catalyst_score = s.get("catalyst_score") or 0.0
        risk_penalty = s.get("risk_penalty") or 0.0
        shadow_tier = assign_tier(
            shadow_total, catalyst_score, risk_penalty, coverage=1.0
        )
        tier_move = f"{orig_tier}→{shadow_tier}" if orig_tier != shadow_tier else ""

        meta = actionable_meta.get(ticker, {
            "event_date": rec.get("event_date", ""),
            "catalyst_type": rec.get("catalyst_type", "unknown"),
            "materiality": rec.get("materiality", ""),
            "sentiment": rec.get("sentiment", ""),
            "confidence": rec.get("confidence", 0.0) or 0.0,
            "validation_status": rec.get("validation_status", ""),
            "quote_validation_pass": rec.get("quote_validation_pass", True),
            "should_affect_score": rec.get("should_affect_score", False),
            "evidence_quote": rec.get("evidence_quote", ""),
        })

        rows.append({
            "ticker": ticker,
            "event_date": meta["event_date"],
            "run_id": s.get("run_id", ""),
            "catalyst_type": meta["catalyst_type"],
            "materiality": meta["materiality"],
            "sentiment": meta["sentiment"],
            "confidence": meta["confidence"],
            "validation_status": meta["validation_status"],
            "quote_validation_pass": meta["quote_validation_pass"],
            "final_should_affect_score": meta["should_affect_score"],
            "evidence_quote": meta["evidence_quote"],
            "original_total": orig_total,
            "original_tier": orig_tier,
            "llm_adjustment": adj,
            "shadow_total": shadow_total,
            "shadow_tier": shadow_tier,
            "tier_move": tier_move,
        })

    return rows


def generate_shadow_report(
    shadow_rows: list[dict],
    output_dir: str,
) -> tuple[str, str]:
    """Write shadow score report.md + rows.csv. Returns (md_path, csv_path)."""
    os.makedirs(output_dir, exist_ok=True)

    n = len(shadow_rows)
    adjusted = [r for r in shadow_rows if r["llm_adjustment"] != 0.0]
    tier_crossings = [r for r in shadow_rows if r["shadow_tier"] != r["original_tier"]]
    bullish_upgrades = [r for r in tier_crossings if r["llm_adjustment"] > 0]
    bearish_downgrades = [r for r in shadow_rows if r["llm_adjustment"] < 0]
    near_misses = [
        r for r in shadow_rows
        if r["shadow_tier"] == "Reject" and 40.0 <= r["shadow_total"] <= 44.9
    ]

    crossing_buckets: dict[str, list[dict]] = defaultdict(list)
    for r in tier_crossings:
        key = f"{r['original_tier']}→{r['shadow_tier']}"
        crossing_buckets[key].append(r)

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "# Catalyst LLM Shadow Score Report",
        "",
        f"Generated: {now} | Tickers analysed: {n}",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Tickers in shadow run | {n} |",
        f"| Tickers with LLM adjustment | {len(adjusted)} |",
        f"| Tier crossings (any direction) | {len(tier_crossings)} |",
        f"| Bullish upgrades | {len(bullish_upgrades)} |",
        f"| Bearish downgrades | {len(bearish_downgrades)} |",
        f"| Near misses (Reject, shadow 40–44.9) | {len(near_misses)} |",
        f"| No change (adjustment = 0) | {n - len(adjusted)} |",
        "",
        "---",
        "",
        "## Tier Movements",
        "",
    ]

    if crossing_buckets:
        for key in sorted(crossing_buckets):
            group = crossing_buckets[key]
            lines.append(f"### {key} ({len(group)} ticker{'s' if len(group) != 1 else ''})")
            lines.append("")
            lines += [
                "| Ticker | Orig Score | Shadow Score | Adjustment | Catalyst Type |",
                "|--------|-----------|-------------|------------|--------------|",
            ]
            for r in sorted(group, key=lambda x: -x["llm_adjustment"]):
                lines.append(
                    f"| {r['ticker']} | {r['original_total']:.1f} | {r['shadow_total']:.1f}"
                    f" | {r['llm_adjustment']:+.1f} | {r['catalyst_type']} |"
                )
            lines.append("")
    else:
        lines.append("_(no tier crossings in this sample)_")
        lines.append("")

    # ── Adjusted Tickers ──────────────────────────────────────────────────────
    lines += [
        "---",
        "",
        "## Adjusted Tickers",
        "",
        "Tickers where at least one validated LLM catalyst produced a non-zero score adjustment.",
        "",
    ]
    if adjusted:
        lines += [
            "| Ticker | Orig Score | Adj | Shadow Score | Orig Tier | Shadow Tier | Catalyst | Evidence |",
            "|--------|-----------|-----|-------------|-----------|-------------|---------|----------|",
        ]
        for r in sorted(adjusted, key=lambda x: -abs(x["llm_adjustment"])):
            evidence = _safe_cell(r.get("evidence_quote", ""), max_len=60)
            lines.append(
                f"| {r['ticker']} | {r['original_total']:.1f} | {r['llm_adjustment']:+.1f}"
                f" | {r['shadow_total']:.1f} | {r['original_tier']} | {r['shadow_tier']}"
                f" | {r['catalyst_type']} | {evidence} |"
            )
    else:
        lines.append("_(no tickers with non-zero LLM adjustment)_")

    # ── Near Misses ───────────────────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## Near Misses",
        "",
        "Reject tickers with shadow score 40–44.9 — one adjustment away from C-tier.",
        "",
    ]
    if near_misses:
        lines += [
            "| Ticker | Orig Score | Shadow Score | Adjustment | Catalyst Type |",
            "|--------|-----------|-------------|------------|--------------|",
        ]
        for r in sorted(near_misses, key=lambda x: -x["shadow_total"]):
            lines.append(
                f"| {r['ticker']} | {r['original_total']:.1f} | {r['shadow_total']:.1f}"
                f" | {r['llm_adjustment']:+.1f} | {r['catalyst_type']} |"
            )
    else:
        lines.append("_(none in this sample)_")

    # ── Bearish Downgrades ────────────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## Bearish Downgrades",
        "",
    ]
    if bearish_downgrades:
        lines += [
            "| Ticker | Orig Score | Shadow Score | Adjustment | Orig Tier | Shadow Tier |",
            "|--------|-----------|-------------|------------|-----------|-------------|",
        ]
        for r in sorted(bearish_downgrades, key=lambda x: x["llm_adjustment"]):
            lines.append(
                f"| {r['ticker']} | {r['original_total']:.1f} | {r['shadow_total']:.1f}"
                f" | {r['llm_adjustment']:+.1f} | {r['original_tier']} | {r['shadow_tier']} |"
            )
    else:
        lines.append("_(no bearish downgrades in this sample)_")

    # ── All Shadow Rows ───────────────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## All Shadow Rows",
        "",
        "| Ticker | Event Date | Orig Score | Orig Tier | Adj | Shadow Score | Shadow Tier | Materiality | Sentiment | Evidence |",
        "|--------|-----------|-----------|-----------|-----|-------------|-------------|-------------|-----------|----------|",
    ]
    for r in sorted(shadow_rows, key=lambda x: -x["shadow_total"]):
        evidence = _safe_cell(r.get("evidence_quote", ""), max_len=50)
        lines.append(
            f"| {r['ticker']} | {r.get('event_date', '')} | {r['original_total']:.1f}"
            f" | {r['original_tier']} | {r['llm_adjustment']:+.1f} | {r['shadow_total']:.1f}"
            f" | {r['shadow_tier']} | {r['materiality']} | {r['sentiment']} | {evidence} |"
        )

    md_path = os.path.join(output_dir, _REPORT_MD)
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    # CSV
    csv_cols = [
        "ticker", "event_date", "run_id", "catalyst_type", "materiality",
        "sentiment", "confidence", "validation_status", "quote_validation_pass",
        "final_should_affect_score", "evidence_quote",
        "original_total", "original_tier", "llm_adjustment",
        "shadow_total", "shadow_tier", "tier_move",
    ]
    csv_path = os.path.join(output_dir, _REPORT_CSV)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(shadow_rows)

    return md_path, csv_path
