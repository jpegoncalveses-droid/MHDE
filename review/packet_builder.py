"""Review packet builder — generates structured candidate review packages."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger("mhde.review")

_REVIEW_TEMPLATE: dict[str, Any] = {
    "review_status": "pending",
    "usefulness_score": None,
    "thesis_quality_score": None,
    "evidence_quality_score": None,
    "false_positive_reason": None,
    "missed_risk": None,
    "missing_evidence": None,
    "review_notes": None,
}

_STRONG_COMPONENT_THRESHOLD = 60.0

_SHARES_CONCEPTS = (
    "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap/WeightedAverageNumberOfSharesOutstandingBasic",
    "us-gaap/CommonStockSharesOutstanding",
    "us-gaap/CommonStockSharesIssued",
)


@dataclass
class ReviewPacket:
    run_id: str
    run_date: str
    generated_at: str
    sections: dict[str, list[dict]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_latest_run_id(conn: duckdb.DuckDBPyConnection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM scores GROUP BY run_id ORDER BY MAX(created_at) DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _score_rows(conn: duckdb.DuckDBPyConnection, run_id: str, where: str = "",
                order: str = "total_score DESC", limit: int = 100) -> list[dict]:
    sql = f"""
        SELECT
            s.ticker,
            c.company_name,
            s.tier,
            ROUND(s.total_score, 1)    AS total_score,
            ROUND(s.cheap_score, 1)    AS cheap_score,
            ROUND(s.quality_score, 1)  AS quality_score,
            ROUND(s.catalyst_score, 1) AS catalyst_score,
            ROUND(s.momentum_score, 1) AS momentum_score,
            ROUND(s.sentiment_score, 1) AS sentiment_score,
            ROUND(s.risk_penalty, 1)   AS risk_penalty,
            s.confidence,
            s.why_ranked,
            s.why_rejected,
            s.missing_data_json
        FROM scores s
        LEFT JOIN companies c ON s.ticker = c.ticker
        WHERE s.run_id = '{run_id}' {('AND ' + where) if where else ''}
        ORDER BY {order}
        LIMIT {limit}
    """
    rows = conn.execute(sql).fetchall()
    cols = ["ticker", "company_name", "tier", "total_score", "cheap_score",
            "quality_score", "catalyst_score", "momentum_score", "sentiment_score",
            "risk_penalty", "confidence", "why_ranked", "why_rejected", "missing_data_json"]
    return [dict(zip(cols, r)) for r in rows]


def _enrich_with_hypothesis(conn: duckdb.DuckDBPyConnection, run_id: str,
                             candidates: list[dict]) -> list[dict]:
    if not candidates:
        return candidates
    tickers = [c["ticker"] for c in candidates]
    placeholders = ",".join(["?" for _ in tickers])
    rows = conn.execute(
        f"""SELECT ticker, thesis, why_now, cheap_evidence_json, quality_evidence_json,
                catalyst_evidence_json, risks_json, missing_evidence_json
            FROM hypotheses WHERE run_id = ? AND ticker IN ({placeholders})""",
        [run_id] + tickers,
    ).fetchall()
    hyp_map = {r[0]: r[1:] for r in rows}
    for c in candidates:
        hyp = hyp_map.get(c["ticker"])
        if hyp:
            c["thesis"] = hyp[0]
            c["why_now"] = hyp[1]
            c["cheap_evidence"] = _parse_json(hyp[2])
            c["quality_evidence"] = _parse_json(hyp[3])
            c["catalyst_evidence"] = _parse_json(hyp[4])
            c["risks"] = _parse_json(hyp[5])
            c["missing_evidence"] = _parse_json(hyp[6])
        else:
            c["thesis"] = None
            c["why_now"] = None
            c["cheap_evidence"] = []
            c["quality_evidence"] = []
            c["catalyst_evidence"] = []
            c["risks"] = []
            c["missing_evidence"] = []
        c["missing_data"] = _parse_json(c.pop("missing_data_json", None))
        c["review"] = dict(_REVIEW_TEMPLATE)
    return candidates


def _enrich_with_valuation_metrics(conn: duckdb.DuckDBPyConnection, run_id: str,
                                    candidates: list[dict]) -> list[dict]:
    """Add price, market_cap, and computed ratio values from features/prices_daily."""
    if not candidates:
        return candidates
    tickers = [c["ticker"] for c in candidates]
    ph = ",".join(["?"] * len(tickers))

    # Latest price per ticker
    price_rows = conn.execute(
        f"SELECT ticker, close, trade_date FROM prices_daily "
        f"WHERE ticker IN ({ph}) "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) = 1",
        tickers,
    ).fetchall()
    prices = {r[0]: (r[1], str(r[2])) for r in price_rows}

    # Valuation feature values (ps_proxy, pe_ratio, pb_ratio feature_value = ratio)
    feat_rows = conn.execute(
        f"SELECT ticker, feature_name, feature_value FROM features "
        f"WHERE run_id=? AND ticker IN ({ph}) "
        f"AND feature_name IN ('ps_proxy', 'pe_ratio', 'pb_ratio')",
        [run_id] + tickers,
    ).fetchall()
    feat_map: dict[str, dict] = defaultdict(dict)
    for ticker, fname, fval in feat_rows:
        feat_map[ticker][fname] = fval

    # Shares for market cap (latest per ticker)
    shares_ph = ",".join(["?"] * len(_SHARES_CONCEPTS))
    shares_rows = conn.execute(
        f"SELECT ticker, value FROM fundamentals_raw "
        f"WHERE ticker IN ({ph}) AND concept IN ({shares_ph}) AND value IS NOT NULL "
        f"QUALIFY ROW_NUMBER() OVER ("
        f"  PARTITION BY ticker "
        f"  ORDER BY CASE concept "
        + " ".join(f"WHEN ? THEN {i}" for i, _ in enumerate(_SHARES_CONCEPTS))
        + f" END, as_of_date DESC) = 1",
        tickers + list(_SHARES_CONCEPTS) + list(_SHARES_CONCEPTS),
    ).fetchall()
    shares_map = {r[0]: r[1] for r in shares_rows}

    for c in candidates:
        t = c["ticker"]
        price_data = prices.get(t)
        price = price_data[0] if price_data else None
        price_date = price_data[1] if price_data else None
        shares = shares_map.get(t)
        market_cap = (price * shares / 1e9) if price and shares else None  # billions

        c["valuation_metrics"] = {
            "price": round(price, 2) if price else None,
            "price_date": price_date,
            "market_cap_b": round(market_cap, 2) if market_cap else None,
            "ps_ratio": round(feat_map[t].get("ps_proxy"), 2) if feat_map[t].get("ps_proxy") is not None else None,
            "pe_ratio": round(feat_map[t].get("pe_ratio"), 2) if feat_map[t].get("pe_ratio") is not None else None,
            "pb_ratio": round(feat_map[t].get("pb_ratio"), 2) if feat_map[t].get("pb_ratio") is not None else None,
        }
    return candidates


def _enrich_with_guard_hits(conn: duckdb.DuckDBPyConnection, run_id: str,
                             candidates: list[dict]) -> list[dict]:
    """Add per-candidate guard hit summary from the features table."""
    if not candidates:
        return candidates
    tickers = [c["ticker"] for c in candidates]
    ph = ",".join(["?"] * len(tickers))
    rows = conn.execute(
        f"""SELECT ticker, feature_group, feature_name, confidence, metadata_json
            FROM features
            WHERE run_id = ? AND ticker IN ({ph})
              AND (confidence = 'low'
                   OR metadata_json LIKE '%missing_reason%'
                   OR metadata_json LIKE '%stale_fundamentals_days%'
                   OR metadata_json LIKE '%warning%')""",
        [run_id] + tickers,
    ).fetchall()

    hits_by_ticker: dict[str, list[str]] = {t: [] for t in tickers}
    for ticker, grp, name, conf, meta_json in rows:
        meta = _parse_json(meta_json) or {}
        parts = [f"{grp}.{name}"]
        if isinstance(meta, dict):
            if "missing_reason" in meta:
                parts.append(f"missing_reason={meta['missing_reason']}")
            if "stale_fundamentals_days" in meta:
                parts.append(f"stale_fundamentals_days={meta['stale_fundamentals_days']}")
            if "warning" in meta:
                parts.append(f"warning={meta['warning']}")
        hit = ": ".join(parts)
        if hit not in hits_by_ticker.get(ticker, []):
            hits_by_ticker.setdefault(ticker, []).append(hit)

    for c in candidates:
        c["guard_hits"] = hits_by_ticker.get(c["ticker"], [])
    return candidates


def _enrich_with_catalyst_evidence(conn: duckdb.DuckDBPyConnection, run_id: str,
                                    candidates: list[dict]) -> list[dict]:
    """Append actual filing and upcoming-event evidence to each candidate's catalyst_evidence."""
    if not candidates:
        return candidates
    tickers = [c["ticker"] for c in candidates]
    ph = ",".join(["?"] * len(tickers))
    today = date.today()
    sixty_days_ago = (today - timedelta(days=60)).isoformat()
    fourteen_days_out = (today + timedelta(days=14)).isoformat()

    filing_rows = conn.execute(
        f"""SELECT ticker, form_type, filing_date, description
            FROM filings
            WHERE ticker IN ({ph}) AND filing_date >= ?
            ORDER BY filing_date DESC""",
        tickers + [sixty_days_ago],
    ).fetchall()

    event_rows = conn.execute(
        f"""SELECT ticker, event_type, event_date, title
            FROM events
            WHERE ticker IN ({ph}) AND event_date <= ? AND is_upcoming = true
            ORDER BY event_date ASC""",
        tickers + [fourteen_days_out],
    ).fetchall()

    evidence_by_ticker: dict[str, list[dict]] = {t: [] for t in tickers}
    for ticker, form_type, filing_date, description in filing_rows:
        evidence_by_ticker.setdefault(ticker, []).append({
            "type": "filing",
            "date": str(filing_date),
            "detail": f"{form_type}: {description or '(no description)'}",
        })
    for ticker, event_type, event_date, title in event_rows:
        evidence_by_ticker.setdefault(ticker, []).append({
            "type": "event",
            "date": str(event_date),
            "detail": title or event_type,
        })

    for c in candidates:
        existing = c.get("catalyst_evidence") or []
        if not isinstance(existing, list):
            existing = []
        c["catalyst_evidence"] = existing + evidence_by_ticker.get(c["ticker"], [])
    return candidates


