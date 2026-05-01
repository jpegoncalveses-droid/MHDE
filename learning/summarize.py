from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import duckdb

from learning.calibration import (
    false_positive_reasons,
    feature_coverage_vs_confidence,
    llm_confidence_vs_human_review,
    outcome_by_review_status,
    outcome_by_score_bucket,
    outcome_by_tier,
    score_component_vs_outcome,
    source_failure_impact,
)
from learning.experiments import get_experiments
from learning.insights import generate_insights

logger = logging.getLogger("mhde.learning.summarize")

_INSUFFICIENT_MSG = "Insufficient outcome/review history for reliable calibration."


def write_learning_report(
    conn: duckdb.DuckDBPyConnection,
    output_path: str | Path,
) -> Path:
    today = date.today()
    output_dir = Path(output_path)
    if output_dir.suffix == "":
        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / f"learning_report_{today.isoformat()}.md"
        json_path = output_dir / f"learning_report_{today.isoformat()}.json"
    else:
        md_path = output_dir
        md_path.parent.mkdir(parents=True, exist_ok=True)
        json_path = md_path.with_suffix(".json")

    review_count = conn.execute("SELECT COUNT(*) FROM candidate_reviews").fetchone()[0]
    outcome_count = conn.execute("SELECT COUNT(*) FROM candidate_outcomes").fetchone()[0]

    data: dict = {
        "generated_at": datetime.utcnow().isoformat(),
        "as_of_date": today.isoformat(),
        "outcome_count": outcome_count,
        "review_count": review_count,
        "insufficient_data": review_count < 5 or outcome_count < 5,
    }

    if not data["insufficient_data"]:
        data["outcome_by_tier"] = outcome_by_tier(conn)
        data["outcome_by_score_bucket"] = outcome_by_score_bucket(conn)
        data["outcome_by_review_status"] = outcome_by_review_status(conn)
        data["false_positive_reasons"] = false_positive_reasons(conn)
        data["score_component_vs_outcome"] = score_component_vs_outcome(conn)
        data["feature_coverage"] = feature_coverage_vs_confidence(conn)
        data["source_failure_impact"] = source_failure_impact(conn)
        data["llm_vs_human_review"] = llm_confidence_vs_human_review(conn)
        data["insights"] = generate_insights(conn)
    else:
        data["source_failure_impact"] = source_failure_impact(conn)
        data["insights"] = []

    data["experiments"] = get_experiments(conn, limit=20)

    _write_markdown(md_path, data)
    _write_json(json_path, data)
    logger.info("Learning report written: %s", md_path)
    return md_path


def _write_markdown(path: Path, data: dict) -> None:
    today = data["as_of_date"]
    lines = [
        f"# MHDE Learning Report — {today}",
        f"\n**Generated:** {data['generated_at']}",
        f"**Outcomes tracked:** {data['outcome_count']}",
        f"**Reviews completed:** {data['review_count']}",
        "",
        "---",
        "",
    ]

    if data["insufficient_data"]:
        lines += [
            f"> {_INSUFFICIENT_MSG}",
            "",
            "Continue running daily-radar and submitting human reviews to build calibration data.",
            "",
        ]
    else:
        lines += _section_outcome_by_tier(data.get("outcome_by_tier", []))
        lines += _section_outcome_by_score_bucket(data.get("outcome_by_score_bucket", []))
        lines += _section_outcome_by_review(data.get("outcome_by_review_status", []))
        lines += _section_false_positives(data.get("false_positive_reasons", []))
        lines += _section_score_vs_outcome(data.get("score_component_vs_outcome", []))
        lines += _section_feature_coverage(data.get("feature_coverage", []))

    lines += _section_source_failures(data.get("source_failure_impact", []))
    lines += _section_llm_vs_human(data.get("llm_vs_human_review", []))
    lines += _section_insights(data.get("insights", []))
    lines += _section_experiments(data.get("experiments", []))

    lines += [
        "",
        "---",
        "> Research purposes only. Not investment advice.",
        "> MHDE does not automatically apply scorecard changes.",
        "> All experiments require human approval before being applied.",
    ]

    path.write_text("\n".join(lines))


def _section_outcome_by_tier(rows: list[dict]) -> list[str]:
    lines = ["## Outcome by Tier", ""]
    if not rows:
        return lines + ["No data.", ""]
    lines += ["| Tier | Count | Avg 20d Return | Avg 60d Return | Avg Drawdown 20d |",
              "|------|-------|---------------|----------------|-----------------|"]
    for r in rows:
        ret20 = f"{r['avg_return_20d']:.1%}" if r.get("avg_return_20d") is not None else "N/A"
        ret60 = f"{r['avg_return_60d']:.1%}" if r.get("avg_return_60d") is not None else "N/A"
        dd20 = f"{r['avg_drawdown_20d']:.1%}" if r.get("avg_drawdown_20d") is not None else "N/A"
        lines.append(f"| {r['tier']} | {r['count']} | {ret20} | {ret60} | {dd20} |")
    return lines + [""]


def _section_outcome_by_score_bucket(rows: list[dict]) -> list[str]:
    lines = ["## Outcome by Score Bucket", ""]
    if not rows:
        return lines + ["No data.", ""]
    lines += ["| Score Bucket | Count | Avg 20d Return | Avg 60d Return |",
              "|-------------|-------|----------------|----------------|"]
    for r in rows:
        ret20 = f"{r['avg_return_20d']:.1%}" if r.get("avg_return_20d") is not None else "N/A"
        ret60 = f"{r['avg_return_60d']:.1%}" if r.get("avg_return_60d") is not None else "N/A"
        lines.append(f"| {r['score_bucket']} | {r['count']} | {ret20} | {ret60} |")
    return lines + [""]


