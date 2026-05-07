"""TDD tests for daily catalyst queue history archive and manual review tracking."""
from __future__ import annotations

import csv
import json
import os

import pytest


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_artifacts(tmp_path, suffix=""):
    """Create minimal artifact files, return (md, csv, jsonl) paths."""
    md = str(tmp_path / f"queue{suffix}.md")
    cv = str(tmp_path / f"queue{suffix}.csv")
    jl = str(tmp_path / f"queue{suffix}.jsonl")
    open(md, "w").write("# test report")
    open(cv, "w").write("ticker,validation_status,catalyst_type\n")
    open(jl, "w").write("")
    return md, cv, jl


def _archive(history_root, date, tmp_path, metadata=None, suffix=""):
    from missed.catalyst_history import archive_run
    md, cv, jl = _make_artifacts(tmp_path, suffix=suffix)
    return archive_run(history_root, date, md, cv, jl, metadata or {})


# ── 1. archive_run creates dated directory ────────────────────────────────────

def test_archive_run_creates_dated_directory(tmp_path):
    """archive_run creates a YYYY-MM-DD directory under history_root."""
    from missed.catalyst_history import archive_run
    history_root = str(tmp_path / "history")
    md, cv, jl = _make_artifacts(tmp_path)
    archive_run(history_root, "2026-05-02", md, cv, jl, {})
    assert os.path.isdir(os.path.join(history_root, "2026-05-02"))


# ── 2. archive_run copies all three artifacts ─────────────────────────────────

def test_archive_run_copies_all_three_artifacts(tmp_path):
    """archive_run copies md, csv, and jsonl to the dated directory under canonical names."""
    history_root = str(tmp_path / "history")
    _archive(history_root, "2026-05-02", tmp_path)
    day_dir = os.path.join(history_root, "2026-05-02")
    assert os.path.exists(os.path.join(day_dir, "daily_catalyst_queue.md"))
    assert os.path.exists(os.path.join(day_dir, "daily_catalyst_queue.csv"))
    assert os.path.exists(os.path.join(day_dir, "daily_catalyst_queue_enriched.jsonl"))


# ── 3. archive_run writes run_metadata.json ───────────────────────────────────

def test_archive_run_writes_run_metadata_json(tmp_path):
    """archive_run persists the metadata dict as run_metadata.json."""
    history_root = str(tmp_path / "history")
    meta = {"sampled": 43, "valid_actionable": 4, "tier_crossings": 2}
    _archive(history_root, "2026-05-02", tmp_path, metadata=meta)
    meta_path = os.path.join(history_root, "2026-05-02", "run_metadata.json")
    assert os.path.exists(meta_path)
    loaded = json.loads(open(meta_path).read())
    assert loaded["sampled"] == 43
    assert loaded["valid_actionable"] == 4
    assert loaded["tier_crossings"] == 2


# ── 4. archive_run creates empty manual_review.csv with header ────────────────

def test_archive_run_creates_empty_manual_review_csv_with_header(tmp_path):
    """archive_run creates manual_review.csv with header row but no data rows."""
    history_root = str(tmp_path / "history")
    _archive(history_root, "2026-05-02", tmp_path)
    review_path = os.path.join(history_root, "2026-05-02", "manual_review.csv")
    assert os.path.exists(review_path)
    rows = list(csv.DictReader(open(review_path)))
    assert rows == []  # header only


# ── 5. manual_review.csv has the required analyst columns ─────────────────────

def test_manual_review_csv_has_correct_columns(tmp_path):
    """manual_review.csv columns match the required analyst tracking schema."""
    from missed.catalyst_history import MANUAL_REVIEW_COLS
    history_root = str(tmp_path / "history")
    _archive(history_root, "2026-05-02", tmp_path)
    review_path = os.path.join(history_root, "2026-05-02", "manual_review.csv")
    reader = csv.DictReader(open(review_path))
    col_set = set(reader.fieldnames or [])
    for required in ("ticker", "run_date", "analyst_decision", "analyst_notes", "reviewed_at"):
        assert required in col_set, f"Missing column: {required}"
    assert set(MANUAL_REVIEW_COLS) == col_set


# ── 6. archive_run does not overwrite existing manual review ──────────────────

def test_archive_run_does_not_overwrite_existing_manual_review(tmp_path):
    """Re-running archive_run on the same date preserves analyst review data."""
    from missed.catalyst_history import MANUAL_REVIEW_COLS
    history_root = str(tmp_path / "history")
    _archive(history_root, "2026-05-02", tmp_path)

    # Analyst fills in review
    review_path = os.path.join(history_root, "2026-05-02", "manual_review.csv")
    with open(review_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANUAL_REVIEW_COLS)
        writer.writeheader()
        writer.writerow({
            "ticker": "CTRA", "run_date": "2026-05-02",
            "analyst_decision": "accept", "analyst_notes": "solid deal",
            "reviewed_at": "2026-05-02T15:00:00",
        })

    # Re-run same date
    _archive(history_root, "2026-05-02", tmp_path, suffix="-v2")

    rows = list(csv.DictReader(open(review_path)))
    assert len(rows) == 1
    assert rows[0]["ticker"] == "CTRA"
    assert rows[0]["analyst_decision"] == "accept"


