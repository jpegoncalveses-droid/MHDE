"""Daily LLM catalyst shadow review queue.

Orchestrates the full near-threshold pipeline and produces three output artifacts:
  daily_catalyst_queue.md       — human-readable markdown report
  daily_catalyst_queue.csv      — flat review spreadsheet
  daily_catalyst_queue_enriched.jsonl — raw revalidated enrichments for audit

No production scores are written.  Shadow projections are read-only analysis.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import duckdb

from missed.catalyst_classifier import classify_events, revalidate_enrichments
from missed.catalyst_sampler import sample_near_threshold_events
from missed.catalyst_shadow_scorer import _safe_cell, compute_shadow_scores
from missed.catalyst_source_resolver import enrich_events_with_source

logger = logging.getLogger("mhde.missed.catalyst_queue")

_QUEUE_MD = "daily_catalyst_queue.md"


def _normalize_blank_lines(lines: list[str]) -> list[str]:
    """Collapse consecutive blank lines to at most one, and strip trailing blank."""
    result: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        result.append(line)
        prev_blank = is_blank
    while result and result[-1] == "":
        result.pop()
    return result

# ── Catalyst display-label helpers ────────────────────────────────────────────

_REGULATORY_SETTLEMENT_RE = re.compile(
    r'settlement agreement|consent decree|asset freeze', re.IGNORECASE
)
_REGULATORY_COMMERCIAL_RE = re.compile(
    r'commercial deliver|first cargo|first lng|commercial operation|first deliveries',
    re.IGNORECASE,
)


def _regulatory_subtype(evidence_quote: str) -> str | None:
    """Return a display subtype for regulatory entries with specific patterns."""
    q = evidence_quote or ""
    if _REGULATORY_SETTLEMENT_RE.search(q) and _REGULATORY_COMMERCIAL_RE.search(q):
        return "settlement/commercial_agreement"
    return None


def _display_catalyst_type(entry: dict) -> str:
    """Return the catalyst type to display, with subtype refinement where applicable."""
    cat = entry.get("catalyst_type", "")
    if cat == "regulatory":
        sub = _regulatory_subtype(entry.get("evidence_quote", ""))
        if sub:
            return sub
    return cat
_QUEUE_CSV = "daily_catalyst_queue.csv"
_QUEUE_JSONL = "daily_catalyst_queue_enriched.jsonl"

_CSV_COLS = [
    "ticker", "event_date", "filing_form_type", "constructed_url",
    "original_score", "llm_adjustment", "shadow_score",
    "original_tier", "shadow_tier", "tier_move",
    "catalyst_type", "materiality", "sentiment", "confidence",
    "validation_status", "quote_validation_pass", "final_should_affect_score",
    "evidence_quote",
]


def build_daily_queue(
    conn: duckdb.DuckDBPyConnection,
    *,
    n: int = 50,
    score_min: float = 40.0,
    score_max: float = 44.9,
    max_events_per_ticker: int = 1,
    use_mock: bool = True,
    provider_name: str = "nvidia",
    model: str = "meta/llama-3.3-70b-instruct",
    cache_path: str | None = None,
    refresh_cache: bool = False,
    cfg: dict | None = None,
    rpm_limit: int | None = None,
    _provider=None,
    _fetch_fn=None,
) -> tuple[list[dict], list[dict], dict]:
    """Run the full near-threshold catalyst queue pipeline.

    Returns (queue_entries, revalidated_enrichments, metadata).
    queue_entries: one dict per ticker that has enrichments + scores.
    revalidated_enrichments: raw revalidated dicts for the JSONL artifact.
    metadata: run statistics.

    No production scores are written.
    """
    sample = sample_near_threshold_events(
        conn, n=n, score_min=score_min, score_max=score_max,
        max_events_per_ticker=max_events_per_ticker,
    )
    if not sample:
        logger.info("Daily catalyst queue: no near-threshold tickers found")
        return [], [], {"sampled": 0}

    # Source resolution
    enriched_sample = enrich_events_with_source(sample, _fetch_fn=_fetch_fn)
    n_source = sum(1 for s in enriched_sample if (s.get("source_text_char_count") or 0) >= 200)
    logger.info("Daily catalyst queue: %d/%d events have source text", n_source, len(enriched_sample))

    # LLM classification
    enriched_objs = classify_events(
        enriched_sample,
        use_mock=use_mock,
        provider_name=provider_name,
        model=model,
        cache_path=cache_path,
        refresh_cache=refresh_cache,
        cfg=cfg,
        rpm_limit=rpm_limit,
        _provider=_provider,
    )

    # Revalidation (sufficiency + sentiment filter)
    raw_dicts = [e.to_dict() for e in enriched_objs]
    revalidated = revalidate_enrichments(raw_dicts)

    # Load scores for shadow projection
    try:
        score_rows_raw = conn.execute(
            "SELECT ticker, total_score, catalyst_score, risk_penalty, tier, run_id FROM scores"
        ).fetchall()
    except Exception:
        score_rows_raw = []
    score_cols = ("ticker", "total_score", "catalyst_score", "risk_penalty", "tier", "run_id")
    score_rows = [dict(zip(score_cols, r)) for r in score_rows_raw]

    # Shadow scoring (read-only)
    shadow_rows = compute_shadow_scores(revalidated, score_rows)
    shadow_by_ticker: dict[str, dict] = {r["ticker"]: r for r in shadow_rows}

    # Sample metadata by ticker (most recent entry per ticker)
    sample_by_ticker: dict[str, dict] = {}
    for s in enriched_sample:
        t = s.get("ticker", "")
        sample_by_ticker.setdefault(t, s)

    # Build queue entries — merge enrichment + shadow + sample metadata
    seen: set[str] = set()
    queue_entries: list[dict] = []
    for r in revalidated:
        ticker = r.get("ticker", "")
        if ticker in seen:
            continue
        seen.add(ticker)

        shadow = shadow_by_ticker.get(ticker, {})
        sample_meta = sample_by_ticker.get(ticker, {})

        orig_score = shadow.get("original_total") or float(sample_meta.get("current_score") or 0.0)
        shadow_score = shadow.get("shadow_total", orig_score)
        orig_tier = shadow.get("original_tier") or sample_meta.get("current_tier", "")
        shadow_tier = shadow.get("shadow_tier", orig_tier)

        entry = {
            "ticker": ticker,
            "event_date": r.get("event_date", ""),
            "filing_form_type": sample_meta.get("filing_form_type"),
            "constructed_url": sample_meta.get("constructed_url"),
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
        }
        queue_entries.append(entry)

    # Sort: tier crossings first (Reject→C), then by shadow_score DESC
    def _sort_key(e: dict) -> tuple:
        has_crossing = 1 if (e.get("tier_move") and "→C" in e["tier_move"]) else 0
        return (-has_crossing, -e["shadow_score"])

    queue_entries.sort(key=_sort_key)

    n_promoted = sum(1 for e in queue_entries if e["final_should_affect_score"])
    n_crossings = sum(1 for e in queue_entries if e.get("tier_move") and "→C" in e["tier_move"])
    metadata = {
        "sampled": len(sample),
        "source_available": n_source,
        "classified": len(enriched_objs),
        "valid_actionable": n_promoted,
        "tier_crossings": n_crossings,
        "run_time": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info(
        "Daily catalyst queue: %d entries, %d promoted, %d Reject→C crossings",
        len(queue_entries), n_promoted, n_crossings,
    )
    return queue_entries, revalidated, metadata


_SOURCE_TEXT_THRESHOLD = 200


def _source_available_str(meta: dict, revalidated: list[dict]) -> str:
    """Return a numeric string for source_available, computing from revalidated if needed."""
    val = meta.get("source_available")
    if isinstance(val, int):
        return str(val)
    # Fallback: count revalidated records whose source_text_char_count meets threshold
    computed = sum(
        1 for r in revalidated
        if (r.get("source_text_char_count") or 0) >= _SOURCE_TEXT_THRESHOLD
    )
    return str(computed) if computed > 0 else "—"


def generate_queue_report(
    queue_entries: list[dict],
    revalidated: list[dict],
    output_dir: str,
    *,
    run_metadata: dict | None = None,
) -> tuple[str, str, str]:
    """Write md + csv + enriched.jsonl artifacts. Returns (md_path, csv_path, jsonl_path)."""
    os.makedirs(output_dir, exist_ok=True)
    meta = run_metadata or {}
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Partition entries by section
    promoted = [e for e in queue_entries if e["final_should_affect_score"]]
    bearish_down = [e for e in queue_entries
                    if not e["final_should_affect_score"] and e["sentiment"] == "bearish"
                    and e["llm_adjustment"] < 0]
    valid_no_cross = [e for e in promoted if not e.get("tier_move")]
    crossings = [e for e in promoted if e.get("tier_move")]
    weak_rejected = [e for e in queue_entries
                     if e["validation_status"] in ("weak_evidence", "invalid_quote", "neutral_sentiment")]

    # ── Markdown ──────────────────────────────────────────────────────────────
    lines: list[str] = [
        "# Daily Catalyst Queue",
        "",
        f"Generated: {now}",
        "",
        "> **Shadow-only: production scores were not changed.**",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Sampled near-threshold events | {meta.get('sampled', len(queue_entries))} |",
        f"| Source text available | {_source_available_str(meta, revalidated)} |",
        f"| LLM classified | {meta.get('classified', len(queue_entries))} |",
        f"| Valid + actionable (promoted) | {len(promoted)} |",
        f"| Reject→C tier crossings | {len(crossings)} |",
        f"| Bearish downgrades | {len(bearish_down)} |",
        f"| Weak / rejected evidence | {len(weak_rejected)} |",
        "",
        "---",
        "",
        "## Promoted Candidates",
        "",
        "Tickers with validated LLM catalyst and positive shadow score adjustment.",
        "",
    ]

    def _ticker_cell(e: dict) -> str:
        url = e.get("constructed_url")
        ticker = e["ticker"]
        return f"[{ticker}]({url})" if url else ticker

    if crossings:
        lines += [
            "### Reject→C Tier Crossings",
            "",
            "| Ticker | Orig Score | Shadow Score | Adj | Catalyst Type | Confidence | Evidence |",
            "|--------|-----------|-------------|-----|--------------|------------|----------|",
        ]
        for e in crossings:
            quote = _safe_cell(e.get("evidence_quote", ""), max_len=210)
            lines.append(
                f"| {_ticker_cell(e)} | {e['original_score']:.1f} | {e['shadow_score']:.1f}"
                f" | {e['llm_adjustment']:+.1f} | {_display_catalyst_type(e)}"
                f" | {e['confidence']:.2f} | {quote} |"
            )
        lines.append("")

    if valid_no_cross:
        lines += [
            "### Valid but No Tier Change",
            "",
            "| Ticker | Orig Score | Shadow Score | Adj | Orig Tier | Shadow Tier | Catalyst Type |",
            "|--------|-----------|-------------|-----|-----------|------------|--------------|",
        ]
        for e in valid_no_cross:
            lines.append(
                f"| {_ticker_cell(e)} | {e['original_score']:.1f} | {e['shadow_score']:.1f}"
                f" | {e['llm_adjustment']:+.1f} | {e['original_tier']} | {e['shadow_tier']}"
                f" | {_display_catalyst_type(e)} |"
            )
        lines.append("")

    if not promoted:
        lines.append("_(no promoted candidates in this run)_")
        lines.append("")

    lines += [
        "---",
        "",
        "## Bearish Downgrades",
        "",
    ]
    if bearish_down:
        lines += [
            "| Ticker | Orig Score | Shadow Score | Adj | Catalyst Type |",
            "|--------|-----------|-------------|-----|--------------|",
        ]
        for e in sorted(bearish_down, key=lambda x: x["llm_adjustment"]):
            lines.append(
                f"| {_ticker_cell(e)} | {e['original_score']:.1f} | {e['shadow_score']:.1f}"
                f" | {e['llm_adjustment']:+.1f} | {_display_catalyst_type(e)} |"
            )
        lines.append("")
    else:
        lines.append("_(none in this run)_")

    lines += [
        "",
        "---",
        "",
        "## Weak / Rejected Evidence",
        "",
    ]
    if weak_rejected:
        lines += [
            "| Ticker | Catalyst Type | Status | Reason |",
            "|--------|--------------|--------|--------|",
        ]
        for e in weak_rejected:
            reason = (e.get("invalid_reason") or e["validation_status"]).replace("|", "\\|")
            lines.append(
                f"| {e['ticker']} | {e['catalyst_type']} | {e['validation_status']} | {reason} |"
            )
        lines.append("")
    else:
        lines.append("_(no weak or rejected evidence)_")

    lines += [
        "",
        "---",
        "",
        "## Source Coverage",
        "",
        f"- Near-threshold events sampled: {meta.get('sampled', '—')}",
        f"- Source text available (≥200 chars): {_source_available_str(meta, revalidated)}",
        "",
        "---",
        "",
        "## Run Metadata",
        "",
        f"- Run time: {meta.get('run_time', now)}",
        f"- Score range: {meta.get('score_min', 40.0):.1f}–{meta.get('score_max', 44.9):.1f}",
        f"- Provider: {meta.get('provider', '—')}",
        "",
    ]

    lines = _normalize_blank_lines(lines)
    md_path = os.path.join(output_dir, _QUEUE_MD)
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    # ── CSV ───────────────────────────────────────────────────────────────────
    csv_path = os.path.join(output_dir, _QUEUE_CSV)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(queue_entries)

    # ── JSONL ─────────────────────────────────────────────────────────────────
    jsonl_path = os.path.join(output_dir, _QUEUE_JSONL)
    with open(jsonl_path, "w") as f:
        for r in revalidated:
            f.write(json.dumps(r) + "\n")

    return md_path, csv_path, jsonl_path