def _section_outcome_by_review(rows: list[dict]) -> list[str]:
    lines = ["## Outcome by Review Status", ""]
    if not rows:
        return lines + ["No data.", ""]
    lines += ["| Review Status | Count | Avg Usefulness | Avg Thesis Quality | Avg Evidence Quality |",
              "|--------------|-------|----------------|-------------------|---------------------|"]
    for r in rows:
        def fmt(v):
            return f"{v:.1f}" if v is not None else "N/A"
        lines.append(
            f"| {r['review_status']} | {r['count']} | {fmt(r['avg_usefulness'])} | "
            f"{fmt(r['avg_thesis_quality'])} | {fmt(r['avg_evidence_quality'])} |"
        )
    return lines + [""]


def _section_false_positives(rows: list[dict]) -> list[str]:
    lines = ["## False-Positive Reasons", ""]
    if not rows:
        return lines + ["No false-positive reviews recorded.", ""]
    for r in rows:
        lines.append(f"- **{r['reason']}**: {r['count']}")
    return lines + [""]


def _section_score_vs_outcome(rows: list[dict]) -> list[str]:
    lines = ["## Score Components vs Outcome", ""]
    if not rows:
        return lines + ["No linked score/outcome data.", ""]
    r = rows[0]
    def fmt(v):
        return f"{v:.1f}" if v is not None else "N/A"
    lines += [
        f"- Avg cheap score: {fmt(r.get('avg_cheap'))}",
        f"- Avg quality score: {fmt(r.get('avg_quality'))}",
        f"- Avg catalyst score: {fmt(r.get('avg_catalyst'))}",
        f"- Avg momentum score: {fmt(r.get('avg_momentum'))}",
        f"- Avg sentiment score: {fmt(r.get('avg_sentiment'))}",
        f"- Avg risk penalty: {fmt(r.get('avg_risk'))}",
        f"- Avg 20d forward return: {fmt(r.get('avg_return_20d'))}",
        f"- Sample size: {r.get('count', 0)}",
    ]
    return lines + [""]


def _section_feature_coverage(rows: list[dict]) -> list[str]:
    lines = ["## Feature Coverage", ""]
    if not rows:
        return lines + ["No feature data.", ""]
    coverages = [r["coverage_pct"] for r in rows if r.get("coverage_pct") is not None]
    if coverages:
        lines.append(f"- Avg feature coverage: {sum(coverages)/len(coverages):.0f}%")
        lines.append(f"- Min coverage: {min(coverages):.0f}%")
        lines.append(f"- Max coverage: {max(coverages):.0f}%")
    return lines + [""]


def _section_source_failures(rows: list[dict]) -> list[str]:
    lines = ["## Source Reliability", ""]
    if not rows:
        return lines + ["No source run data.", ""]
    lines += ["| Source | Runs | Error Rate | Last Run |",
              "|--------|------|-----------|---------|"]
    for r in rows:
        er = f"{r['error_rate_pct']:.0f}%" if r.get("error_rate_pct") is not None else "N/A"
        lines.append(f"| {r['source_name']} | {r['total_runs']} | {er} | {r.get('last_run', 'N/A')} |")
    return lines + [""]


def _section_llm_vs_human(rows: list[dict]) -> list[str]:
    lines = ["## LLM vs Human Review", ""]
    if not rows:
        return lines + ["No linked LLM/review data.", ""]
    for r in rows:
        def fmt(v):
            return f"{v:.1f}" if v is not None else "N/A"
        lines.append(f"- **{r['model']}** ({r['count']} reviewed): avg usefulness {fmt(r['avg_usefulness'])}, avg thesis quality {fmt(r['avg_thesis_quality'])}")
    return lines + [""]


def _section_insights(insights: list[dict]) -> list[str]:
    lines = ["## Suggested Experiments & Insights", ""]
    if not insights:
        return lines + ["No insights generated (insufficient review data or no patterns detected).", ""]
    for i, ins in enumerate(insights, 1):
        lines.append(f"### {i}. [{ins['severity'].upper()}] {ins['category']}")
        lines.append(f"{ins['message']}")
        exp = ins.get("suggested_experiment")
        if exp:
            lines.append(f"\n**Suggested change:** {exp['hypothesis']}")
            lines.append(f"**Expected effect:** {exp['expected_effect']}")
            lines.append(f"**Affected:** {', '.join(exp['affected_components'])}")
            lines.append(f"\n> Status: proposed (not applied — requires human approval)")
        lines.append("")
    return lines


def _section_experiments(exps: list[dict]) -> list[str]:
    lines = ["## Experiment History", ""]
    if not exps:
        return lines + ["No experiments recorded.", ""]
    lines += ["| ID | Status | Hypothesis | Approved By |",
              "|----|--------|-----------|------------|"]
    for e in exps:
        lines.append(
            f"| {e['experiment_id']} | {e['status']} | {e['hypothesis'][:60]}... | "
            f"{e.get('approved_by') or '—'} |"
        )
    return lines + [""]


def _write_json(path: Path, data: dict) -> None:
    def _default(obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return str(obj)
    path.write_text(json.dumps(data, indent=2, default=_default))
