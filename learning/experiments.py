from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import duckdb

from learning.error_taxonomy import EXPERIMENT_STATUSES

_DECISION_LOG = Path("docs/decision_log.md")


def _now() -> datetime:
    return datetime.utcnow()


def propose_experiment(
    conn: duckdb.DuckDBPyConnection,
    hypothesis: str,
    proposed_change: dict,
    affected_components: list[str],
    expected_effect: str,
    based_on_run_ids: list[str] | None = None,
) -> str:
    experiment_id = uuid.uuid4().hex[:16]
    now = _now()
    conn.execute(
        """
        INSERT INTO scorecard_experiments (
            experiment_id, based_on_run_ids, hypothesis,
            proposed_change_json, affected_components_json,
            expected_effect, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
        """,
        [
            experiment_id,
            json.dumps(based_on_run_ids or []),
            hypothesis,
            json.dumps(proposed_change),
            json.dumps(affected_components),
            expected_effect,
            now, now,
        ],
    )
    return experiment_id


def mark_tested(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    backtest_result: dict,
    backtest_notes: str | None = None,
) -> None:
    """Record test/backtest results and advance status to 'tested'."""
    conn.execute(
        """
        UPDATE scorecard_experiments
        SET status = 'tested',
            backtest_result_json = ?,
            backtest_notes = ?,
            updated_at = ?
        WHERE experiment_id = ?
          AND status IN ('proposed', 'tested')
        """,
        [json.dumps(backtest_result), backtest_notes, _now(), experiment_id],
    )


def approve_experiment(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    approved_by: str,
    review_notes: str | None = None,
) -> None:
    """
    Mark experiment as approved. Does NOT set applied_at.
    Approval means a human considers the experiment valid and may apply it later.
    Application is a separate step via apply_experiment().
    """
    conn.execute(
        """
        UPDATE scorecard_experiments
        SET status = 'approved',
            approved_by = ?,
            review_notes = ?,
            updated_at = ?
        WHERE experiment_id = ?
        """,
        [approved_by, review_notes, _now(), experiment_id],
    )


def apply_experiment(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    applied_by: str,
    notes: str | None = None,
) -> None:
    """
    Apply an approved experiment to production. This is the ONLY function that sets applied_at.

    Requires:
    - experiment must have status = 'approved'
    - applied_by must be provided (audit trail)

    Writes a governance log entry to docs/decision_log.md.
    Does NOT automatically mutate any scoring config, feature rule, or model weight —
    the caller is responsible for making the actual code/config change.
    """
    row = conn.execute(
        "SELECT status, hypothesis, proposed_change_json, affected_components_json, approved_by "
        "FROM scorecard_experiments WHERE experiment_id = ?",
        [experiment_id],
    ).fetchone()

    if row is None:
        raise ValueError(f"Experiment not found: {experiment_id}")

    status, hypothesis, change_json, components_json, approved_by = row
    if status != "approved":
        raise ValueError(
            f"Cannot apply experiment {experiment_id}: status is '{status}', must be 'approved'. "
            "Call approve_experiment() first."
        )

    now = _now()
    conn.execute(
        """
        UPDATE scorecard_experiments
        SET status = 'applied',
            applied_by = ?,
            applied_at = ?,
            review_notes = COALESCE(CASE WHEN ? IS NOT NULL THEN ? END, review_notes),
            updated_at = ?
        WHERE experiment_id = ?
        """,
        [applied_by, now, notes, notes, now, experiment_id],
    )

    _append_decision_log(
        experiment_id=experiment_id,
        hypothesis=hypothesis,
        change_json=change_json,
        components_json=components_json,
        approved_by=approved_by,
        applied_by=applied_by,
        applied_at=now,
        notes=notes,
    )


def reject_experiment(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    review_notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE scorecard_experiments
        SET status = 'rejected', review_notes = ?, updated_at = ?
        WHERE experiment_id = ?
        """,
        [review_notes, _now(), experiment_id],
    )


def archive_experiment(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    review_notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE scorecard_experiments
        SET status = 'archived', review_notes = ?, updated_at = ?
        WHERE experiment_id = ?
        """,
        [review_notes, _now(), experiment_id],
    )


def get_experiments(conn: duckdb.DuckDBPyConnection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT experiment_id, based_on_run_ids, hypothesis, proposed_change_json,
               affected_components_json, expected_effect, backtest_result_json,
               backtest_notes, status, review_notes, approved_by, applied_by,
               applied_at, created_at, updated_at
        FROM scorecard_experiments
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "experiment_id", "based_on_run_ids", "hypothesis", "proposed_change_json",
        "affected_components_json", "expected_effect", "backtest_result_json",
        "backtest_notes", "status", "review_notes", "approved_by", "applied_by",
        "applied_at", "created_at", "updated_at",
    ]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        for field in ("based_on_run_ids", "proposed_change_json", "affected_components_json", "backtest_result_json"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        result.append(d)
    return result


def _append_decision_log(
    experiment_id: str,
    hypothesis: str,
    change_json: str | None,
    components_json: str | None,
    approved_by: str | None,
    applied_by: str,
    applied_at: datetime,
    notes: str | None,
) -> None:
    try:
        change = json.loads(change_json) if change_json else {}
        components = json.loads(components_json) if components_json else []
    except Exception:
        change, components = {}, []

    entry = [
        f"\n## {applied_at.strftime('%Y-%m-%d')} — Experiment applied: {experiment_id}",
        f"\n**Hypothesis:** {hypothesis}",
        f"**Approved by:** {approved_by or 'unknown'}",
        f"**Applied by:** {applied_by}",
        f"**Applied at:** {applied_at.isoformat()}",
        f"**Affected components:** {', '.join(components) if components else 'unknown'}",
        f"**Proposed change:** `{json.dumps(change)}`",
    ]
    if notes:
        entry.append(f"**Notes:** {notes}")
    entry.append("")

    if _DECISION_LOG.exists():
        existing = _DECISION_LOG.read_text()
        _DECISION_LOG.write_text(existing + "\n" + "\n".join(entry))
    else:
        _DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
        _DECISION_LOG.write_text("\n".join(entry))
