"""Pilot review report: markdown summary + CSV for manual inspection."""
from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import datetime

from missed.catalyst_schema import CatalystEnrichment

_REPORT_MD = "catalyst_llm_pilot_report.md"
_REPORT_CSV = "catalyst_llm_pilot_review.csv"


def generate_pilot_report(
    sample: list[dict],
    enriched: list[CatalystEnrichment],
    output_dir: str,
    *,
    target_mode: str = "standard",
) -> tuple[str, str]:
    """Write markdown report + CSV review artifact. Returns (md_path, csv_path)."""
    os.makedirs(output_dir, exist_ok=True)

    # Build lookup: event_id → sample record
    sample_by_id = {r["event_id"]: r for r in sample}

    # ── Compute statistics ────────────────────────────────────────────────────
    n = len(enriched)
    error_records = [e for e in enriched if "[ERROR]" in e.reasoning_short]
    skip_records = [e for e in enriched if "[SKIP]" in e.reasoning_short]
    invalid_quote_records = [e for e in enriched if "[INVALID_QUOTE]" in e.reasoning_short]
    weak_evidence_records = [
        e for e in enriched
        if e.validation_status == "weak_evidence"
        and "[SKIP]" not in e.reasoning_short
        and "[ERROR]" not in e.reasoning_short
    ]
    valid_records = [
        e for e in enriched
        if "[ERROR]" not in e.reasoning_short
        and "[SKIP]" not in e.reasoning_short
        and "[INVALID_QUOTE]" not in e.reasoning_short
    ]
    llm_called_records = [e for e in enriched if "[SKIP]" not in e.reasoning_short]

    # Source text availability from sample
    n_source_available = sum(
        1 for r in sample if (r.get("source_text_char_count") or 0) >= 200
    )
    n_source_missing = len(sample) - n_source_available
    n_quote_pass = sum(
        1 for e in llm_called_records
        if "[INVALID_QUOTE]" not in e.reasoning_short and "[ERROR]" not in e.reasoning_short
    )

    catalyst_counts = Counter(e.catalyst_type for e in valid_records)
    materiality_counts = Counter(e.materiality for e in valid_records)
    sentiment_counts = Counter(e.sentiment for e in valid_records)
    n_score_affecting = sum(1 for e in valid_records if e.should_affect_score)

    # Confidence buckets
    conf_buckets: Counter = Counter()
    for e in valid_records:
        if e.confidence >= 0.8:
            conf_buckets["high (≥0.8)"] += 1
        elif e.confidence >= 0.5:
            conf_buckets["medium (0.5–0.79)"] += 1
        else:
            conf_buckets["low (<0.5)"] += 1

    # Unknown→classified: events where original form was unknown/none but catalyst classified
    unknown_form_classified = [
        e for e in valid_records
        if (sample_by_id.get(e.event_id, {}).get("filing_form_type") or "") == ""
        and e.catalyst_type != "unknown"
    ]

    # High-materiality bullish — only valid + final_should_affect_score=True
    hm_bullish = sorted(
        [e for e in valid_records
         if e.materiality == "high" and e.sentiment == "bullish"
         and e.should_affect_score and e.validation_status == "valid"],
        key=lambda e: -e.confidence,
    )[:20]

    # High-materiality bearish — only valid + final_should_affect_score=True
    hm_bearish = sorted(
        [e for e in valid_records
         if e.materiality == "high" and e.sentiment == "bearish"
         and e.should_affect_score and e.validation_status == "valid"],
        key=lambda e: -e.confidence,
    )[:20]

    provider = enriched[0].provider if enriched else "unknown"

    # ── Build markdown ────────────────────────────────────────────────────────
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    mode_line = (
        f"Mode: **near-threshold** (Reject tickers, score 40.0–44.9)"
        if target_mode == "near-threshold"
        else ""
    )
    lines: list[str] = [
        f"# Catalyst LLM Pilot Report",
        f"",
        f"Generated: {now} | Provider: `{provider}` | Sample: {n} events",
    ]
    if mode_line:
        lines.append(mode_line)
    lines += [
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total classified | {n} |",
        f"| Valid (LLM + grounded quote) | {len(valid_records)} |",
        f"| LLM skipped (no/short source text) | {len(skip_records)} |",
        f"| Invalid quote (unsupported evidence) | {len(invalid_quote_records)} |",
        f"| Weak evidence (boilerplate/wrapper) | {len(weak_evidence_records)} |",
        f"| Errors/failures | {len(error_records)} |",
        f"| final_should_affect_score=True | {n_score_affecting} |",
        f"| Unknown form → classified | {len(unknown_form_classified)} |",
        f"",
        f"## Source Text Coverage",
        f"",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Source text available (≥200 chars) | {n_source_available} |",
        f"| Source text missing/unavailable | {n_source_missing} |",
        f"| LLM called | {len(llm_called_records)} |",
        f"| Quote validation pass | {n_quote_pass} |",
        f"| Unsupported evidence quote | {len(invalid_quote_records)} |",
        f"",
        f"---",
        f"",
        f"## Catalyst Type Distribution",
        f"",
        f"| Catalyst Type | Count |",
        f"|--------------|-------|",
    ]
    for ctype, count in sorted(catalyst_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| {ctype} | {count} |")

    lines += [
        f"",
        f"## Materiality",
        f"",
        f"| Materiality | Count |",
        f"|------------|-------|",
    ]
    for mat, count in sorted(materiality_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| {mat} | {count} |")

    lines += [
        f"",
        f"## Sentiment",
        f"",
        f"| Sentiment | Count |",
        f"|-----------|-------|",
    ]
    for sent, count in sorted(sentiment_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| {sent} | {count} |")

    lines += [
        f"",
        f"## Confidence Buckets",
        f"",
        f"| Bucket | Count |",
        f"|--------|-------|",
    ]
    for bucket, count in sorted(conf_buckets.items()):
        lines.append(f"| {bucket} | {count} |")

    lines += [
        f"",
        f"## Final Score Impact",
        f"",
        f"- final_should_affect_score=True: {n_score_affecting} / {len(valid_records)} valid records",
        f"- Weak evidence (overridden to False): {len(weak_evidence_records)}",
        f"",
        f"---",
        f"",
        f"## Unknown Form → Classified (conversion rate)",
        f"",
        f"Events where the original SEC filing form was unknown/absent but the LLM "
        f"identified a plausible catalyst: **{len(unknown_form_classified)}**",
        f"",
    ]
    if unknown_form_classified:
        lines += [
            f"| Ticker | Event Date | Catalyst Type | Confidence |",
            f"|--------|-----------|--------------|------------|",
        ]
        for e in unknown_form_classified[:10]:
            lines.append(
                f"| {e.ticker} | {e.event_date} | {e.catalyst_type} | {e.confidence:.2f} |"
            )

    lines += [
        f"",
        f"---",
        f"",
        f"## High Materiality — Bullish ({len(hm_bullish)})",
        f"",
    ]
    if hm_bullish:
        lines += [
            f"| Ticker | Event Date | Catalyst Type | Confidence | Evidence |",
            f"|--------|-----------|--------------|------------|----------|",
        ]
        for e in hm_bullish:
            quote = (e.evidence_quote or "—")[:80].replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {e.ticker} | {e.event_date} | {e.catalyst_type} | {e.confidence:.2f} | {quote} |"
            )
    else:
        lines.append("_(none in this sample)_")

    lines += [
        f"",
        f"## High Materiality — Bearish ({len(hm_bearish)})",
        f"",
    ]
    if hm_bearish:
        lines += [
            f"| Ticker | Event Date | Catalyst Type | Confidence | Evidence |",
            f"|--------|-----------|--------------|------------|----------|",
        ]
        for e in hm_bearish:
            quote = (e.evidence_quote or "—")[:80].replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {e.ticker} | {e.event_date} | {e.catalyst_type} | {e.confidence:.2f} | {quote} |"
            )
    else:
        lines.append("_(none in this sample)_")

    # ── Weak / Overridden Candidates ──────────────────────────────────────────
    overridden_records = [
        e for e in enriched
        if e.validation_status in ("weak_evidence", "invalid_quote", "neutral_sentiment")
        and "[SKIP]" not in (e.reasoning_short or "")
        and "[ERROR]" not in (e.reasoning_short or "")
    ]
    lines += [
        f"",
        f"---",
        f"",
        f"## Weak / Overridden Candidates ({len(overridden_records)})",
        f"",
    ]
    if overridden_records:
        lines += [
            f"| Ticker | Catalyst Type | Status | Invalid Reason | Evidence |",
            f"|--------|--------------|--------|----------------|----------|",
        ]
        for e in overridden_records:
            quote = (e.evidence_quote or "—")[:60].replace("|", "\\|").replace("\n", " ")
            reason = (e.invalid_reason or e.validation_status).replace("|", "\\|")
            lines.append(
                f"| {e.ticker} | {e.catalyst_type} | {e.validation_status} "
                f"| {reason} | {quote} |"
            )
    else:
        lines.append("_(none — all LLM classifications passed validation)_")

    if error_records:
        lines += [
            f"",
            f"---",
            f"",
            f"## Failed Classifications ({len(error_records)})",
            f"",
            f"| Ticker | Event Date | Error |",
            f"|--------|-----------|-------|",
        ]
        for e in error_records:
            err_msg = e.reasoning_short.replace("[ERROR]", "").strip()[:80]
            lines.append(f"| {e.ticker} | {e.event_date} | {err_msg} |")

    md_path = os.path.join(output_dir, _REPORT_MD)
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    # ── Build CSV ─────────────────────────────────────────────────────────────
    csv_path = os.path.join(output_dir, _REPORT_CSV)
    csv_cols = [
        "ticker", "event_date", "event_type", "original_root_cause",
        "original_form_type", "catalyst_type", "materiality", "sentiment",
        "confidence",
        "model_should_affect_score", "final_should_affect_score",
        "validation_status", "quote_validation_pass", "invalid_reason",
        "evidence_quote", "reasoning_short",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols)
        writer.writeheader()
        for e in enriched:
            src = sample_by_id.get(e.event_id, {})
            writer.writerow({
                "ticker": e.ticker,
                "event_date": e.event_date,
                "event_type": src.get("event_type", ""),
                "original_root_cause": src.get("primary_root_cause", ""),
                "original_form_type": src.get("filing_form_type", ""),
                "catalyst_type": e.catalyst_type,
                "materiality": e.materiality,
                "sentiment": e.sentiment,
                "confidence": e.confidence,
                "model_should_affect_score": e.model_should_affect_score,
                "final_should_affect_score": e.should_affect_score,
                "validation_status": e.validation_status,
                "quote_validation_pass": e.quote_validation_pass,
                "invalid_reason": e.invalid_reason,
                "evidence_quote": e.evidence_quote,
                "reasoning_short": e.reasoning_short,
            })

    return md_path, csv_path
