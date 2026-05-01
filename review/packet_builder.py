"""Review packet builder — generates structured candidate review packages."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
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

_NEAR_B_THRESHOLD = 55.0
_HIGH_CATALYST_THRESHOLD = 60.0
_HIGH_CHEAP_QUALITY_THRESHOLD = 60.0
_STRONG_COMPONENT_THRESHOLD = 60.0

_NEAR_B_FALLBACK = 5
_HIGH_CATALYST_FALLBACK = 5
_CHEAP_QUALITY_FALLBACK = 5
_REJECTED_FALLBACK = 5


@dataclass
class ReviewPacket:
    run_id: str
    run_date: str
    generated_at: str
    sections: dict[str, list[dict]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


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


def _parse_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _section_top10(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    rows = _score_rows(conn, run_id, order="total_score DESC", limit=10)
    return _enrich_with_hypothesis(conn, run_id, rows)


def _section_near_b(conn: duckdb.DuckDBPyConnection, run_id: str) -> tuple[list[dict], bool]:
    rows = _score_rows(conn, run_id,
                       where=f"s.tier='C' AND s.total_score>={_NEAR_B_THRESHOLD}",
                       order="total_score DESC", limit=20)
    fallback = False
    if not rows:
        rows = _score_rows(conn, run_id, where="s.tier='C'",
                           order="total_score DESC", limit=_NEAR_B_FALLBACK)
        fallback = True
    return _enrich_with_hypothesis(conn, run_id, rows), fallback


def _section_high_catalyst(conn: duckdb.DuckDBPyConnection, run_id: str) -> tuple[list[dict], bool]:
    rows = _score_rows(conn, run_id,
                       where=f"s.catalyst_score>={_HIGH_CATALYST_THRESHOLD}",
                       order="catalyst_score DESC", limit=20)
    fallback = False
    if not rows:
        rows = _score_rows(conn, run_id, order="catalyst_score DESC",
                           limit=_HIGH_CATALYST_FALLBACK)
        fallback = True
    return _enrich_with_hypothesis(conn, run_id, rows), fallback


def _section_cheap_quality_no_catalyst(conn: duckdb.DuckDBPyConnection, run_id: str) -> tuple[list[dict], bool]:
    rows = _score_rows(
        conn, run_id,
        where=f"s.cheap_score>={_HIGH_CHEAP_QUALITY_THRESHOLD} AND s.quality_score>={_HIGH_CHEAP_QUALITY_THRESHOLD} AND s.catalyst_score<50",
        order="(s.cheap_score + s.quality_score) DESC", limit=20,
    )
    fallback = False
    if not rows:
        rows = _score_rows(conn, run_id,
                           where="s.cheap_score IS NOT NULL AND s.quality_score IS NOT NULL",
                           order="(s.cheap_score + s.quality_score) DESC",
                           limit=_CHEAP_QUALITY_FALLBACK)
        fallback = True
    return _enrich_with_hypothesis(conn, run_id, rows), fallback


def _section_rejected_worth_inspecting(conn: duckdb.DuckDBPyConnection, run_id: str) -> tuple[list[dict], bool]:
    t = _STRONG_COMPONENT_THRESHOLD
    rows = _score_rows(
        conn, run_id,
        where=f"s.tier='Reject' AND (s.cheap_score>={t} OR s.quality_score>={t} OR s.catalyst_score>={t})",
        order="total_score DESC", limit=20,
    )
    fallback = False
    if not rows:
        rows = _score_rows(conn, run_id, where="s.tier='Reject'",
                           order="total_score DESC", limit=_REJECTED_FALLBACK)
        fallback = True
    return _enrich_with_hypothesis(conn, run_id, rows), fallback


def _data_quality_warnings(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[str]:
    warnings = []
    row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND confidence='low'", [run_id]
    ).fetchone()
    if row and row[0]:
        warnings.append(f"{row[0]} candidates have confidence=low (data immature or missing).")
    row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND momentum_score IS NULL", [run_id]
    ).fetchone()
    if row and row[0]:
        warnings.append(f"{row[0]} candidates have NULL momentum_score (insufficient price history).")
    row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND sentiment_score IS NULL", [run_id]
    ).fetchone()
    if row and row[0]:
        warnings.append(f"{row[0]} candidates have NULL sentiment_score (no short interest data).")
    row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND quality_score IS NULL", [run_id]
    ).fetchone()
    if row and row[0]:
        warnings.append(f"{row[0]} candidates have NULL quality_score (no fundamentals).")
    # Check run date
    row = conn.execute(
        "SELECT MIN(as_of_date), MAX(as_of_date) FROM scores WHERE run_id=?", [run_id]
    ).fetchone()
    if row and row[0]:
        warnings.append(f"Score date range: {row[0]} to {row[1]}.")
    return warnings


def build_packet(conn: duckdb.DuckDBPyConnection, run_id: str | None = None) -> ReviewPacket:
    if run_id is None:
        run_id = _get_latest_run_id(conn)
        if run_id is None:
            raise ValueError("No scored runs found in the database.")

    row = conn.execute(
        "SELECT MAX(as_of_date) FROM scores WHERE run_id=?", [run_id]
    ).fetchone()
    run_date = str(row[0]) if row and row[0] else str(date.today())

    warnings: list[str] = []
    sections: dict[str, list[dict]] = {}
    meta: dict = {"run_id": run_id, "run_date": run_date}

    # Count totals
    row = conn.execute("SELECT COUNT(*), MIN(total_score), MAX(total_score) FROM scores WHERE run_id=?", [run_id]).fetchone()
    meta["total_scored"] = row[0] if row else 0
    meta["score_min"] = float(row[1]) if row and row[1] is not None else None
    meta["score_max"] = float(row[2]) if row and row[2] is not None else None

    for tier in ("A", "B", "C", "Reject"):
        r = conn.execute("SELECT COUNT(*) FROM scores WHERE run_id=? AND tier=?", [run_id, tier]).fetchone()
        meta[f"tier_{tier.lower()}"] = r[0] if r else 0

    sections["top_10"] = _section_top10(conn, run_id)

    near_b, near_b_fallback = _section_near_b(conn, run_id)
    sections["near_b"] = near_b
    if near_b_fallback:
        warnings.append(f"No C-tier candidates with total_score≥{_NEAR_B_THRESHOLD}. Showing top-{_NEAR_B_FALLBACK} C-tier by score.")

    high_cat, high_cat_fallback = _section_high_catalyst(conn, run_id)
    sections["high_catalyst"] = high_cat
    if high_cat_fallback:
        warnings.append(f"No candidates with catalyst_score≥{_HIGH_CATALYST_THRESHOLD}. Showing top-{_HIGH_CATALYST_FALLBACK} by catalyst score.")

    cheap_qual, cheap_qual_fallback = _section_cheap_quality_no_catalyst(conn, run_id)
    sections["cheap_quality_no_catalyst"] = cheap_qual
    if cheap_qual_fallback:
        warnings.append(f"No candidates with cheap≥{_HIGH_CHEAP_QUALITY_THRESHOLD} AND quality≥{_HIGH_CHEAP_QUALITY_THRESHOLD} AND catalyst<50. Showing closest matches.")

    rejected, rejected_fallback = _section_rejected_worth_inspecting(conn, run_id)
    sections["rejected_worth_inspecting"] = rejected
    if rejected_fallback:
        warnings.append(f"No rejected candidates with strong component scores. Showing top-{_REJECTED_FALLBACK} rejected by total_score.")

    meta["data_quality_warnings"] = _data_quality_warnings(conn, run_id)

    return ReviewPacket(
        run_id=run_id,
        run_date=run_date,
        generated_at=datetime.utcnow().isoformat(),
        sections=sections,
        warnings=warnings,
        meta=meta,
    )


def write_packet(packet: ReviewPacket, output_dir: str = "outputs") -> tuple[Path, Path]:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    stem = f"review_packet_{packet.run_date}"
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
        "## Executive Summary",
        "",
        f"| Tier | Count |",
        f"|------|-------|",
        f"| A    | {m.get('tier_a', 0)} |",
        f"| B    | {m.get('tier_b', 0)} |",
        f"| C    | {m.get('tier_c', 0)} |",
        f"| Reject | {m.get('tier_reject', 0)} |",
        "",
    ]

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

    lines += [
        "---",
        "",
        "## Review Instructions",
        "",
        "For each candidate you investigate, fill in the `Review:` block below.",
        "",
        "**review_status options:** `pending` | `useful` | `weak` | `false_positive` |",
        "  `needs_more_evidence` | `invalid_due_to_data_issue` | `archived`",
        "",
        "**score fields** (1–5, nullable): `usefulness_score` · `thesis_quality_score` · `evidence_quality_score`",
        "",
        "**false_positive_reason options:** `bad_data` | `stale_data` | `cheap_for_good_reason` |",
        "  `weak_catalyst` | `poor_quality_business` | `macro_headwind` | `llm_overstated_case` |",
        "  `missing_peer_context` | `temporary_noise` | `not_actionable` | `overfit_score` |",
        "  `insufficient_liquidity` | `missing_risk_factor` | `source_failure` | `other`",
        "",
        "Import reviews with: `python main.py review import outputs/review_packet_YYYY-MM-DD.json`",
        "",
        "---",
        "",
    ]

    _render_section(lines, "Top 10 Ranked Candidates", packet.sections.get("top_10", []))
    _render_section(lines, "Near-B Candidates", packet.sections.get("near_b", []),
                    note="C-tier candidates closest to B-tier threshold.")
    _render_section(lines, "High Catalyst-Score Candidates", packet.sections.get("high_catalyst", []),
                    note="Strongest near-term catalyst evidence.")
    _render_section(lines, "High Cheap + Quality, Weak Catalyst", packet.sections.get("cheap_quality_no_catalyst", []),
                    note="Good fundamental profile but no current catalyst — watch list material.")
    _render_section(lines, "Rejected Candidates Worth Inspecting", packet.sections.get("rejected_worth_inspecting", []),
                    note="Rejected overall but showed strength in at least one component.")

    return "\n".join(lines)


def _render_section(lines: list, title: str, candidates: list[dict], note: str = "") -> None:
    lines += [f"## {title}", ""]
    if note:
        lines += [f"*{note}*", ""]
    if not candidates:
        lines += ["*No candidates in this section.*", "", "---", ""]
        return
    for c in candidates:
        lines += [
            f"### {c['ticker']} — {c.get('company_name') or 'Unknown'}",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
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
            if md and (isinstance(md, list) and md) or (isinstance(md, dict) and md):
                lines += [f"**Missing data:** {md}", ""]
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
