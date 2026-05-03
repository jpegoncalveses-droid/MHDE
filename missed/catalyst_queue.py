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
from datetime import date, datetime, timezone

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
_QUEUE_HTML = "daily_catalyst_queue.html"

_CSV_COLS = [
    "ticker", "event_date", "filing_form_type", "constructed_url",
    "original_score", "llm_adjustment", "shadow_score",
    "original_tier", "shadow_tier", "tier_move",
    "catalyst_type", "materiality", "sentiment", "confidence",
    "validation_status", "quote_validation_pass", "final_should_affect_score",
    "evidence_quote",
    # interpretation layer (deterministic, no LLM)
    "expected_direction", "expected_move_summary", "expected_timeframe",
    "action_guidance", "action_reason", "key_checks", "priced_in_risk",
    # score decomposition (read-only from scores table)
    "cheap_score", "quality_score", "catalyst_score",
    "momentum_score", "sentiment_score", "risk_penalty_score",
    "major_positives", "major_negatives",
    # scaled shadow adjustment (deterministic v0)
    "days_since_event", "deal_spread_pct",
    "static_adjustment", "scaled_adjustment",
    "evidence_confidence", "impact_estimate",
    "adjustment_reason", "risk_adjustment",
    "time_decay_applied", "scaled_shadow_score",
]


def _enrich_with_interpretation(queue_entries: list[dict]) -> None:
    """Add deterministic interpretation fields to each entry in-place."""
    from missed.catalyst_interpretation import interpret_catalyst
    for entry in queue_entries:
        entry.update(interpret_catalyst(entry))


_COMPONENT_KEYS = ("cheap_score", "quality_score", "catalyst_score",
                   "momentum_score", "sentiment_score", "risk_penalty")
_COMPONENT_WEIGHTS = {
    "cheap_score": 0.30, "quality_score": 0.25, "catalyst_score": 0.25,
    "momentum_score": 0.10, "sentiment_score": 0.10,
}
_POSITIVE_THRESHOLD = 65.0
_NEGATIVE_THRESHOLD = 35.0


def _enrich_queue_with_score_components(
    conn,
    queue_entries: list[dict],
) -> None:
    """Add component score fields and major_positives/negatives to each entry in-place.

    Reads from the scores table (read-only). Never modifies production scores.
    """
    if not queue_entries:
        return

    tickers = list({e["ticker"] for e in queue_entries if e.get("ticker")})
    if not tickers:
        return

    try:
        placeholders = ", ".join("?" * len(tickers))
        rows = conn.execute(
            f"""
            SELECT ticker, cheap_score, quality_score, catalyst_score,
                   momentum_score, sentiment_score, risk_penalty
            FROM scores
            WHERE ticker IN ({placeholders})
            """,
            tickers,
        ).fetchall()
    except Exception:
        return

    by_ticker: dict[str, dict] = {}
    cols = ("ticker", "cheap_score", "quality_score", "catalyst_score",
            "momentum_score", "sentiment_score", "risk_penalty")
    for row in rows:
        d = dict(zip(cols, row))
        by_ticker[d["ticker"]] = d

    for entry in queue_entries:
        t = entry.get("ticker", "")
        comp = by_ticker.get(t)
        if not comp:
            continue

        entry["cheap_score"] = comp.get("cheap_score")
        entry["quality_score"] = comp.get("quality_score")
        entry["catalyst_score"] = comp.get("catalyst_score")
        entry["momentum_score"] = comp.get("momentum_score")
        entry["sentiment_score"] = comp.get("sentiment_score")
        entry["risk_penalty_score"] = comp.get("risk_penalty")

        positives = []
        negatives = []
        for key in ("cheap_score", "quality_score", "catalyst_score",
                    "momentum_score", "sentiment_score"):
            v = comp.get(key)
            if v is None:
                continue
            label = key.replace("_score", "")
            if v >= _POSITIVE_THRESHOLD:
                positives.append(label)
            elif v <= _NEGATIVE_THRESHOLD:
                negatives.append(label)

        risk = comp.get("risk_penalty")
        if risk is not None and risk >= 40:
            negatives.append("risk")

        entry["major_positives"] = "; ".join(positives)
        entry["major_negatives"] = "; ".join(negatives)


