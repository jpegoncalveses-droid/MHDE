from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import duckdb

logger = logging.getLogger("mhde.reports")


def write_json_report(
    run_id: str,
    conn: duckdb.DuckDBPyConnection,
    output_path: str | Path,
    run_summary: dict | None = None,
) -> Path:
    today = date.today().isoformat()
    output_dir = Path(output_path)
    if output_dir.suffix == "":
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"daily_radar_{today}.json"
    else:
        path = output_dir
        path.parent.mkdir(parents=True, exist_ok=True)

    scores = conn.execute(
        "SELECT * FROM scores WHERE run_id = ? ORDER BY total_score DESC",
        [run_id],
    ).fetchall()
    score_cols = [d[0] for d in conn.description]

    hyps = conn.execute(
        "SELECT * FROM hypotheses WHERE run_id = ? ORDER BY rank",
        [run_id],
    ).fetchall()
    hyp_cols = [d[0] for d in conn.description]

    report = {
        "run_id": run_id,
        "generated_at": datetime.utcnow().isoformat(),
        "as_of_date": date.today().isoformat(),
        "summary": run_summary or {},
        "scores": [dict(zip(score_cols, r)) for r in scores],
        "hypotheses": [dict(zip(hyp_cols, r)) for r in hyps],
        "disclaimer": (
            "Research candidates only. Not investment advice. "
            "Experimental — not validated for decision use."
        ),
    }

    path.write_text(json.dumps(report, indent=2, default=str))
    logger.info("JSON report written: %s", path)
    return path
