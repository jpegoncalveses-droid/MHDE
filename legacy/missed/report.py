"""Missed-opportunity report generator."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import duckdb

logger = logging.getLogger("mhde.missed.report")


def generate_report(
    conn: duckdb.DuckDBPyConnection,
    output_dir: str = "outputs",
) -> tuple[Path, Path]:
    """
    Generate markdown + JSON report from investigated missed opportunities.
    Returns (md_path, json_path).
    """
    today = date.today().isoformat()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"missed_opportunities_{today}.md"
    json_path = out / f"missed_opportunities_{today}.json"

    events = _load_events(conn)
    investigations = _load_investigations(conn)
    root_cause_breakdown = _root_cause_breakdown(investigations)
    truly_unpredictable = sum(
        1 for inv in investigations
        if inv.get("primary_root_cause") == "truly_unpredictable"
    )

    # JSON output
    data = {
        "generated_at": today,
        "missed_events": len(events),
        "investigated": len(investigations),
        "truly_unpredictable": truly_unpredictable,
        "root_cause_breakdown": root_cause_breakdown,
        "top_events": events[:20],
    }
    json_path.write_text(json.dumps(data, indent=2, default=str))

    # Markdown output
    lines = [
        f"# MHDE Missed Opportunities Report — {today}",
        "",
        f"**Total missed events detected:** {len(events)}",
        f"**Investigated:** {len(investigations)}",
        f"**Truly unpredictable:** {truly_unpredictable}",
        "",
        "## Root cause breakdown",
        "",
    ]
    for cause, count in sorted(root_cause_breakdown.items(), key=lambda x: -x[1]):
        lines.append(f"- `{cause}`: {count}")
    lines.append("")

    if events:
        lines += ["## Biggest missed moves", ""]
        for e in events[:10]:
            lines.append(
                f"- **{e.get('ticker')}** +{e.get('return_value', 0):.1f}% "
                f"({e.get('event_type')}) on {e.get('event_date')} "
                f"— tier_before={e.get('tier_before_event') or 'N/A'}"
            )
        lines.append("")

    md_path.write_text("\n".join(lines))
    logger.info("Missed opportunities report written: %s", md_path)
    return md_path, json_path


def _load_events(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """SELECT event_id, ticker, event_date, event_type, return_value, window_days,
                  tier_before_event, was_in_universe, was_scored, had_catalyst_evidence
           FROM missed_opportunity_events
           ORDER BY return_value DESC LIMIT 100"""
    ).fetchall()
    cols = ["event_id", "ticker", "event_date", "event_type", "return_value",
            "window_days", "tier_before_event", "was_in_universe",
            "was_scored", "had_catalyst_evidence"]
    return [dict(zip(cols, r)) for r in rows]


def _load_investigations(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """SELECT investigation_id, ticker, event_date, primary_root_cause,
                  root_causes_json, text_enrichment_needed
           FROM missed_opportunity_investigations
           ORDER BY created_at DESC LIMIT 200"""
    ).fetchall()
    cols = ["investigation_id", "ticker", "event_date", "primary_root_cause",
            "root_causes_json", "text_enrichment_needed"]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["root_causes"] = json.loads(d.get("root_causes_json") or "[]")
        except Exception:
            d["root_causes"] = []
        result.append(d)
    return result


def _root_cause_breakdown(investigations: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for inv in investigations:
        rc = inv.get("primary_root_cause") or "other"
        counts[rc] = counts.get(rc, 0) + 1
    return counts