# ── 7. history summary counts total runs ──────────────────────────────────────

def test_history_summary_counts_total_runs(tmp_path):
    """generate_history_summary shows the correct total number of historical runs."""
    from missed.catalyst_history import generate_history_summary
    history_root = str(tmp_path / "history")
    _archive(history_root, "2026-05-01", tmp_path, metadata={"sampled": 20, "valid_actionable": 3})
    _archive(history_root, "2026-05-02", tmp_path, metadata={"sampled": 30, "valid_actionable": 4}, suffix="b")

    summary = generate_history_summary(history_root)
    assert "Total runs" in summary
    assert "| 2 |" in summary


# ── 8. history summary aggregates promoted candidate counts ───────────────────

def test_history_summary_counts_promoted_candidates(tmp_path):
    """generate_history_summary sums valid_actionable across all runs."""
    from missed.catalyst_history import generate_history_summary
    history_root = str(tmp_path / "history")
    _archive(history_root, "2026-05-01", tmp_path, metadata={"sampled": 20, "valid_actionable": 3})
    _archive(history_root, "2026-05-02", tmp_path, metadata={"sampled": 30, "valid_actionable": 4}, suffix="b")

    summary = generate_history_summary(history_root)
    assert "Total promoted" in summary
    assert "| 7 |" in summary  # 3 + 4


# ── 9. history summary shows analyst decision counts ─────────────────────────

def test_history_summary_shows_analyst_decision_counts(tmp_path):
    """generate_history_summary reads manual_review.csv and shows accept/reject/watch counts."""
    from missed.catalyst_history import archive_run, generate_history_summary, MANUAL_REVIEW_COLS
    history_root = str(tmp_path / "history")
    md, cv, jl = _make_artifacts(tmp_path)
    archive_run(history_root, "2026-05-02", md, cv, jl, {"sampled": 10, "valid_actionable": 3})

    review_path = os.path.join(history_root, "2026-05-02", "manual_review.csv")
    with open(review_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANUAL_REVIEW_COLS)
        writer.writeheader()
        writer.writerows([
            {"ticker": "CTRA", "run_date": "2026-05-02", "analyst_decision": "accept",
             "analyst_notes": "", "reviewed_at": "2026-05-02T15:00:00"},
            {"ticker": "VG", "run_date": "2026-05-02", "analyst_decision": "reject",
             "analyst_notes": "routine", "reviewed_at": "2026-05-02T15:01:00"},
            {"ticker": "EPD", "run_date": "2026-05-02", "analyst_decision": "watch",
             "analyst_notes": "", "reviewed_at": "2026-05-02T15:02:00"},
        ])

    summary = generate_history_summary(history_root)
    # All three decisions must appear in the summary
    assert "accept" in summary.lower()
    assert "reject" in summary.lower()
    assert "watch" in summary.lower()
    # Each appears exactly once in the review data
    assert "| accept | 1 |" in summary
    assert "| reject | 1 |" in summary
    assert "| watch | 1 |" in summary


# ── 10. history summary shows recurring promoted tickers ─────────────────────

def test_history_summary_shows_recurring_promoted_tickers(tmp_path):
    """Tickers reviewed in multiple runs appear in the Recurring Promoted Tickers section."""
    from missed.catalyst_history import archive_run, generate_history_summary, MANUAL_REVIEW_COLS
    history_root = str(tmp_path / "history")

    for i, date in enumerate(["2026-05-01", "2026-05-02"]):
        md, cv, jl = _make_artifacts(tmp_path, suffix=f"-{i}")
        archive_run(history_root, date, md, cv, jl, {"sampled": 10, "valid_actionable": 2})
        review_path = os.path.join(history_root, date, "manual_review.csv")
        with open(review_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=MANUAL_REVIEW_COLS)
            writer.writeheader()
            writer.writerow({
                "ticker": "CTRA", "run_date": date,
                "analyst_decision": "accept", "analyst_notes": "",
                "reviewed_at": f"{date}T15:00:00",
            })

    summary = generate_history_summary(history_root)
    assert "Recurring" in summary
    assert "CTRA" in summary


# ── 11. history summary shows common weak evidence reasons ────────────────────

