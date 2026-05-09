"""Phase 0 report renderer — Markdown go/no-go document.

Consumes the structured ``Phase0Verdict`` from ``phase0_evaluate`` and
emits operator-facing markdown with:

  - Top-of-report verdict per model (PASS / FAIL / INTERIM)
  - Per-criterion table (status, current value, threshold, sample size)
  - ASCII reliability diagram (text bars, no matplotlib dependency)
  - Sample accumulation note when below the 200-gate

Saved to ``data/reports/phase0_report_YYYY-MM-DD.md`` by default; the
CLI accepts ``--out -`` to print to stdout only.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from crypto.ml.phase0_evaluate import (
    CRYPTO,
    EngineConfig,
    Phase0Verdict,
    ReliabilityBucket,
    SampleAccumulationProjection,
    evaluate_all,
    project_sample_accumulation,
)

logger = logging.getLogger("mhde.crypto.phase0_report")


_VERDICT_ICON = {
    "PASS": "✅",
    "FAIL": "❌",
    "INTERIM": "⏳",
}


def _format_status(s: str) -> str:
    return {"pass": "✅ pass", "fail": "❌ fail", "skip": "— skip"}.get(s, s)


def _format_optional_pct(v: Optional[float], *, signed: bool = False) -> str:
    if v is None:
        return "—"
    if signed:
        return f"{v * 100:+.2f}%"
    return f"{v * 100:.2f}%"


def _format_optional_float(v: Optional[float], digits: int = 3) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


# ──────────────────────────────────────────────────────────────────────
# ASCII reliability diagram
# ──────────────────────────────────────────────────────────────────────


def format_reliability_diagram(
    buckets: list[ReliabilityBucket],
    *,
    bar_width: int = 20,
) -> str:
    """One row per bucket. Two bars side-by-side: expected midpoint
    and observed hit rate. Buckets with n=0 are still emitted so the
    operator sees coverage gaps."""
    if not buckets:
        return "_(no buckets to render)_"
    lines = []
    lines.append(
        f"{'bucket':<13}{'n':>5}  {'expected':>9}  {'observed':>9}  "
        f"{'dev':>8}  diagram"
    )
    lines.append("-" * (13 + 5 + 2 + 9 + 2 + 9 + 2 + 8 + 2 + bar_width + 5))
    for b in buckets:
        bucket_lbl = f"{b.low:.2f}–{b.high:.2f}"
        if b.actual_rate is None:
            actual_str = "—"
            dev_str = "—"
            bar = "·" * bar_width
        else:
            actual_str = f"{b.actual_rate * 100:.1f}%"
            dev_str = f"{b.deviation_pp:+.1f}pp"
            # Two characters: '|' marks expected midpoint, '#' = observed
            expected_pos = int(round(b.midpoint * bar_width))
            observed_pos = int(round(b.actual_rate * bar_width))
            chars = ["·"] * bar_width
            if 0 <= expected_pos < bar_width:
                chars[expected_pos] = "|"
            if 0 <= observed_pos < bar_width:
                # If the observed lands on the expected, show 'X' for "match".
                chars[observed_pos] = "X" if chars[observed_pos] == "|" else "#"
            bar = "".join(chars)
        lines.append(
            f"{bucket_lbl:<13}{b.n:>5}  "
            f"{b.midpoint * 100:>8.1f}%  "
            f"{actual_str:>9}  "
            f"{dev_str:>8}  {bar}"
        )
    lines.append("")
    lines.append(
        "    Legend: `|` = expected midpoint of bucket, "
        "`#` = observed hit rate, `X` = both match."
    )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Per-verdict markdown block
# ──────────────────────────────────────────────────────────────────────


def _criterion_row(c) -> str:
    cv = c.current_value
    ev = c.expected_value
    lo = c.threshold_lo
    hi = c.threshold_hi

    # Format current/expected/threshold sensibly per-criterion
    if c.name == "hit_rate_within_25pct":
        current = _format_optional_pct(cv) if cv is not None else "—"
        expected = _format_optional_pct(ev) if ev is not None else "—"
        threshold = (
            f"[{_format_optional_pct(lo)}, {_format_optional_pct(hi)}]"
            if lo is not None and hi is not None else "—"
        )
    elif c.name == "lift_over_base":
        current = f"{cv:.2f}×" if cv is not None else "—"
        expected = f"{ev:.2f}×" if ev is not None else "—"
        threshold = f"≥ {lo:.2f}×" if lo is not None else "—"
    elif c.name == "calibration_buckets":
        current = f"{cv:.1f}pp avg|dev|" if cv is not None else "—"
        expected = "0pp drift" if ev is not None else "—"
        threshold = (
            f"|bucket dev| ≤ {hi:.0f}pp" if hi is not None else "—"
        )
    elif c.name == "minimum_sample":
        current = f"{int(cv)}" if cv is not None else "—"
        expected = f"{int(ev)}" if ev is not None else "—"
        threshold = f"≥ {int(lo)}" if lo is not None else "—"
    else:
        current = _format_optional_float(cv)
        expected = _format_optional_float(ev)
        threshold = "—"

    return (
        f"| `{c.name}` | {_format_status(c.status)} | {current} | "
        f"{expected} | {threshold} | {c.sample_size} |"
    )


def format_verdict(
    verdict: Phase0Verdict,
    *,
    accumulation: Optional[SampleAccumulationProjection] = None,
) -> str:
    """One model's section of the markdown report."""
    icon = _VERDICT_ICON.get(verdict.overall, "?")
    lines: list[str] = []
    lines.append(
        f"## {icon} `{verdict.model_id}` ({verdict.horizon}) — "
        f"**{verdict.overall}**"
    )
    lines.append("")

    if verdict.overall == "INTERIM":
        lines.append(
            f"_Sample size {verdict.sample_size} below the 200-prediction "
            f"gate; verdict is interim. All four criteria are still "
            f"computed and shown below for trajectory tracking._"
        )
        lines.append("")

    # Criterion table
    lines.append(
        "| criterion | status | current | expected | threshold | sample |"
    )
    lines.append(
        "|---|---|---:|---:|---|---:|"
    )
    # Order: hit_rate, lift, calibration, sample
    order = [
        "hit_rate_within_25pct",
        "lift_over_base",
        "calibration_buckets",
        "minimum_sample",
    ]
    for name in order:
        c = verdict.criteria.get(name)
        if c is not None:
            lines.append(_criterion_row(c))
    lines.append("")

    # Per-criterion detail line(s)
    lines.append("**Per-criterion detail.**")
    lines.append("")
    for name in order:
        c = verdict.criteria.get(name)
        if c is not None:
            lines.append(f"- `{c.name}`: {c.detail}")
    lines.append("")

    # Sample-accumulation projection block (when below threshold)
    if accumulation is not None and accumulation.eta is not None:
        lines.append(
            f"**Sample accumulation.** "
            f"{accumulation.n_filled_now} / "
            f"{accumulation.n_filled_threshold} filled. "
            f"Last 7d added {accumulation.n_filled_last_7d} fills "
            f"({accumulation.n_filled_last_7d / 7.0:.1f}/day). "
            f"Linear projection: gate at "
            f"`{accumulation.eta}` "
            f"({accumulation.days_to_threshold:.0f} days from today)."
        )
        lines.append("")
    elif accumulation is not None and accumulation.n_filled_now >= 200:
        lines.append("**Sample accumulation.** ≥ 200 filled — gate met.")
        lines.append("")
    elif accumulation is not None:
        lines.append(
            "**Sample accumulation.** No filled outcomes in the last 7 "
            "days; cannot project ETA. Verify the prediction pipeline "
            "is firing."
        )
        lines.append("")

    # Reliability diagram
    lines.append("### Reliability diagram (calibration)")
    lines.append("")
    lines.append("```")
    lines.append(format_reliability_diagram(verdict.reliability))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Full report
