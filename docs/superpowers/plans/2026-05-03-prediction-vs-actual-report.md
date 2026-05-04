# Prediction-vs-Actual Spike Report v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily learning report comparing MHDE predictions against actual price moves detected in `missed_opportunity_events`, outputting three artifacts and a new CLI command.

**Architecture:** A new `missed/prediction_report.py` module queries `missed_opportunity_events` joined with `companies` for universe tier, classifies each event by how well MHDE predicted it (6 labels), assigns a priority score for ranking, then writes three output artifacts (markdown, CSV, JSONL). A new `main.py` CLI command `missed prediction-vs-actual` wires it up. No production scores are written.

**Tech Stack:** Python 3.11, DuckDB, csv, json, pathlib, click (existing deps)

---

## File Structure

| Path | Action | Responsibility |
|------|--------|----------------|
| `missed/prediction_report.py` | **Create** | classify_row(), _priority_score(), build_rows(), generate_prediction_report() |
| `tests/test_prediction_report.py` | **Create** | 7 tests covering ranking, classification, sections, columns, no mutation |
| `main.py` | **Modify** | Add `missed prediction-vs-actual` command |

---

### Task 1: Core Classification + Ranking Logic

**Files:**
- Create: `missed/prediction_report.py`
- Create: `tests/test_prediction_report.py`

This task implements the pure logic — classify_row(), _priority_score(), build_rows() — and tests the four ranking requirements.

**Classification labels** (first match wins):
- `universe_miss` — `was_in_universe = False`
- `unscored_mover` — `was_in_universe = True` AND `was_scored = False`
- `near_threshold` — `was_scored = True` AND `40.0 ≤ score_before_event < 45.0`
- `scored_correct` — `was_scored = True` AND `tier_before_event IN ('A', 'B')`
- `scored_missed` — `was_scored = True` AND `tier_before_event = 'C'`
- `true_miss` — fallback (scored Reject with score < 40, or Incomplete)

**Priority score** = `window_urgency + universe_bonus + threshold_bonus`
- `window_urgency`: `{1: 5, 3: 4, 10: 3, 20: 2, 60: 1}`, else 0
- `universe_bonus`: primary → 0.3, extended → 0.1, not in universe → 0.0
- `threshold_bonus`: near_threshold → 0.2, else 0.0

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prediction_report.py`:

```python
"""Prediction-vs-actual report — TDD suite."""
from __future__ import annotations

