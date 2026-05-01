from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import duckdb

logger = logging.getLogger("mhde.reports")

_DISCLAIMER = (
    "\n---\n"
    "> **Disclaimer:** This is a research candidate, not a buy/sell recommendation. "
    "MHDE outputs are experimental and have not been validated for investment decision use. "
    "Always conduct your own research.\n"
)


def write_daily_report(
    run_id: str,
    conn: duckdb.DuckDBPyConnection,
    output_path: str | Path,
    run_summary: dict | None = None,
) -> Path:
    today = date.today().isoformat()
    output_dir = Path(output_path)
    if output_dir.suffix == "":
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"daily_radar_{today}.md"
    else:
        path = output_dir
        path.parent.mkdir(parents=True, exist_ok=True)
    run_summary = run_summary or {}

    # Fetch data
    scores = conn.execute(
        "SELECT ticker, tier, total_score, why_ranked, why_rejected FROM scores WHERE run_id = ? ORDER BY total_score DESC",
        [run_id],
    ).fetchall()

    hyps = conn.execute(
        "SELECT ticker, tier, total_score, thesis, why_now FROM hypotheses WHERE run_id = ? ORDER BY rank",
        [run_id],
    ).fetchall()

    rejects = conn.execute(
        "SELECT ticker, reason FROM rejections WHERE run_id = ? LIMIT 20",
        [run_id],
    ).fetchall()

    src_runs = conn.execute(
        "SELECT source_name, status, records_inserted, error_message FROM source_runs WHERE run_id = ?",
        [run_id],
    ).fetchall()

    health = conn.execute(
        "SELECT check_name, status, message FROM health_checks WHERE run_id = ? ORDER BY created_at",
        [run_id],
    ).fetchall()

    tier_a = [h for h in hyps if h[1] == "A"]
    tier_b = [h for h in hyps if h[1] == "B"]
    tier_c = [h for h in hyps if h[1] == "C"]

    lines = [
        f"# MHDE Daily Radar — {today}",
        f"\n**Run ID:** `{run_id}`  ",
        f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        f"- Universe size: {run_summary.get('universe_size', 'N/A')}",
        f"- Tickers scored: {len(scores)}",
        f"- Candidates (A/B/C): {len(tier_a)} / {len(tier_b)} / {len(tier_c)}",
        f"- Rejected: {len(rejects)}",
        f"- Sources succeeded: {run_summary.get('sources_succeeded', 'N/A')}",
        f"- Alerts sent: {run_summary.get('alerts_sent', 0)}",
        "",
    ]

    if tier_a:
        lines += ["## A-Tier Candidates", ""]
        for h in tier_a:
            lines += [
                f"### {h[0]} (Score: {h[2]:.0f})",
                f"**Thesis:** {h[3]}",
                f"**Why now:** {h[4]}",
                "",
            ]

    if tier_b:
        lines += ["## B-Tier Candidates", ""]
        for h in tier_b:
            lines += [f"- **{h[0]}** (Score: {h[2]:.0f}): {(h[3] or '')[:120]}"]
        lines.append("")

    if tier_c:
        lines += ["## C-Tier Candidates", ""]
        for h in tier_c:
            lines += [f"- **{h[0]}** (Score: {h[2]:.0f})"]
        lines.append("")

    if rejects:
        lines += ["## Rejected Candidates", ""]
        for r in rejects[:15]:
            lines += [f"- **{r[0]}**: {r[1] or 'Below threshold'}"]
        lines.append("")

    lines += ["## Source Status", ""]
    for sr in src_runs:
        status_icon = "✓" if sr[1] in ("ok", "experimental") else "✗" if sr[1] == "error" else "–"
        lines += [f"- {status_icon} **{sr[0]}**: {sr[1]} ({sr[2] or 0} records)"]
    lines.append("")

    if health:
        lines += ["## Health Checks", ""]
        for hc in health:
            icon = "✓" if hc[1] == "pass" else "⚠" if hc[1] == "warn" else "✗" if hc[1] == "fail" else "–"
            lines += [f"- {icon} {hc[0]}: {hc[2] or ''}"]
        lines.append("")

    lines += [
        "## Known Limitations",
        "",
        "- Universe selection is name-filtered only (no market cap ranking)",
        "- Feature data may be sparse on first run",
        "- LLM briefs may be in mock mode if no API key is configured",
        "- Backtest validation requires historical data accumulation",
        "- Scores are experimental and have not been validated over time",
        "",
    ]

    lines.append(_DISCLAIMER)

    path.write_text("\n".join(lines))
    logger.info("Daily report written: %s", path)
    return path