# ──────────────────────────────────────────────────────────────────────


def format_report(
    verdicts: list[Phase0Verdict],
    *,
    accumulations: Optional[dict[str, SampleAccumulationProjection]] = None,
    report_date: Optional[date] = None,
) -> str:
    """Top-of-report header + each verdict block + footer."""
    accumulations = accumulations or {}
    report_date = report_date or datetime.now(timezone.utc).date()
    lines: list[str] = []
    lines.append(f"# Phase 0 calibration report — {report_date.isoformat()}")
    lines.append("")
    if not verdicts:
        lines.append("_No active models found to evaluate._")
        return "\n".join(lines)

    # Summary table at top
    lines.append("## Summary")
    lines.append("")
    lines.append("| model | horizon | sample | verdict |")
    lines.append("|---|---|---:|:---:|")
    for v in verdicts:
        icon = _VERDICT_ICON.get(v.overall, "?")
        lines.append(
            f"| `{v.model_id}` | {v.horizon} | {v.sample_size} | "
            f"{icon} {v.overall} |"
        )
    lines.append("")
    lines.append(
        "Pass criteria (`docs/PATH_TO_LIVE_PLAN.md` § Phase 0): "
        "hit rate within ±25% of walk-forward baseline; lift ≥ 1.3× over "
        "rolling 30d; no run of ≥ 3 consecutive calibration buckets off "
        "> 10pp; ≥ 200 filled outcomes."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for v in verdicts:
        lines.append(format_verdict(v, accumulation=accumulations.get(v.model_id)))
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def default_report_path(report_date: Optional[date] = None) -> Path:
    report_date = report_date or datetime.now(timezone.utc).date()
    return Path("data") / "reports" / f"phase0_report_{report_date.isoformat()}.md"


def save_report(
    text: str,
    path: Optional[Path] = None,
    *,
    report_date: Optional[date] = None,
) -> Path:
    """Write report to ``path`` (default
    ``data/reports/phase0_report_<date>.md``); ensures parent dir exists."""
    if path is None:
        path = default_report_path(report_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def build_report(
    conn,
    *,
    engine: EngineConfig = CRYPTO,
    model_id: Optional[str] = None,
    report_date: Optional[date] = None,
) -> str:
    """End-to-end: pull verdicts + accumulations from the DB and
    return the rendered markdown."""
    verdicts = evaluate_all(conn, engine=engine, model_id=model_id)
    accumulations = {
        v.model_id: project_sample_accumulation(conn, v.model_id, engine=engine)
        for v in verdicts
    }
    return format_report(
        verdicts, accumulations=accumulations, report_date=report_date,
    )
