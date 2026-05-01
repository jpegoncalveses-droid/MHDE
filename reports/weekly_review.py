from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

logger = logging.getLogger("mhde.reports")


def write_weekly_review(
    conn: duckdb.DuckDBPyConnection,
    output_path: str | Path,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today()
    week_ago = today - timedelta(days=7)

    # Score distribution over past week
    tier_dist = conn.execute(
        """
        SELECT tier, COUNT(*) as count, AVG(total_score) as avg_score
        FROM scores
        WHERE created_at >= ?
        GROUP BY tier ORDER BY avg_score DESC
        """,
        [week_ago],
    ).fetchall()

    # Hypothesis status changes
    hyp_summary = conn.execute(
        """
        SELECT status, COUNT(*) FROM hypotheses
        WHERE created_at >= ?
        GROUP BY status
        """,
        [week_ago],
    ).fetchall()

    # Alerts sent
    alert_count = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE created_at >= ?",
        [week_ago],
    ).fetchone()

    lines = [
        f"# MHDE Weekly Review — {today.isoformat()}",
        f"\n**Period:** {week_ago.isoformat()} to {today.isoformat()}",
        f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "---",
        "",
        "## Score Distribution",
        "",
    ]

    if tier_dist:
        lines += ["| Tier | Count | Avg Score |", "|------|-------|-----------|"]
        for t, c, avg in tier_dist:
            lines += [f"| {t} | {c} | {avg:.1f} |"]
    else:
        lines += ["No score data for this period. Run 'score' to populate."]

    lines += ["", "## Hypothesis Status", ""]
    if hyp_summary:
        for status, count in hyp_summary:
            lines += [f"- **{status}**: {count}"]
    else:
        lines += ["No hypotheses this period."]

    lines += [
        "",
        "## Alerts",
        f"- Alerts sent this week: {alert_count[0] if alert_count else 0}",
        "",
        "## Paper Trade Performance",
        "",
        "Paper trade outcomes require manual exit price entry. See `paper_trades` table.",
        "",
        "---",
        "> Research purposes only. Not investment advice.",
    ]

    path.write_text("\n".join(lines))
    logger.info("Weekly review written: %s", path)
    return path