def _enrich(conn: duckdb.DuckDBPyConnection, run_id: str, rows: list[dict]) -> list[dict]:
    rows = _enrich_with_hypothesis(conn, run_id, rows)
    rows = _enrich_with_valuation_metrics(conn, run_id, rows)
    rows = _enrich_with_guard_hits(conn, run_id, rows)
    rows = _enrich_with_catalyst_evidence(conn, run_id, rows)
    return rows


def _parse_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


# ── Sections ──────────────────────────────────────────────────────────────────

def _section_c_tier(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    rows = _score_rows(conn, run_id, where="s.tier='C'", order="total_score DESC", limit=50)
    return _enrich(conn, run_id, rows)


def _section_top_reject(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    rows = _score_rows(conn, run_id, where="s.tier='Reject'", order="total_score DESC", limit=10)
    return _enrich(conn, run_id, rows)


def _section_top_cheap(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    rows = _score_rows(conn, run_id, where="s.cheap_score IS NOT NULL",
                       order="s.cheap_score DESC, s.total_score DESC", limit=10)
    return _enrich(conn, run_id, rows)


def _section_top_quality(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    rows = _score_rows(conn, run_id, where="s.quality_score IS NOT NULL",
                       order="s.quality_score DESC, s.total_score DESC", limit=10)
    return _enrich(conn, run_id, rows)


def _section_top_catalyst(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    rows = _score_rows(conn, run_id, where="s.catalyst_score IS NOT NULL",
                       order="s.catalyst_score DESC, s.total_score DESC", limit=10)
    return _enrich(conn, run_id, rows)


def _section_cheap_quality_weak_catalyst(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    """Best cheap+quality profiles where catalyst is weak (<50) — watch list material."""
    rows = _score_rows(
        conn, run_id,
        where="s.cheap_score IS NOT NULL AND s.quality_score IS NOT NULL AND (s.catalyst_score IS NULL OR s.catalyst_score < 50)",
        order="(COALESCE(s.cheap_score, 0) + COALESCE(s.quality_score, 0)) DESC, s.total_score DESC",
        limit=10,
    )
    return _enrich(conn, run_id, rows)


# ── Cross-reference summary ────────────────────────────────────────────────────

_SECTION_LABELS = {
    "c_tier": "C-tier",
    "top_reject": "Reject (top)",
    "top_cheap": "Cheap (top)",
    "top_quality": "Quality (top)",
    "top_catalyst": "Catalyst (top)",
    "cheap_quality_weak_catalyst": "CheapQuality/NoCatalyst",
}


def _build_cross_reference(sections: dict[str, list[dict]], all_scores: dict[str, dict]) -> list[dict]:
    ticker_sections: dict[str, list[str]] = defaultdict(list)
    for key, candidates in sections.items():
        label = _SECTION_LABELS.get(key, key)
        for c in candidates:
            t = c.get("ticker")
            if t and label not in ticker_sections[t]:
                ticker_sections[t].append(label)

    rows = []
    for ticker, section_list in sorted(ticker_sections.items()):
        count = len(section_list)
        if count >= 3:
            priority = "high"
        elif count == 2:
            priority = "medium"
        else:
            priority = "low"

        sc = all_scores.get(ticker, {})
        # C-tier always gets high priority regardless of section count
        if sc.get("tier") == "C":
            priority = "high"

        rows.append({
            "ticker": ticker,
            "sections_appeared_in": ", ".join(section_list),
            "section_count": count,
            "tier": sc.get("tier", "?"),
            "total_score": sc.get("total_score"),
            "cheap_score": sc.get("cheap_score"),
            "quality_score": sc.get("quality_score"),
            "catalyst_score": sc.get("catalyst_score"),
            "review_priority": priority,
        })

    rows.sort(key=lambda r: (
        0 if r["review_priority"] == "high" else 1 if r["review_priority"] == "medium" else 2,
        -(r["total_score"] or 0),
    ))
    return rows


# ── Diagnostics ───────────────────────────────────────────────────────────────

def _data_quality_warnings(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[str]:
    warnings = []
    for label, col in [
        ("low/no confidence", "confidence IN ('low', 'none')"),
        ("NULL momentum_score", "momentum_score IS NULL"),
        ("NULL sentiment_score", "sentiment_score IS NULL"),
        ("NULL quality_score", "quality_score IS NULL"),
        ("NULL cheap_score", "cheap_score IS NULL"),
    ]:
        row = conn.execute(f"SELECT COUNT(*) FROM scores WHERE run_id=? AND {col}", [run_id]).fetchone()
        if row and row[0]:
            warnings.append(f"{row[0]} candidates have {label}.")
    row = conn.execute("SELECT MIN(as_of_date), MAX(as_of_date) FROM scores WHERE run_id=?", [run_id]).fetchone()
    if row and row[0]:
        warnings.append(f"Score date range: {row[0]} to {row[1]}.")
    return warnings


def _scoring_diagnostics(conn: duckdb.DuckDBPyConnection, run_id: str) -> dict:
    total_row = conn.execute("SELECT COUNT(*) FROM scores WHERE run_id=?", [run_id]).fetchone()
    total = total_row[0] if total_row else 0
    if total == 0:
        return {"total": 0}

    def _count(where: str) -> int:
        r = conn.execute(f"SELECT COUNT(*) FROM scores WHERE run_id=? AND {where}", [run_id]).fetchone()
        return r[0] if r else 0

    null_cheap = _count("cheap_score IS NULL")
    null_quality = _count("quality_score IS NULL")
    null_catalyst = _count("catalyst_score IS NULL")
    null_momentum = _count("momentum_score IS NULL")
    null_sentiment = _count("sentiment_score IS NULL")
    low_conf = _count("confidence IN ('low', 'none')")
    incomplete = _count("tier='Incomplete'")
    n_reject = _count("tier='Reject'")
    n_c = _count("tier='C'")
    n_b = _count("tier='B'")
    n_a = _count("tier='A'")

    # Price source breakdown
    source_rows = conn.execute(
        "SELECT source, COUNT(DISTINCT ticker) FROM prices_daily "
        "WHERE ticker IN (SELECT DISTINCT ticker FROM scores WHERE run_id=?) "
        "GROUP BY source ORDER BY source",
        [run_id],
    ).fetchall()
    price_sources = {r[0] or "unknown": r[1] for r in source_rows}

    score_rows = conn.execute(
        "SELECT ROUND(total_score, 0) AS s, COUNT(*) FROM scores WHERE run_id=? GROUP BY s ORDER BY s",
        [run_id],
    ).fetchall()
    score_dist = {str(int(r[0])): r[1] for r in score_rows if r[0] is not None}

    return {
        "total_scored": total,
        "tier_a": n_a,
        "tier_b": n_b,
        "tier_c": n_c,
        "tier_incomplete": incomplete,
        "tier_reject": n_reject,
        "low_confidence_count": low_conf,
        "low_confidence_pct": round(low_conf / total * 100, 1),
        "null_rates": {
            "cheap": round(null_cheap / total * 100, 1),
            "quality": round(null_quality / total * 100, 1),
            "catalyst": round(null_catalyst / total * 100, 1),
            "momentum": round(null_momentum / total * 100, 1),
            "sentiment": round(null_sentiment / total * 100, 1),
        },
        "price_sources": price_sources,
        "score_distribution": score_dist,
    }


# ── Build ─────────────────────────────────────────────────────────────────────

def build_packet(conn: duckdb.DuckDBPyConnection, run_id: str | None = None) -> ReviewPacket:
    if run_id is None:
        run_id = _get_latest_run_id(conn)
        if run_id is None:
            raise ValueError("No scored runs found in the database.")

    row = conn.execute("SELECT MAX(as_of_date) FROM scores WHERE run_id=?", [run_id]).fetchone()
    run_date = str(row[0]) if row and row[0] else str(date.today())

    warnings: list[str] = []
    meta: dict = {"run_id": run_id, "run_date": run_date}

    row = conn.execute("SELECT COUNT(*), MIN(total_score), MAX(total_score) FROM scores WHERE run_id=?", [run_id]).fetchone()
    meta["total_scored"] = row[0] if row else 0
    meta["score_min"] = float(row[1]) if row and row[1] is not None else None
    meta["score_max"] = float(row[2]) if row and row[2] is not None else None

    for tier in ("A", "B", "C", "Incomplete", "Reject"):
        r = conn.execute("SELECT COUNT(*) FROM scores WHERE run_id=? AND tier=?", [run_id, tier]).fetchone()
        meta[f"tier_{tier.lower()}"] = r[0] if r else 0

    # Load all scores for cross-reference
    all_score_rows = _score_rows(conn, run_id, limit=10000)
    all_scores = {r["ticker"]: r for r in all_score_rows}

    sections: dict[str, list[dict]] = {
        "c_tier":                      _section_c_tier(conn, run_id),
        "top_reject":                   _section_top_reject(conn, run_id),
        "top_cheap":                    _section_top_cheap(conn, run_id),
        "top_quality":                  _section_top_quality(conn, run_id),
        "top_catalyst":                 _section_top_catalyst(conn, run_id),
        "cheap_quality_weak_catalyst":  _section_cheap_quality_weak_catalyst(conn, run_id),
    }

    if not sections["c_tier"]:
        warnings.append("No C-tier candidates in this run.")

    cross_ref = _build_cross_reference(sections, all_scores)
    meta["cross_reference_table"] = cross_ref
    meta["data_quality_warnings"] = _data_quality_warnings(conn, run_id)
    meta["scoring_diagnostics"] = _scoring_diagnostics(conn, run_id)

    return ReviewPacket(
        run_id=run_id,
        run_date=run_date,
        generated_at=datetime.utcnow().isoformat(),
        sections=sections,
        warnings=warnings,
        meta=meta,
    )


# ── Write ─────────────────────────────────────────────────────────────────────

def write_packet(packet: ReviewPacket, output_dir: str = "outputs",
                 stem_suffix: str = "") -> tuple[Path, Path]:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    suffix_part = f"_{stem_suffix}" if stem_suffix else ""
    stem = f"review_packet{suffix_part}_{packet.run_date}"
    md_path = Path(output_dir) / f"{stem}.md"
    json_path = Path(output_dir) / f"{stem}.json"

    md_path.write_text(_render_markdown(packet), encoding="utf-8")
    json_path.write_text(json.dumps(_to_json_dict(packet), indent=2, default=str), encoding="utf-8")

    return md_path, json_path


def _to_json_dict(packet: ReviewPacket) -> dict:
    return {
        "run_id": packet.run_id,
        "run_date": packet.run_date,
        "generated_at": packet.generated_at,
        "meta": packet.meta,
        "warnings": packet.warnings,
        "sections": packet.sections,
        "review_instructions": _REVIEW_INSTRUCTIONS_JSON,
    }


_REVIEW_INSTRUCTIONS_JSON = {
    "purpose": (
        "Fill in the 'review' block for each candidate you investigate. "
        "Import completed reviews with: python main.py review import <path>"
    ),
    "review_status_options": [
        "pending", "useful", "weak", "false_positive",
        "needs_more_evidence", "invalid_due_to_data_issue", "archived",
    ],
    "score_range": "1 (lowest) to 5 (highest), or null if not assessed",
    "false_positive_reason_options": [
        "bad_data", "stale_data", "cheap_for_good_reason", "weak_catalyst",
        "poor_quality_business", "macro_headwind", "llm_overstated_case",
        "missing_peer_context", "temporary_noise", "not_actionable",
        "overfit_score", "insufficient_liquidity", "missing_risk_factor",
        "source_failure", "other",
    ],
}


# ── Markdown rendering ────────────────────────────────────────────────────────

def _render_markdown(packet: ReviewPacket) -> str:
    m = packet.meta
    lines = [
        f"# MHDE Review Packet — {packet.run_date}",
        "",
        f"**Run ID:** `{packet.run_id}`  ",
        f"**Generated:** {packet.generated_at}  ",
        f"**Universe scored:** {m.get('total_scored', '?')} tickers  ",
        f"**Score range:** {m.get('score_min', '?')} – {m.get('score_max', '?')}",
        "",
        "## Tier Summary",
        "",
        "| Tier | Count |",
        "|------|-------|",
        f"| A          | {m.get('tier_a', 0)} |",
        f"| B          | {m.get('tier_b', 0)} |",
        f"| C          | {m.get('tier_c', 0)} |",
        f"| Incomplete | {m.get('tier_incomplete', 0)} |",
        f"| Reject     | {m.get('tier_reject', 0)} |",
        "",
    ]

    # Cross-reference summary table
    xref = m.get("cross_reference_table", [])
    if xref:
        high = [r for r in xref if r["review_priority"] == "high"]
        med = [r for r in xref if r["review_priority"] == "medium"]
        lines += [
            "## Review Priority Summary",
            "",
            f"**High priority:** {len(high)} | **Medium:** {len(med)} | **Low:** {len(xref)-len(high)-len(med)}",
            "",
            "| Ticker | Sections | Tier | Total | Cheap | Quality | Catalyst | Priority |",
            "|--------|----------|------|-------|-------|---------|----------|----------|",
        ]
        for r in xref:
            lines.append(
                f"| {r['ticker']} | {r['sections_appeared_in']} | {r['tier']} "
                f"| {r['total_score']} | {r['cheap_score']} | {r['quality_score']} "
                f"| {r['catalyst_score']} | **{r['review_priority']}** |"
            )
        lines.append("")

    if packet.warnings:
        lines += ["### Packet Warnings", ""]
        for w in packet.warnings:
            lines.append(f"- {w}")
        lines.append("")

    dqw = m.get("data_quality_warnings", [])
    if dqw:
        lines += ["### Data Quality Notes", ""]
        for w in dqw:
            lines.append(f"- {w}")
        lines.append("")

    diag = m.get("scoring_diagnostics", {})
    if diag:
        nr = diag.get("null_rates", {})
        ps = diag.get("price_sources", {})
        ps_str = ", ".join(f"{s}:{c}" for s, c in ps.items()) if ps else "none"
        lines += [
            "## Scoring Diagnostics",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total scored | {diag.get('total_scored', '?')} |",
            f"| A-tier | {diag.get('tier_a', 0)} |",
            f"| B-tier | {diag.get('tier_b', 0)} |",
            f"| C-tier | {diag.get('tier_c', 0)} |",
            f"| Incomplete | {diag.get('tier_incomplete', 0)} |",
            f"| Reject | {diag.get('tier_reject', 0)} |",
            f"| Low/no confidence | {diag.get('low_confidence_count', 0)} ({diag.get('low_confidence_pct', 0):.1f}%) |",
            f"| Price sources | {ps_str} |",
            "",
            "**Component null rates:**",
            "",
            f"| Component | Null rate |",
            f"|-----------|-----------|",
            f"| Valuation (cheap) | {nr.get('cheap', '?')}% |",
            f"| Quality | {nr.get('quality', '?')}% |",
            f"| Catalyst | {nr.get('catalyst', '?')}% |",
            f"| Momentum | {nr.get('momentum', '?')}% |",
            f"| Sentiment | {nr.get('sentiment', '?')}% |",
            "",
        ]
        dist = diag.get("score_distribution", {})
        if dist:
            lines += ["**Score distribution:**", ""]
            for score_val, cnt in sorted(dist.items(), key=lambda x: int(x[0])):
                bar = "█" * min(cnt, 40)
                lines.append(f"  {score_val:>3}: {bar} ({cnt})")
            lines.append("")

    lines += [
        "---",
        "",
        "## Review Instructions",
        "",
        "For each candidate, fill in the `Review:` block. Import with: `python main.py review import <json_path>`",
        "",
        "**review_status:** `pending` | `useful` | `weak` | `false_positive` | `needs_more_evidence` | `invalid_due_to_data_issue` | `archived`",
        "",
        "**false_positive_reason:** `bad_data` | `stale_data` | `cheap_for_good_reason` | `weak_catalyst` | `poor_quality_business` | `macro_headwind` | `overfit_score` | `source_failure` | `other`",
        "",
        "---",
        "",
    ]

    # Compute multi-section tickers for markdown annotation
    ticker_sections: dict[str, list[str]] = defaultdict(list)
    for key, candidates in packet.sections.items():
        label = _SECTION_LABELS.get(key, key)
        for c in candidates:
            t = c.get("ticker")
            if t and label not in ticker_sections[t]:
                ticker_sections[t].append(label)
    multi_section = {t: sections for t, sections in ticker_sections.items() if len(sections) > 1}

    section_defs = [
        ("c_tier",                     "C-Tier Candidates",
         "All candidates that met the C-tier threshold (total ≥ 45, observed ≥ 2 components)."),
        ("top_reject",                  "Top 10 Rejects by Score",
         "Highest-scoring candidates that fell below C-tier — useful for threshold calibration."),
        ("top_cheap",                   "Top 10 by Valuation (Cheap) Score",
         "Best valuation scores. Low P/S, P/E, or P/B relative to price."),
        ("top_quality",                 "Top 10 by Quality Score",
         "Strongest fundamental quality: revenue growth, net income, low dilution."),
        ("top_catalyst",                "Top 10 by Catalyst Score",
         "Strongest near-term catalyst signals: earnings, filings, short interest change."),
        ("cheap_quality_weak_catalyst", "Top 10: Strong Cheap+Quality, Weak Catalyst",
         "Good fundamental profile but no current catalyst. Watch list material."),
    ]

    for key, title, note in section_defs:
        _render_section(lines, title, packet.sections.get(key, []),
                        note=note, multi_section=multi_section)

    return "\n".join(lines)


def _render_section(lines: list, title: str, candidates: list[dict],
                    note: str = "", multi_section: dict | None = None) -> None:
    lines += [f"## {title}", ""]
    if note:
        lines += [f"*{note}*", ""]
    if not candidates:
        lines += ["*No candidates in this section.*", "", "---", ""]
        return
    for c in candidates:
        ticker = c["ticker"]
        also_in = []
        if multi_section and ticker in multi_section:
            # List sections other than the current one based on title
            also_in = [s for s in multi_section[ticker] if s != _SECTION_LABELS.get(
                next((k for k, t, _ in [
                    ("c_tier", "C-Tier Candidates", ""),
                    ("top_reject", "Top 10 Rejects by Score", ""),
                    ("top_cheap", "Top 10 by Valuation (Cheap) Score", ""),
                    ("top_quality", "Top 10 by Quality Score", ""),
                    ("top_catalyst", "Top 10 by Catalyst Score", ""),
                    ("cheap_quality_weak_catalyst", "Top 10: Strong Cheap+Quality, Weak Catalyst", ""),
                ] if t == title), None), "")]

        heading = f"### {ticker} — {c.get('company_name') or 'Unknown'}"
        if also_in:
            heading += f"  *(also in: {', '.join(also_in)})*"
        lines.append(heading)
        lines.append("")

        # Score table
        vm = c.get("valuation_metrics", {})
        lines += [
            "| Field | Value |",
            "|-------|-------|",
            f"| Tier  | {c['tier']} |",
            f"| Total score | {c['total_score']} |",
            f"| Cheap | {c['cheap_score']} |",
            f"| Quality | {c['quality_score']} |",
            f"| Catalyst | {c['catalyst_score']} |",
            f"| Momentum | {c['momentum_score']} |",
            f"| Sentiment | {c['sentiment_score']} |",
            f"| Risk penalty | {c['risk_penalty']} |",
            f"| Confidence | {c.get('confidence') or '—'} |",
            "",
        ]

        # Valuation metrics
        if vm and any(v is not None for v in vm.values()):
            lines += [
                "**Valuation metrics:**",
                "",
                "| Metric | Value |",
                "|--------|-------|",
                f"| Price | {'$' + str(vm['price']) if vm.get('price') else '—'} ({vm.get('price_date') or '?'}) |",
                f"| Market cap | {'$' + str(vm['market_cap_b']) + 'B' if vm.get('market_cap_b') else '—'} |",
                f"| P/S | {vm.get('ps_ratio') or '—'} |",
                f"| P/E | {vm.get('pe_ratio') or '—'} |",
                f"| P/B | {vm.get('pb_ratio') or '—'} |",
                "",
            ]

        if c.get("why_ranked"):
            lines += [f"**Why ranked:** {c['why_ranked']}", ""]
        if c.get("why_rejected"):
            lines += [f"**Why rejected:** {c['why_rejected']}", ""]
        if c.get("thesis"):
            lines += [f"**Thesis:** {c['thesis']}", ""]
        if c.get("why_now"):
            lines += [f"**Why now:** {c['why_now']}", ""]
        if c.get("risks"):
            risks_str = "; ".join(c["risks"]) if isinstance(c["risks"], list) else str(c["risks"])
            lines += [f"**Risks:** {risks_str}", ""]
        if c.get("missing_data"):
            md = c["missing_data"]
            if md and ((isinstance(md, list) and md) or (isinstance(md, dict) and md)):
                lines += [f"**Missing data:** {md}", ""]

        guard_hits = c.get("guard_hits") or []
        if guard_hits:
            lines += ["**Data quality guards triggered:**", ""]
            for h in guard_hits:
                lines.append(f"- {h}")
            lines.append("")

        cat_evidence = [e for e in (c.get("catalyst_evidence") or []) if isinstance(e, dict)]
        if cat_evidence:
            lines += ["**Catalyst evidence:**", ""]
            for e in cat_evidence:
                lines.append(f"- {e.get('detail', '?')} ({e.get('date', '?')})")
            lines.append("")

        lines += [
            "**Review:**",
            "```",
            "review_status: pending",
            "usefulness_score: null",
            "thesis_quality_score: null",
            "evidence_quality_score: null",
            "false_positive_reason: null",
            "missed_risk: null",
            "missing_evidence: null",
            "review_notes: null",
            "```",
            "",
        ]
    lines += ["---", ""]