def test_history_summary_shows_common_weak_evidence_reasons(tmp_path):
    """generate_history_summary shows the most common catalyst types for weak evidence rows."""
    from missed.catalyst_history import archive_run, generate_history_summary
    history_root = str(tmp_path / "history")

    # Write a queue CSV with two weak_evidence management_change rows
    weak_csv = (
        "ticker,validation_status,catalyst_type,event_date,filing_form_type,"
        "constructed_url,original_score,llm_adjustment,shadow_score,original_tier,"
        "shadow_tier,tier_move,materiality,sentiment,confidence,"
        "quote_validation_pass,final_should_affect_score,evidence_quote\n"
        "PCG,weak_evidence,management_change,2026-01-15,8-K,,42.0,0.0,42.0,Reject,Reject,,low,neutral,0.4,True,False,\n"
        "USB,weak_evidence,management_change,2026-01-15,8-K,,41.0,0.0,41.0,Reject,Reject,,low,neutral,0.4,True,False,\n"
    )
    md = str(tmp_path / "queue.md")
    cv = str(tmp_path / "queue.csv")
    jl = str(tmp_path / "queue.jsonl")
    open(md, "w").write("# report")
    open(cv, "w").write(weak_csv)
    open(jl, "w").write("")

    archive_run(history_root, "2026-05-02", md, cv, jl, {"sampled": 5, "valid_actionable": 1})
    summary = generate_history_summary(history_root)

    assert "Weak Evidence" in summary or "weak evidence" in summary.lower()
    assert "management_change" in summary


# ── 12. generate_queue_report archives when history_root is set ───────────────

def test_generate_queue_report_archives_when_history_root_set(tmp_path):
    """generate_queue_report writes history artifacts when history_root kwarg is provided."""
    from missed.catalyst_queue import generate_queue_report

    queue_entries = [{
        "ticker": "AAA", "event_date": "2026-01-10",
        "filing_form_type": "8-K", "constructed_url": None,
        "catalyst_type": "merger_acquisition", "materiality": "high",
        "sentiment": "bullish", "confidence": 0.9,
        "evidence_quote": "AAA definitive merger agreement.",
        "validation_status": "valid", "quote_validation_pass": True,
        "final_should_affect_score": True,
        "original_score": 43.0, "original_tier": "Reject",
        "llm_adjustment": 5.0, "shadow_score": 48.0,
        "shadow_tier": "C", "tier_move": "Reject→C",
    }]
    revalidated: list[dict] = []
    metadata = {
        "sampled": 1, "valid_actionable": 1, "tier_crossings": 1,
        "run_time": "2026-05-02T12:00:00+00:00",
    }
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")

    generate_queue_report(
        queue_entries, revalidated, output_dir,
        run_metadata=metadata,
        history_root=history_root,
    )

    day_dir = os.path.join(history_root, "2026-05-02")
    assert os.path.isdir(day_dir), "History dated directory was not created"
    assert os.path.exists(os.path.join(day_dir, "daily_catalyst_queue.md"))
    assert os.path.exists(os.path.join(day_dir, "run_metadata.json"))
    assert os.path.exists(os.path.join(day_dir, "manual_review.csv"))


# ── 13. archive_run copies HTML when html_path provided ──────────────────────

def test_archive_run_copies_html_when_provided(tmp_path):
    """archive_run copies html_path to daily_catalyst_queue.html in the dated directory."""
    from missed.catalyst_history import archive_run
    history_root = str(tmp_path / "history")
    md, cv, jl = _make_artifacts(tmp_path)
    html = str(tmp_path / "report.html")
    open(html, "w").write("<html><body>test</body></html>")

    archive_run(history_root, "2026-05-03", md, cv, jl, {}, html_path=html)

    dest = os.path.join(history_root, "2026-05-03", "daily_catalyst_queue.html")
    assert os.path.exists(dest)
    assert "<html>" in open(dest).read()


# ── 14. archive_run works without html_path ───────────────────────────────────

def test_archive_run_works_without_html_path(tmp_path):
    """archive_run with no html_path does not crash and skips HTML copy."""
    from missed.catalyst_history import archive_run
    history_root = str(tmp_path / "history")
    md, cv, jl = _make_artifacts(tmp_path)
    archive_run(history_root, "2026-05-03", md, cv, jl, {})  # no html_path

    day_dir = os.path.join(history_root, "2026-05-03")
    assert os.path.isdir(day_dir)
    assert not os.path.exists(os.path.join(day_dir, "daily_catalyst_queue.html"))


# ── 15. generate_queue_report archives HTML when html_path passed ─────────────

def test_generate_queue_report_archives_html_when_html_path_set(tmp_path):
    """generate_queue_report passes html_path through to archive_run."""
    from missed.catalyst_queue import generate_queue_report

    queue_entries: list[dict] = []
    revalidated: list[dict] = []
    metadata = {
        "sampled": 0, "valid_actionable": 0, "tier_crossings": 0,
        "run_time": "2026-05-03T12:00:00+00:00",
    }
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    html = str(tmp_path / "queue.html")
    open(html, "w").write("<html>report</html>")

    generate_queue_report(
        queue_entries, revalidated, output_dir,
        run_metadata=metadata,
        history_root=history_root,
        html_path=html,
    )

    dest = os.path.join(history_root, "2026-05-03", "daily_catalyst_queue.html")
    assert os.path.exists(dest)
