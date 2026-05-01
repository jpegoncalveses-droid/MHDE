from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

logger = logging.getLogger("mhde.pipelines.weekly_review")


def run(cfg: dict, conn: duckdb.DuckDBPyConnection) -> None:
    cutoff = date.today() - timedelta(days=7)
    logger.info("Generating weekly review (last 7 days since %s)", cutoff)

    runs = conn.execute(
        """
        SELECT run_id, COUNT(*) as candidates, AVG(total_score) as avg_score
        FROM scores
        WHERE created_at >= ?
        GROUP BY run_id
        ORDER BY MIN(created_at) DESC
        """,
        [cutoff],
    ).fetchall()

    tier_dist = conn.execute(
        """
        SELECT tier, COUNT(*) FROM scores
        WHERE created_at >= ?
        GROUP BY tier ORDER BY tier
        """,
        [cutoff],
    ).fetchall()

    alerts = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE created_at >= ?",
        [cutoff],
    ).fetchone()

    hyp_changes = conn.execute(
        """
        SELECT status, COUNT(*) FROM hypotheses
        WHERE updated_at >= ?
        GROUP BY status
        """,
        [cutoff],
    ).fetchall()

    from reports.weekly_review import write_weekly_review
    report = {
        "cutoff": cutoff,
        "runs": [{"run_id": r[0], "candidates": r[1], "avg_score": r[2]} for r in runs],
        "tier_distribution": dict(tier_dist),
        "alerts_sent": alerts[0] if alerts else 0,
        "hypothesis_status": dict(hyp_changes),
    }
    write_weekly_review(conn, "outputs")

    print(f"\nMHDE Weekly Review (since {cutoff})")
    print(f"  Pipeline runs:     {len(runs)}")
    print(f"  Alerts sent:       {report['alerts_sent']}")
    if tier_dist:
        print("  Tier distribution:")
        for tier, count in sorted(tier_dist):
            print(f"    {tier}: {count}")