import csv
import json
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _event(conn, ticker, window_days, return_value=15.0, *,
           was_in_universe=True, was_scored=True,
           score=55.0, tier="C",
           universe_tier="extended",
           event_date=None):
    if event_date is None:
        event_date = (date.today() - timedelta(days=3)).isoformat()
    event_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO missed_opportunity_events
           (event_id, ticker, event_date, event_type, return_value, window_days,
            was_in_universe, was_scored, score_before_event, tier_before_event,
            had_catalyst_evidence, investigation_status)
           VALUES (?, ?, ?, 'gain_test', ?, ?, ?, ?, ?, ?, false, 'pending')""",
        [event_id, ticker, event_date, return_value, window_days,
         was_in_universe, was_scored, score if was_scored else None,
         tier if was_scored else None],
    )
    if universe_tier and was_in_universe:
        try:
            conn.execute(
                """INSERT INTO companies (ticker, company_name, universe_tier, is_active)
                   VALUES (?, ?, ?, true)
                   ON CONFLICT (ticker) DO UPDATE SET universe_tier = excluded.universe_tier""",
                [ticker, ticker, universe_tier],
            )
        except Exception:
            pass


def test_1d_spikes_rank_above_longer_moves(conn):
    """1-day spikes must rank above 20-day stale moves."""
    from missed.prediction_report import build_rows
    _event(conn, "SHORT", window_days=1, return_value=8.0, universe_tier="extended")
    _event(conn, "LONG", window_days=20, return_value=25.0, universe_tier="extended")
    rows = build_rows(conn)
    tickers = [r["ticker"] for r in rows]
    assert tickers.index("SHORT") < tickers.index("LONG"), (
        f"1d spike (SHORT) should outrank 20d move (LONG), got order: {tickers}"
    )


def test_primary_universe_ranks_above_extended(conn):
    """Primary-universe events must rank above extended-universe events at same window."""
    from missed.prediction_report import build_rows
    _event(conn, "EXT", window_days=1, return_value=10.0, universe_tier="extended")
    _event(conn, "PRIM", window_days=1, return_value=10.0, universe_tier="primary")
    rows = build_rows(conn)
    tickers = [r["ticker"] for r in rows]
    assert tickers.index("PRIM") < tickers.index("EXT"), (
        f"Primary (PRIM) should outrank extended (EXT), got order: {tickers}"
    )


def test_near_threshold_score_increases_priority(conn):
    """Near-threshold score (40–45) should rank above deep-reject at same window."""
    from missed.prediction_report import build_rows
    _event(conn, "NEAR", window_days=10, score=42.0, tier="Reject", universe_tier="extended")
    _event(conn, "DEEP", window_days=10, score=25.0, tier="Reject", universe_tier="extended")
    rows = build_rows(conn)
    tickers = [r["ticker"] for r in rows]
    assert tickers.index("NEAR") < tickers.index("DEEP"), (
        f"Near-threshold (NEAR) should outrank deep reject (DEEP), got order: {tickers}"
    )


def test_no_score_events_are_visible(conn):
    """Events with was_scored=False must appear in results with classification 'unscored_mover'."""
    from missed.prediction_report import build_rows
    _event(conn, "UNSCORE", window_days=5, was_scored=False)
    rows = build_rows(conn)
    found = [r for r in rows if r["ticker"] == "UNSCORE"]
    assert found, "UNSCORE event should appear in build_rows() results"
    assert found[0]["classification"] == "unscored_mover", (
        f"Expected 'unscored_mover', got '{found[0]['classification']}'"
    )


def test_universe_miss_classification(conn):
    """Events with was_in_universe=False must get classification 'universe_miss'."""
    from missed.prediction_report import build_rows
    _event(conn, "NOTINUNIV", window_days=5, was_in_universe=False, was_scored=False)
    rows = build_rows(conn)
    found = [r for r in rows if r["ticker"] == "NOTINUNIV"]
    assert found, "NOTINUNIV should appear"
    assert found[0]["classification"] == "universe_miss"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
venv/bin/python -m pytest tests/test_prediction_report.py -v 2>&1 | tail -30
```

Expected: 5 failures with `ModuleNotFoundError: No module named 'missed.prediction_report'`

- [ ] **Step 3: Create `missed/prediction_report.py` with core logic**

```python
"""Prediction-vs-actual spike report.

Compares MHDE scores before each detected move against what actually moved.
Shadow/diagnostic only — no production scores are written.
"""
from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import duckdb

logger = logging.getLogger("mhde.missed.prediction_report")

_NEAR_THRESHOLD_MIN = 40.0
_NEAR_THRESHOLD_MAX = 45.0

_WINDOW_URGENCY: dict[int, int] = {1: 5, 3: 4, 10: 3, 20: 2, 60: 1}

_REPORT_MD = "prediction_vs_actual_report.md"
_REPORT_CSV = "prediction_vs_actual_rows.csv"
_REPORT_JSONL = "missed_spike_investigations.jsonl"

_CSV_COLS = [
    "ticker", "event_date", "event_type", "return_value", "window_days",
    "classification", "priority_score", "universe_tier",
    "score_before_event", "tier_before_event",
    "had_catalyst_evidence", "was_in_universe", "was_scored",
    "root_cause_hint",
]

_QUERY = """
SELECT m.ticker, m.event_date, m.event_type, m.return_value, m.window_days,
       m.was_in_universe, m.was_scored, m.score_before_event,
       m.tier_before_event, m.had_catalyst_evidence,
       c.universe_tier