def _enrich_with_scaled_adjustment(queue_entries: list[dict]) -> None:
    """Add scaled shadow adjustment fields to each entry in-place (shadow-only)."""
    from missed.catalyst_adjustment import compute_scaled_catalyst_adjustment
    for entry in queue_entries:
        adj = compute_scaled_catalyst_adjustment(entry)
        entry.update(adj)


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

        # Compute days since event for time-decay model
        event_date_raw = r.get("event_date", "")
        try:
            if isinstance(event_date_raw, str) and event_date_raw:
                ed = date.fromisoformat(event_date_raw[:10])
            elif hasattr(event_date_raw, "date"):
                ed = event_date_raw.date()
            elif hasattr(event_date_raw, "year"):
                ed = event_date_raw
            else:
                ed = None
            days_since = (date.today() - ed).days if ed else 0
        except (ValueError, TypeError):
            days_since = 0

        entry = {
            "ticker": ticker,
            "event_date": event_date_raw,
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
            "days_since_event": days_since,
            "deal_spread_pct": r.get("deal_spread_pct"),
        }
        queue_entries.append(entry)

    # Sort: tier crossings first (Reject→C), then by shadow_score DESC
    def _sort_key(e: dict) -> tuple:
        has_crossing = 1 if (e.get("tier_move") and "→C" in e["tier_move"]) else 0
        return (-has_crossing, -e["shadow_score"])

    queue_entries.sort(key=_sort_key)
    _enrich_with_interpretation(queue_entries)
    _enrich_queue_with_score_components(conn, queue_entries)
    _enrich_with_scaled_adjustment(queue_entries)

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
    history_root: str | None = None,
    html_path: str | None = None,
) -> tuple[str, str, str]:
    """Write md + csv + enriched.jsonl artifacts. Returns (md_path, csv_path, jsonl_path).

    If history_root is given, archives artifacts to history_root/YYYY-MM-DD/.
    If html_path is given, it is included in the archive.
    """
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
            direction = e.get("expected_direction", "")
            guidance = e.get("action_guidance", "")
            timeframe = e.get("expected_timeframe", "")
            key_checks = e.get("key_checks", "")
            if direction or guidance:
                lines += [
                    "",
                    f"  > **Action guidance:** {guidance} &nbsp;|&nbsp;"
                    f" **Direction:** {direction} &nbsp;|&nbsp;"
                    f" **Timeframe:** {timeframe}",
                    f"  > **Key checks:** {key_checks}" if key_checks else "",
                ]
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

    # ── History archive ───────────────────────────────────────────────────────
    if history_root:
        from missed.catalyst_history import archive_run
        run_time_str = meta.get("run_time", "")
        run_date = run_time_str[:10] if run_time_str else datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        archive_run(history_root, run_date, md_path, csv_path, jsonl_path, meta, html_path=html_path)

    return md_path, csv_path, jsonl_path


# ── HTML report artifact ──────────────────────────────────────────────────────

