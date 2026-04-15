from __future__ import annotations

import csv
import json
import logging
from datetime import date
from pathlib import Path
from typing import Sequence

from adapters.base import ValidationResult

logger = logging.getLogger("mhde.reporter")

_STATUS_ORDER = ["Core", "Useful but optional", "Fallback only", "Reject for v1"]

_CSV_FIELDS = [
    "source", "use_case", "tickers_tested", "access_result", "access_error",
    "required_fields_present", "missing_fields", "historical_depth", "freshness",
    "parsing_difficulty", "rate_limit_notes", "fallback_suggestion",
    "final_status", "notes",
    "score_access", "score_completeness", "score_freshness", "score_reliability",
    "score_parsing_ease", "score_cost_efficiency", "score_strategic_value", "score_total",
]


class Reporter:
    def __init__(self, output_dir: str = "outputs"):
        self._dir = Path(output_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, results: Sequence[ValidationResult]) -> Path:
        path = self._dir / "validation_results.json"
        data = [r.to_dict() for r in results]
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.info("JSON written: %s", path)
        return path

    def write_csv(self, results: Sequence[ValidationResult]) -> Path:
        path = self._dir / "validation_results.csv"
        with open(path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for r in results:
                d = r.to_dict()
                row = {k: d.get(k, "") for k in _CSV_FIELDS}
                row["tickers_tested"] = ",".join(d.get("tickers_tested", []))
                row["missing_fields"] = ",".join(d.get("missing_fields", []))
                s = d.get("scores", {})
                row["score_access"] = s.get("access", "")
                row["score_completeness"] = s.get("completeness", "")
                row["score_freshness"] = s.get("freshness", "")
                row["score_reliability"] = s.get("reliability", "")
                row["score_parsing_ease"] = s.get("parsing_ease", "")
                row["score_cost_efficiency"] = s.get("cost_efficiency", "")
                row["score_strategic_value"] = s.get("strategic_value", "")
                row["score_total"] = s.get("total", "")
                writer.writerow(row)
        logger.info("CSV written: %s", path)
        return path

    def write_markdown(self, results: Sequence[ValidationResult]) -> Path:
        path = self._dir / "validation_report.md"
        lines = [
            f"# MHDE Source Validation Report",
            f"",
            f"**Generated:** {date.today().isoformat()}  ",
            f"**Ticker basket:** AAPL, NVDA, TSLA, JPM, UBER, RKLB, IWM, XLE  ",
            f"**Sources tested:** {len({r.source for r in results})}  ",
            f"**Use-case pairs:** {len(results)}",
            f"",
            f"---",
            f"",
            f"## Summary",
            f"",
        ]

        # Status table
        lines += [
            "| Source | Use Case | Access | Fields OK | Freshness | Score | Status |",
            "|--------|----------|--------|-----------|-----------|-------|--------|",
        ]
        for r in sorted(results, key=lambda x: _STATUS_ORDER.index(x.final_status)):
            fields_ok = "Yes" if r.required_fields_present else f"No ({','.join(r.missing_fields[:2])})"
            score = r.scores.total()
            lines.append(
                f"| {r.source} | {r.use_case} | {r.access_result} | {fields_ok} "
                f"| {r.freshness} | {score}/35 | **{r.final_status}** |"
            )
        lines += ["", "---", ""]

        # Per-source detail
        seen_sources: set[str] = set()
        for r in results:
            if r.source not in seen_sources:
                lines += [f"## {r.source.replace('_', ' ').title()}", ""]
                seen_sources.add(r.source)
            lines += [
                f"### {r.use_case}",
                f"",
                f"- **Access:** {r.access_result}" + (f" — {r.access_error}" if r.access_error else ""),
                f"- **Tickers tested:** {', '.join(r.tickers_tested)}",
                f"- **Required fields present:** {'Yes' if r.required_fields_present else 'No'}",
            ]
            if r.missing_fields:
                lines.append(f"- **Missing fields:** {', '.join(r.missing_fields)}")
            lines += [
                f"- **Historical depth:** {r.historical_depth}",
                f"- **Freshness:** {r.freshness}",
                f"- **Parsing difficulty:** {r.parsing_difficulty}",
                f"- **Rate limit notes:** {r.rate_limit_notes}",
                f"- **Fallback suggestion:** {r.fallback_suggestion}",
                f"- **Notes:** {r.notes}",
                f"",
                f"**Scores:** access={r.scores.access} completeness={r.scores.completeness} "
                f"freshness={r.scores.freshness} reliability={r.scores.reliability} "
                f"parsing_ease={r.scores.parsing_ease} cost={r.scores.cost_efficiency} "
                f"strategic={r.scores.strategic_value} **total={r.scores.total()}/35**",
                f"",
                f"> **Final status: {r.final_status}**",
                f"",
            ]

        path.write_text("\n".join(lines))
        logger.info("Markdown written: %s", path)
        return path

    def write_all(self, results: Sequence[ValidationResult]) -> list[Path]:
        return [
            self.write_json(results),
            self.write_csv(results),
            self.write_markdown(results),
        ]