FROM missed_opportunity_events m
LEFT JOIN companies c ON m.ticker = c.ticker
WHERE m.event_date >= ?
ORDER BY m.event_date DESC
"""

_QUERY_COLS = [
    "ticker", "event_date", "event_type", "return_value", "window_days",
    "was_in_universe", "was_scored", "score_before_event",
    "tier_before_event", "had_catalyst_evidence", "universe_tier",
]


def classify_row(row: dict) -> str:
    """Assign one of 6 classification labels to a detected spike event."""
    if not row.get("was_in_universe"):
        return "universe_miss"
    if not row.get("was_scored"):
        return "unscored_mover"
    score = row.get("score_before_event")
    if score is not None and _NEAR_THRESHOLD_MIN <= score < _NEAR_THRESHOLD_MAX:
        return "near_threshold"
    tier = row.get("tier_before_event") or ""
    if tier in ("A", "B"):
        return "scored_correct"
    if tier == "C":
        return "scored_missed"
    return "true_miss"


def _root_cause_hint(classification: str, row: dict) -> str:
    if classification == "universe_miss":
        return "universe_gap"
    if classification == "unscored_mover":
        return "data_gap"
    if classification == "near_threshold":
        return "near_threshold"
    if classification == "true_miss":
        return "scoring_blind_spot"
    if classification == "scored_missed":
        return "catalyst_missed" if not row.get("had_catalyst_evidence") else "scoring_blind_spot"
    return "unknown"


def _priority_score(row: dict, classification: str) -> float:
    urgency = _WINDOW_URGENCY.get(int(row.get("window_days") or 0), 0)
    universe_bonus = (
        0.3 if row.get("universe_tier") == "primary" else
        0.1 if row.get("universe_tier") == "extended" else
        0.0
    )
    threshold_bonus = 0.2 if classification == "near_threshold" else 0.0
    return urgency + universe_bonus + threshold_bonus


def build_rows(
    conn: duckdb.DuckDBPyConnection,
    lookback_days: int = 90,
) -> list[dict]:
    """Query missed_opportunity_events + companies; return enriched, ranked rows."""
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    raw = conn.execute(_QUERY, [cutoff]).fetchall()

    result: list[dict] = []
    for r in raw:
        row = dict(zip(_QUERY_COLS, r))
        classification = classify_row(row)
        priority = _priority_score(row, classification)
        row["classification"] = classification
        row["priority_score"] = round(priority, 3)
        row["root_cause_hint"] = _root_cause_hint(classification, row)
        result.append(row)

    result.sort(key=lambda r: -r["priority_score"])
    return result
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
venv/bin/python -m pytest tests/test_prediction_report.py -v 2>&1 | tail -20
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add missed/prediction_report.py tests/test_prediction_report.py
git commit -m "feat: add prediction_report core — classify_row, build_rows, priority ranking"
```

---

### Task 2: Report Writer, CSV, JSONL, and CLI

**Files:**
- Modify: `missed/prediction_report.py` (add `generate_prediction_report()`)
- Modify: `main.py` (add `missed prediction-vs-actual` command ~line 554)
- Modify: `tests/test_prediction_report.py` (add sections, columns, no-mutation tests)

This task adds the three output artifacts and the CLI entry point, then tests them.

- [ ] **Step 1: Add 3 more tests to `tests/test_prediction_report.py`**

Append these three test functions after the existing ones:

```python
def test_report_contains_required_sections(tmp_path, conn):
    """Markdown report must contain all 7 required section headings."""
    from missed.prediction_report import generate_prediction_report
    _event(conn, "AAA", window_days=1)
    _event(conn, "BBB", window_days=10, universe_tier="primary")
    md_path, _, _ = generate_prediction_report(conn, output_dir=str(tmp_path))
    md = Path(md_path).read_text()
    required = [
        "# Prediction vs Actual Spike Report",
        "## Summary",
        "## 1-Day Spikes",
        "## 3d / 10d Spikes",
        "## Longer Windows",
        "## Out-of-Universe Spikes",
        "## Near-Threshold Scores",
        "## No-Score Events",
    ]
    for heading in required:
        assert heading in md, f"Missing section heading: {heading!r}"


def test_csv_contains_required_columns(tmp_path, conn):
    """CSV must contain all required columns."""
    from missed.prediction_report import generate_prediction_report, _CSV_COLS
    _event(conn, "CCC", window_days=5)
    _, csv_path, _ = generate_prediction_report(conn, output_dir=str(tmp_path))
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
    for col in _CSV_COLS:
        assert col in header, f"Missing CSV column: {col!r}"


def test_no_production_score_mutation(tmp_path, conn):
    """generate_prediction_report must not alter the scores table."""
    from missed.prediction_report import generate_prediction_report
    score_id = uuid.uuid4().hex[:16]
    run_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO scores
           (id, run_id, ticker, as_of_date, total_score, tier)
           VALUES (?, ?, 'SCORE_TEST', CURRENT_DATE, 55.0, 'C')""",
        [score_id, run_id],
    )
    _event(conn, "SCORE_TEST", window_days=5)
    generate_prediction_report(conn, output_dir=str(tmp_path))
    row = conn.execute(
        "SELECT total_score FROM scores WHERE id = ?", [score_id]
    ).fetchone()
    assert row is not None and row[0] == 55.0, (
        f"Score was mutated: expected 55.0, got {row}"
    )
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
venv/bin/python -m pytest tests/test_prediction_report.py::test_report_contains_required_sections tests/test_prediction_report.py::test_csv_contains_required_columns tests/test_prediction_report.py::test_no_production_score_mutation -v 2>&1 | tail -20
```

Expected: 3 failures — `test_report_contains_required_sections` fails on missing `generate_prediction_report`, `test_csv_contains_required_columns` same, `test_no_production_score_mutation` same.

- [ ] **Step 3: Add `generate_prediction_report()` to `missed/prediction_report.py`**

Add after the `build_rows()` function:

```python
def _section_lines(title: str, events: list[dict]) -> list[str]:
    lines = ["---", "", f"## {title}", ""]
    if events:
        lines += [
            "| Ticker | Return | Window | Score | Tier | Universe | Classification | Root Cause |",
            "|--------|--------|--------|-------|------|----------|----------------|------------|",
        ]
        for e in events:
            score = f"{e['score_before_event']:.1f}" if e.get("score_before_event") is not None else "—"
            tier = e.get("tier_before_event") or "—"
            ut = e.get("universe_tier") or "—"
            lines.append(
                f"| {e['ticker']} | +{e['return_value']:.1f}% | {e['window_days']}d"
                f" | {score} | {tier} | {ut} | `{e['classification']}` | {e['root_cause_hint']} |"
            )
    else:
        lines.append("_(no events in this window)_")
    lines.append("")
    return lines