def generate_html_report(
    queue_entries: list[dict],
    revalidated: list[dict],
    output_dir: str,
    *,
    run_metadata: dict | None = None,
) -> str:
    """Write daily_catalyst_queue.html. Returns the file path."""
    os.makedirs(output_dir, exist_ok=True)
    meta = run_metadata or {}
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_time = meta.get("run_time", now_str)
    if isinstance(run_time, str) and "T" in run_time:
        run_time = run_time[:16].replace("T", " ") + " UTC"

    promoted = [e for e in queue_entries if e.get("final_should_affect_score")]
    crossings = [e for e in promoted if e.get("tier_move") and "→C" in str(e["tier_move"])]
    valid_no_cross = [e for e in promoted if not e.get("tier_move")]
    bearish = [e for e in queue_entries
               if not e.get("final_should_affect_score") and e.get("sentiment") == "bearish"
               and (e.get("llm_adjustment") or 0) < 0]
    weak = [e for e in queue_entries
            if e.get("validation_status") in ("weak_evidence", "invalid_quote", "neutral_sentiment")]

    source_avail = _source_available_str(meta, revalidated)

    def _esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _link(e: dict) -> str:
        url = e.get("constructed_url") or ""
        t = _esc(e.get("ticker", ""))
        return f'<a href="{_esc(url)}">{t}</a>' if url else t

    def _interpretation_block(e: dict) -> str:
        direction = _esc(e.get("expected_direction", ""))
        guidance = _esc(e.get("action_guidance", ""))
        timeframe = _esc(e.get("expected_timeframe", ""))
        move = _esc(e.get("expected_move_summary", ""))
        checks = _esc(e.get("key_checks", ""))
        priced = _esc(e.get("priced_in_risk", ""))
        if not (direction or guidance):
            return ""
        return (
            f'<div class="interp">'
            f'<span class="interp-item"><strong>Action:</strong> {guidance}</span> '
            f'<span class="interp-item"><strong>Direction:</strong> {direction}</span> '
            f'<span class="interp-item"><strong>Timeframe:</strong> {timeframe}</span> '
            f'<span class="interp-item"><strong>Priced-in risk:</strong> {priced}</span>'
            f'<br><span class="interp-note">{move}</span>'
            + (f'<br><strong>Key checks:</strong> {checks}' if checks else "")
            + f'</div>'
        )

    def _adj_cell(e: dict) -> str:
        """Show both static LLM adj and scaled adj side-by-side."""
        llm = e.get("llm_adjustment", 0.0) or 0.0
        scaled = e.get("scaled_adjustment")
        if scaled is not None:
            return f'{llm:+.1f} / <em>{scaled:+.2f}s</em>'
        return f'{llm:+.1f}'

    def _crossing_row(e: dict) -> str:
        quote = _esc(_safe_cell(e.get("evidence_quote", ""), max_len=250))
        url = e.get("constructed_url") or ""
        sec = f' <a href="{_esc(url)}">[SEC]</a>' if url else ""
        interp = _interpretation_block(e)
        scaled_score = e.get("scaled_shadow_score")
        shadow_cell = (
            f'<strong>{e.get("shadow_score", 0):.1f}</strong>'
            + (f'<br><small style="color:#388e3c">{scaled_score:.1f}s</small>'
               if scaled_score is not None else "")
        )
        return (
            f'<tr class="crossing">'
            f'<td>{_link(e)}</td>'
            f'<td>{e.get("original_score", 0):.1f}</td>'
            f'<td>{shadow_cell}</td>'
            f'<td>{_adj_cell(e)}</td>'
            f'<td>{_esc(_display_catalyst_type(e))}</td>'
            f'<td>{e.get("confidence", 0):.2f}</td>'
            f'<td class="quote">{quote}{sec}'
            + (f'<br>{interp}' if interp else "")
            + f'</td>'
            f'</tr>'
        )

    def _valid_row(e: dict) -> str:
        return (
            f'<tr>'
            f'<td>{_link(e)}</td>'
            f'<td>{e.get("original_score", 0):.1f}</td>'
            f'<td>{e.get("shadow_score", 0):.1f}</td>'
            f'<td>{_adj_cell(e)}</td>'
            f'<td>{_esc(e.get("original_tier", ""))}</td>'
            f'<td>{_esc(e.get("shadow_tier", ""))}</td>'
            f'<td>{_esc(_display_catalyst_type(e))}</td>'
            f'</tr>'
        )

    def _bearish_row(e: dict) -> str:
        return (
            f'<tr class="bearish">'
            f'<td>{_link(e)}</td>'
            f'<td>{e.get("original_score", 0):.1f}</td>'
            f'<td>{e.get("shadow_score", 0):.1f}</td>'
            f'<td>{_adj_cell(e)}</td>'
            f'<td>{_esc(_display_catalyst_type(e))}</td>'
            f'</tr>'
        )

    crossing_rows = "\n".join(_crossing_row(e) for e in crossings)
    valid_rows = "\n".join(_valid_row(e) for e in valid_no_cross)
    bearish_rows = "\n".join(_bearish_row(e) for e in bearish)
    weak_rows_html = "\n".join(
        f'<tr><td>{_esc(e.get("ticker",""))}</td>'
        f'<td>{_esc(e.get("catalyst_type",""))}</td>'
        f'<td>{_esc(e.get("validation_status",""))}</td></tr>'
        for e in weak
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MHDE Catalyst Queue — {_esc(run_time)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto;padding:16px;color:#222;}}
h1{{font-size:1.4rem;margin-bottom:4px;}}
h2{{font-size:1.1rem;margin-top:24px;border-bottom:1px solid #ddd;padding-bottom:4px;}}
.disclaimer{{background:#fff3e0;border-left:4px solid #ff9800;padding:8px 12px;margin:12px 0;font-size:.9rem;}}
table{{border-collapse:collapse;width:100%;font-size:.88rem;margin:8px 0;}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid #eee;}}
th{{background:#f5f5f5;font-weight:600;}}
tr.crossing{{background:#e8f5e9;}}
tr.bearish{{background:#fce4ec;}}
.quote{{max-width:280px;word-break:break-word;}}
details summary{{cursor:pointer;color:#555;}}
a{{color:#1565c0;}}
.interp{{margin-top:6px;padding:6px 8px;background:rgba(255,255,255,.6);border-left:3px solid #1565c0;font-size:.82rem;}}
.interp-item{{margin-right:12px;display:inline-block;}}
.interp-note{{color:#555;font-style:italic;}}
</style>
</head>
<body>
<h1>MHDE Catalyst Queue</h1>
<div class="disclaimer">&#9888; <strong>Shadow-only</strong> — production scores were not changed.</div>
<p style="color:#666;font-size:.85rem;">Generated: {_esc(run_time)}</p>

<h2>Summary</h2>
<table>
<tr><th>Metric</th><th>Count</th></tr>
<tr><td>Sampled near-threshold events</td><td>{meta.get('sampled', len(queue_entries))}</td></tr>
<tr><td>Source text available (≥200 chars)</td><td>{_esc(source_avail)}</td></tr>
<tr><td>LLM classified</td><td>{meta.get('classified', len(queue_entries))}</td></tr>
<tr><td>Valid + actionable (promoted)</td><td>{len(promoted)}</td></tr>
<tr><td>Reject→C tier crossings</td><td>{len(crossings)}</td></tr>
<tr><td>Bearish downgrades</td><td>{len(bearish)}</td></tr>
<tr><td>Weak / rejected evidence</td><td>{len(weak)}</td></tr>
</table>

<h2>Reject→C Tier Crossings</h2>
{"<table><tr><th>Ticker</th><th>Orig</th><th>Shadow</th><th>Adj (llm/s)</th><th>Catalyst</th><th>Conf</th><th>Evidence</th></tr>" + crossing_rows + "</table>" if crossings else "<p><em>None in this run.</em></p>"}

<h2>Valid but No Tier Change</h2>
{"<table><tr><th>Ticker</th><th>Orig</th><th>Shadow</th><th>Adj (llm/s)</th><th>Orig Tier</th><th>Shadow Tier</th><th>Catalyst</th></tr>" + valid_rows + "</table>" if valid_no_cross else "<p><em>None.</em></p>"}

<h2>Bearish Downgrades</h2>
{"<table><tr><th>Ticker</th><th>Orig</th><th>Shadow</th><th>Adj (llm/s)</th><th>Catalyst</th></tr>" + bearish_rows + "</table>" if bearish else "<p><em>None in this run.</em></p>"}

<h2>Weak / Rejected Evidence</h2>
<details><summary>{len(weak)} entries — click to expand</summary>
{"<table><tr><th>Ticker</th><th>Catalyst Type</th><th>Status</th></tr>" + weak_rows_html + "</table>" if weak else "<p><em>None.</em></p>"}
</details>

<h2>Run Metadata</h2>
<table>
<tr><td>Score range</td><td>{meta.get('score_min', 40.0):.1f}–{meta.get('score_max', 44.9):.1f}</td></tr>
<tr><td>Provider</td><td>{_esc(str(meta.get('provider', '—')))}</td></tr>
<tr><td>Run time</td><td>{_esc(run_time)}</td></tr>
</table>
</body>
</html>"""

    html_path = os.path.join(output_dir, _QUEUE_HTML)
    with open(html_path, "w") as f:
        f.write(html)
    return html_path
