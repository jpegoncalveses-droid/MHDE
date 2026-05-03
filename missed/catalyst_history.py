"""Daily catalyst queue history archive and manual review tracking."""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone

logger = logging.getLogger("mhde.missed.catalyst_history")

MANUAL_REVIEW_COLS = ["ticker", "run_date", "analyst_decision", "analyst_notes", "reviewed_at"]
_VALID_DECISIONS = frozenset({"accept", "reject", "watch", "unknown"})

_MD_DEST = "daily_catalyst_queue.md"
_CSV_DEST = "daily_catalyst_queue.csv"
_JSONL_DEST = "daily_catalyst_queue_enriched.jsonl"
_METADATA_FILE = "run_metadata.json"
_REVIEW_FILE = "manual_review.csv"
_SOURCE_THRESHOLD = 200


_HTML_DEST = "daily_catalyst_queue.html"


def archive_run(
    history_root: str,
    run_date: str,
    md_path: str,
    csv_path: str,
    jsonl_path: str,
    metadata: dict,
    *,
    html_path: str | None = None,
) -> str:
    """Copy run artifacts to history_root/YYYY-MM-DD/. Returns the dated directory path.

    Does not overwrite an existing manual_review.csv so analyst notes are preserved
    across same-day re-runs.
    """
    day_dir = os.path.join(history_root, run_date)
    os.makedirs(day_dir, exist_ok=True)

    shutil.copy2(md_path, os.path.join(day_dir, _MD_DEST))
    shutil.copy2(csv_path, os.path.join(day_dir, _CSV_DEST))
    shutil.copy2(jsonl_path, os.path.join(day_dir, _JSONL_DEST))
    if html_path and os.path.exists(html_path):
        shutil.copy2(html_path, os.path.join(day_dir, _HTML_DEST))

    with open(os.path.join(day_dir, _METADATA_FILE), "w") as f:
        json.dump(metadata, f, indent=2, default=str)

    review_path = os.path.join(day_dir, _REVIEW_FILE)
    if not os.path.exists(review_path):
        with open(review_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=MANUAL_REVIEW_COLS)
            writer.writeheader()

    logger.info("Archived catalyst queue run to %s", day_dir)
    return day_dir


def _iter_day_dirs(history_root: str):
    """Yield (date_str, day_path) for each valid dated directory in history_root."""
    if not os.path.isdir(history_root):
        return
    for name in sorted(os.listdir(history_root)):
        path = os.path.join(history_root, name)
        if os.path.isdir(path) and os.path.exists(os.path.join(path, _METADATA_FILE)):
            yield name, path


def _read_all_metadata(history_root: str) -> list[dict]:
    runs = []
    for date_str, day_path in _iter_day_dirs(history_root):
        try:
            with open(os.path.join(day_path, _METADATA_FILE)) as f:
                meta = json.load(f)
            meta["_run_date"] = date_str
            meta["_day_path"] = day_path
            runs.append(meta)
        except Exception:
            pass
    return runs


def _read_all_reviews(history_root: str) -> list[dict]:
    reviews = []
    for date_str, day_path in _iter_day_dirs(history_root):
        review_path = os.path.join(day_path, _REVIEW_FILE)
        if not os.path.exists(review_path):
            continue
        try:
            with open(review_path, newline="") as f:
                for row in csv.DictReader(f):
                    row["_run_date"] = date_str
                    reviews.append(row)
        except Exception:
            pass
    return reviews


def _read_weak_evidence_rows(history_root: str) -> list[dict]:
    rows = []
    for date_str, day_path in _iter_day_dirs(history_root):
        csv_path = os.path.join(day_path, _CSV_DEST)
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("validation_status") == "weak_evidence":
                        row["_run_date"] = date_str
                        rows.append(row)
        except Exception:
            pass
    return rows


def generate_history_summary(history_root: str) -> str:
    """Return a markdown summary of all historical catalyst queue runs."""
    runs = _read_all_metadata(history_root)
    reviews = _read_all_reviews(history_root)
    weak_rows = _read_weak_evidence_rows(history_root)

    n_runs = len(runs)
    total_sampled = sum(int(r.get("sampled", 0) or 0) for r in runs)
    total_promoted = sum(int(r.get("valid_actionable", 0) or 0) for r in runs)
    total_crossings = sum(int(r.get("tier_crossings", 0) or 0) for r in runs)

    decision_counts: Counter = Counter()
    for r in reviews:
        d = (r.get("analyst_decision") or "unknown").strip()
        decision_counts[d] += 1

    ticker_dates: dict[str, set] = defaultdict(set)
    for r in reviews:
        t = (r.get("ticker") or "").strip()
        if t:
            ticker_dates[t].add(r.get("_run_date") or r.get("run_date", ""))

    recurring = {t: dates for t, dates in ticker_dates.items() if len(dates) > 1}
    weak_by_type: Counter = Counter(r.get("catalyst_type", "unknown") for r in weak_rows)

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dates_range = (
        f"{runs[0]['_run_date']} – {runs[-1]['_run_date']}" if runs else "—"
    )

    lines: list[str] = [
        "# Catalyst Queue History Summary",
        "",
        f"Generated: {now}",
        "",
        "---",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total runs | {n_runs} |",
        f"| Date range | {dates_range} |",
        f"| Total sampled events | {total_sampled} |",
        f"| Total promoted candidates | {total_promoted} |",
        f"| Total Reject→C crossings | {total_crossings} |",
        "",
        "---",
        "",
        "## Analyst Decisions",
        "",
        "| Decision | Count |",
        "|----------|-------|",
    ]
    for decision in ("accept", "reject", "watch", "unknown"):
        lines.append(f"| {decision} | {decision_counts.get(decision, 0)} |")
    total_reviewed = sum(decision_counts.values())
    lines += [
        f"| **Total reviewed** | **{total_reviewed}** |",
        "",
        "---",
        "",
        "## Recurring Promoted Tickers",
        "",
    ]
    if recurring:
        lines += [
            "| Ticker | Appearances | Dates |",
            "|--------|-------------|-------|",
        ]
        for ticker, dates in sorted(recurring.items(), key=lambda x: -len(x[1])):
            lines.append(f"| {ticker} | {len(dates)} | {', '.join(sorted(dates))} |")
    else:
        lines.append("_(no tickers promoted in multiple runs yet)_")

    lines += [
        "",
        "---",
        "",
        "## Common Weak Evidence Reasons",
        "",
    ]
    if weak_rows:
        lines += [
            "| Catalyst Type | Count |",
            "|---------------|-------|",
        ]
        for reason, count in weak_by_type.most_common(10):
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("_(no weak evidence data yet)_")

    lines.append("")
    return "\n".join(lines)