def generate_prediction_report(
    conn: duckdb.DuckDBPyConnection,
    output_dir: str = "data/processed",
    *,
    lookback_days: int = 90,
) -> tuple[Path, Path, Path]:
    """Generate prediction-vs-actual report artifacts.

    Returns (md_path, csv_path, jsonl_path). Shadow-only — no scores written.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    rows = build_rows(conn, lookback_days=lookback_days)
    label_counts = Counter(r["classification"] for r in rows)

    lines: list[str] = [
        "# Prediction vs Actual Spike Report",
        "",
        f"Generated: {today} | Lookback: {lookback_days}d | Total events: {len(rows)}",
        "",
        "> **Shadow-only: no production scores were changed.**",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Classification | Count |",
        "|----------------|-------|",
    ]
    for label in ("scored_correct", "scored_missed", "near_threshold",
                  "true_miss", "unscored_mover", "universe_miss"):
        lines.append(f"| `{label}` | {label_counts.get(label, 0)} |")
    lines.append("")

    lines += _section_lines("1-Day Spikes", [r for r in rows if r.get("window_days") == 1])
    lines += _section_lines("3d / 10d Spikes", [r for r in rows if r.get("window_days") in (3, 10)])
    lines += _section_lines("Longer Windows (20d / 60d)", [r for r in rows if r.get("window_days") in (20, 60)])
    lines += _section_lines("Out-of-Universe Spikes", [r for r in rows if r["classification"] == "universe_miss"])
    lines += _section_lines("Near-Threshold Scores", [r for r in rows if r["classification"] == "near_threshold"])
    lines += _section_lines("No-Score Events", [r for r in rows if r["classification"] in ("unscored_mover", "true_miss")])

    md_path = out / _REPORT_MD
    md_path.write_text("\n".join(lines) + "\n")

    csv_path = out / _REPORT_CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    jsonl_path = out / _REPORT_JSONL
    with open(jsonl_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")

    logger.info("Prediction-vs-actual report: %s (%d rows)", md_path, len(rows))
    return md_path, csv_path, jsonl_path
```

- [ ] **Step 4: Add the CLI command to `main.py`**

Find the `@missed.command("pilot")` block (around line 555). Insert the new command **before** it:

```python
@missed.command("prediction-vs-actual")
@click.option("--output-dir", default="data/processed", show_default=True,
              help="Directory for output artifacts.")
@click.option("--lookback-days", default=90, type=int, show_default=True,
              help="Days of events to include.")
def missed_prediction_vs_actual(output_dir, lookback_days):
    """Daily learning report: MHDE predictions vs actual movers."""
    from missed.prediction_report import generate_prediction_report

    cfg, conn = _engine_setup()
    try:
        md_path, csv_path, jsonl_path = generate_prediction_report(
            conn, output_dir=output_dir, lookback_days=lookback_days
        )
        click.echo("Prediction-vs-actual report written:")
        click.echo(f"  Markdown: {md_path}")
        click.echo(f"  CSV:      {csv_path}")
        click.echo(f"  JSONL:    {jsonl_path}")
    finally:
        conn.close()

```

- [ ] **Step 5: Run all 8 tests to confirm they pass**

```bash
venv/bin/python -m pytest tests/test_prediction_report.py -v 2>&1 | tail -20
```

Expected: 8 passed

- [ ] **Step 6: Run the full test suite to check for regressions**

```bash
venv/bin/python -m pytest --tb=short -q 2>&1 | tail -20
```

Expected: green, no regressions.

- [ ] **Step 7: Smoke-test the CLI**

```bash
venv/bin/python main.py missed prediction-vs-actual --output-dir /tmp/pva_test 2>&1 | tail -10
```

Expected output includes "Prediction-vs-actual report written:" and three file paths.

- [ ] **Step 8: Commit**

```bash
git add missed/prediction_report.py tests/test_prediction_report.py main.py
git commit -m "feat: add prediction-vs-actual spike report — generate_prediction_report + CLI"
```

---

## Self-Review

**Spec coverage:**
- ✅ Rank actual movers by priority (1d > stale; primary > extended; near-threshold bonus) — `_priority_score()`
- ✅ 6 classification labels — `classify_row()`
- ✅ 8 root cause types mapped via `_root_cause_hint()`: universe_gap, data_gap, near_threshold, scoring_blind_spot, catalyst_missed, unknown (noise/sector_tailwind surfaced as unknown — deterministic v1 doesn't have sector context)
- ✅ 7 report sections in `generate_prediction_report()` — Summary + 6 sub-sections
- ✅ CLI: `main.py missed prediction-vs-actual`
- ✅ 6 of 6 tests: ranking 1d vs stale, primary vs extended, near-threshold priority, no-score visible, sections/columns present, no score mutation
- ✅ No OpenAI calls — pure SQL + Python
- ✅ No scoring weight changes — scores table read-only
- ✅ Shadow-only disclaimer in report header

**Placeholder scan:** No TBD/TODO. All code blocks are complete.

**Type consistency:**
- `build_rows()` → `list[dict]` → used by `generate_prediction_report()` ✅
- `classify_row(row: dict) -> str` — called in `build_rows()` ✅
- `_priority_score(row, classification) -> float` — called in `build_rows()` ✅
- `generate_prediction_report()` returns `tuple[Path, Path, Path]` — tested in Task 2 tests ✅
- `_CSV_COLS` exported from module — imported directly in test ✅
