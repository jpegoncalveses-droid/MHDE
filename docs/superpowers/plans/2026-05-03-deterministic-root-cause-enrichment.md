# Deterministic Root-Cause Enrichment for Prediction-vs-Actual Missed Spikes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic (no LLM) root-cause labels to every row in the prediction-vs-actual report, surfacing *why* MHDE missed or nearly caught each spike using existing DB tables only.

**Architecture:** A new `missed/root_cause_enrichment.py` module fetches lookup data from the DB (scores components, fundamentals_features, events, companies) and assigns one of 11 structured root-cause labels to each prediction row. `generate_prediction_report` calls `enrich_rows` internally to embed a Root Cause Summary section. A separate `missed enrich-root-causes` CLI command reads the prediction CSV and writes two standalone enrichment artifacts.

**Tech Stack:** Python 3.11, DuckDB, Click, `missed/prediction_report.py` (existing), standard library only — no LLM, no new data sources, no migrations.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `missed/root_cause_enrichment.py` | **Create** | 11-label deterministic classifier; DB lookups; enrichment report writer |
| `tests/test_root_cause_enrichment.py` | **Create** | 8 tests covering each root-cause branch |
| `main.py` | **Modify** (~line 575) | Add `missed enrich-root-causes` CLI command after `prediction-vs-actual` |
| `missed/prediction_report.py` | **Modify** | Call `enrich_rows` inside `generate_prediction_report`; add Root Cause Summary section |
| `tests/test_prediction_report.py` | **Modify** | Update `test_report_contains_required_sections` to include new heading |

---

## Root-Cause Label Reference

11 labels assigned by first-match priority (most specific first):

| Label | Group | Priority | Detection Logic |
|-------|-------|----------|-----------------|
| `universe_not_seeded` | `universe_gap` | 1 | `classification == "universe_miss"` |
| `pre_score_history` | `data_gap` | 2 | `classification == "unscored_mover"` |
| `incomplete_fundamentals` | `data_gap` | 3 | `tier_before_event == "Incomplete"` |
| `missing_earnings_context` | `feature_gap` | 4 | event_date within 7 days of earnings event in `events` table |
| `sector_cluster_move` | `feature_gap` | 5 | 3+ tickers in same sector moved same window within ±3 days |
| `no_evidence_no_filing` | `data_gap` | 6 | `had_catalyst_evidence == False` |
| `low_catalyst_score` | `scoring_gap` | 7 | score available AND `catalyst_score < 30` |
| `low_quality_score` | `scoring_gap` | 8 | score available AND `quality_score < 40` |
| `near_threshold_no_catalyst` | `near_miss` | 9 | `classification == "near_threshold"` AND `catalyst_score < 30` |
| `near_threshold_scored` | `near_miss` | 10 | `classification == "near_threshold"` AND `catalyst_score >= 30` |
| `unknown` | `unknown` | 11 | fallback |

Each enriched row gains 6 new fields:
- `enriched_root_cause` (str) — label from table above
- `root_cause_group` (str) — broad group: data_gap / scoring_gap / feature_gap / near_miss / universe_gap / unknown
- `explanation_short` (str) — one sentence for display
- `evidence_fields_used` (str) — comma-separated DB fields that drove the classification
- `suggested_fix` (str) — actionable next step
- `confidence` (str) — `high` / `medium` / `low`

---

## Task 1: Core Enrichment Module

**Files:**
- Create: `missed/root_cause_enrichment.py`

- [ ] **Step 1: Write failing test — enrich_rows attaches 6 new fields to every row**

```python
# tests/test_root_cause_enrichment.py
from __future__ import annotations
import uuid
from datetime import date, timedelta
import pytest
from storage.db import get_connection, init_schema

ENRICHMENT_FIELDS = [
    "enriched_root_cause", "root_cause_group",
    "explanation_short", "evidence_fields_used",
    "suggested_fix", "confidence",
]

@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()

def _row(**kwargs) -> dict:
    defaults = dict(
        ticker="TST", event_date=date.today() - timedelta(days=3),
        event_type="gain_1d", return_value=10.0, window_days=1,
        classification="true_miss", was_in_universe=True, was_scored=True,
        score_before_event=30.0, tier_before_event="Reject",
        had_catalyst_evidence=True, universe_tier="primary",
        root_cause_hint="scoring_blind_spot", score_join_method="scores_join",
        priority_score=5.3,
    )
    defaults.update(kwargs)
    return defaults

def test_enrich_rows_attaches_all_six_fields(conn):
    from missed.root_cause_enrichment import enrich_rows
    rows = [_row()]
    enriched = enrich_rows(rows, conn)
    assert len(enriched) == 1
    for field in ENRICHMENT_FIELDS:
        assert field in enriched[0], f"Missing field: {field}"
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_root_cause_enrichment.py::test_enrich_rows_attaches_all_six_fields -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'missed.root_cause_enrichment'`

- [ ] **Step 3: Create `missed/root_cause_enrichment.py` with stub**

```python
"""Deterministic root-cause enrichment for prediction-vs-actual rows.

Assigns one of 11 structured labels using existing DB tables only — no LLM.
Shadow/diagnostic only; no production scores are written.
"""
from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import duckdb

logger = logging.getLogger("mhde.missed.root_cause_enrichment")

_ROOT_CAUSE_GROUPS: dict[str, str] = {
    "universe_not_seeded":       "universe_gap",
    "pre_score_history":         "data_gap",
    "incomplete_fundamentals":   "data_gap",
    "no_evidence_no_filing":     "data_gap",
    "missing_earnings_context":  "feature_gap",
    "sector_cluster_move":       "feature_gap",
    "low_catalyst_score":        "scoring_gap",
    "low_quality_score":         "scoring_gap",
    "near_threshold_no_catalyst":"near_miss",
    "near_threshold_scored":     "near_miss",
    "unknown":                   "unknown",
}

_EXPLANATIONS: dict[str, str] = {
    "universe_not_seeded":        "Ticker not in the MHDE universe YAML — never scored.",
    "pre_score_history":          "Event predates score history; no prior score to join.",
    "incomplete_fundamentals":    "Tier=Incomplete: fewer than 2 fundamental components available.",
    "no_evidence_no_filing":      "No catalyst text found; had_catalyst_evidence=False.",
    "missing_earnings_context":   "Event within 7 days of an earnings event; no earnings-surprise signal.",
    "sector_cluster_move":        "3+ sector peers moved the same window in the same 3-day window.",
    "low_catalyst_score":         "Catalyst component score below 30; weak or absent catalyst signal.",
    "low_quality_score":          "Quality component score below 40; business quality signal too weak.",
    "near_threshold_no_catalyst": "Score near C-tier threshold but catalyst score < 30.",
    "near_threshold_scored":      "Score near C-tier threshold; a stronger catalyst signal might tip it.",
    "unknown":                    "Root cause could not be determined from available data.",
}

_SUGGESTED_FIXES: dict[str, str] = {
    "universe_not_seeded":        "Verify YAML covers all current S&P 500 members; add missing tickers.",
    "pre_score_history":          "Accumulate score history — no code change needed.",
    "incomplete_fundamentals":    "Add fundamentals data source (Alpha Vantage or Polygon) for this ticker.",
    "no_evidence_no_filing":      "Add EFTS fallback or press-release scraper to increase filing coverage.",
    "missing_earnings_context":   "Add EPS estimates adapter; wire earnings-proximity feature to scoring.",
    "sector_cluster_move":        "Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature.",
    "low_catalyst_score":         "Investigate catalyst source coverage for this ticker and date.",
    "low_quality_score":          "Review quality fundamentals for this ticker; check data freshness.",
    "near_threshold_no_catalyst": "Improve catalyst coverage; this ticker may tip to C-tier with better signals.",
    "near_threshold_scored":      "Calibrate threshold — consider 43.0 as a watch-list boundary.",
    "unknown":                    "Manual investigation required.",
}

_CONFIDENCE: dict[str, str] = {
    "universe_not_seeded":        "high",
    "pre_score_history":          "high",
    "incomplete_fundamentals":    "high",
    "no_evidence_no_filing":      "medium",
    "missing_earnings_context":   "medium",
    "sector_cluster_move":        "medium",
    "low_catalyst_score":         "medium",
    "low_quality_score":          "low",
    "near_threshold_no_catalyst": "medium",
    "near_threshold_scored":      "medium",
    "unknown":                    "low",
}

_EVIDENCE_FIELDS: dict[str, str] = {
    "universe_not_seeded":        "was_in_universe,classification",
    "pre_score_history":          "classification,score_join_method",
    "incomplete_fundamentals":    "tier_before_event",
    "no_evidence_no_filing":      "had_catalyst_evidence",
    "missing_earnings_context":   "event_date,events.event_date,events.event_type",
    "sector_cluster_move":        "companies.sector,window_days,event_date",
    "low_catalyst_score":         "scores.catalyst_score",
    "low_quality_score":          "scores.quality_score",
    "near_threshold_no_catalyst": "score_before_event,scores.catalyst_score",
    "near_threshold_scored":      "score_before_event,scores.catalyst_score",
    "unknown":                    "",
}

_REPORT_ENRICHED_CSV = "prediction_vs_actual_enriched_rows.csv"
_REPORT_ENRICHED_MD  = "root_cause_enrichment_report.md"

_ENRICHMENT_EXTRA_COLS = [
    "enriched_root_cause", "root_cause_group", "explanation_short",
    "evidence_fields_used", "suggested_fix", "confidence",
]
```

Continue the file with the lookup and classification functions (Step 4).

- [ ] **Step 4: Add DB lookup functions to `missed/root_cause_enrichment.py`**

Append after the constants block:

```python
def _fetch_score_components(conn: duckdb.DuckDBPyConnection) -> dict[tuple, dict]:
    """Return {(ticker, as_of_date_str): {catalyst_score, quality_score, ...}}."""
    rows = conn.execute("""
        SELECT ticker, as_of_date, catalyst_score, quality_score, momentum_score, cheap_score
        FROM scores
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY ticker, as_of_date ORDER BY created_at DESC
        ) = 1
    """).fetchall()
    result: dict[tuple, dict] = {}
    for ticker, as_of_date, cat, qual, mom, cheap in rows:
        key = (ticker, str(as_of_date))
        result[key] = {
            "catalyst_score": cat,
            "quality_score":  qual,
            "momentum_score": mom,
            "cheap_score":    cheap,
        }
    return result


def _fetch_earnings_dates(conn: duckdb.DuckDBPyConnection) -> dict[str, list[date]]:
    """Return {ticker: [earnings_date, ...]} from events table."""
    rows = conn.execute(
        "SELECT ticker, event_date FROM events WHERE event_type = 'earnings' AND ticker IS NOT NULL"
    ).fetchall()
    result: dict[str, list[date]] = defaultdict(list)
    for ticker, event_date in rows:
        if ticker and event_date:
            result[ticker].append(event_date if isinstance(event_date, date) else date.fromisoformat(str(event_date)))
    return dict(result)


def _fetch_sector_map(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Return {ticker: sector} for all companies with a non-NULL sector."""
    rows = conn.execute(
        "SELECT ticker, sector FROM companies WHERE sector IS NOT NULL"
    ).fetchall()
    return {ticker: sector for ticker, sector in rows}


def _detect_sector_clusters(
    rows: list[dict],
    sector_map: dict[str, str],
) -> set[tuple]:
    """Return set of (ticker, event_date_str, window_days) tuples in a 3+ sector cluster."""
    result: set[tuple] = set()
    for i, r in enumerate(rows):
        sector = sector_map.get(r["ticker"])
        if not sector:
            continue
        window = r.get("window_days")
        d_raw = r["event_date"]
        d = d_raw if isinstance(d_raw, date) else date.fromisoformat(str(d_raw))

        peers = [
            j for j, other in enumerate(rows)
            if j != i
            and sector_map.get(other["ticker"]) == sector
            and other.get("window_days") == window
            and abs(
                (
                    (other["event_date"] if isinstance(other["event_date"], date)
                     else date.fromisoformat(str(other["event_date"]))) - d
                ).days
            ) <= 3
        ]
        if len(peers) >= 2:
            result.add((r["ticker"], str(d), window))
    return result


def _best_score_key(
    ticker: str,
    event_date_raw,
    score_components: dict[tuple, dict],
) -> dict | None:
    """Return score component dict for the latest score on or before event_date."""
    event_date = (
        event_date_raw if isinstance(event_date_raw, date)
        else date.fromisoformat(str(event_date_raw))
    )
    candidates = [
        (date.fromisoformat(d_str), v)
        for (t, d_str), v in score_components.items()
        if t == ticker and date.fromisoformat(d_str) <= event_date
    ]
    if not candidates:
        return None
    _, best = max(candidates, key=lambda x: x[0])
    return best


def _assign_root_cause(
    row: dict,
    *,
    score_components: dict[tuple, dict],
    earnings_dates: dict[str, list[date]],
    sector_clusters: set[tuple],
) -> str:
    """Return the first-matching root-cause label for this row."""
    classification = row.get("classification", "")
    ticker = row["ticker"]
    event_date_raw = row["event_date"]
    event_date = (
        event_date_raw if isinstance(event_date_raw, date)
        else date.fromisoformat(str(event_date_raw))
    )
    window = row.get("window_days")
    tier = row.get("tier_before_event") or ""

    if classification == "universe_miss":
        return "universe_not_seeded"
    if classification == "unscored_mover":
        return "pre_score_history"
    if tier == "Incomplete":
        return "incomplete_fundamentals"

    # Check earnings proximity (within 7 days before or after)
    for earn_date in earnings_dates.get(ticker, []):
        if abs((earn_date - event_date).days) <= 7:
            return "missing_earnings_context"

    # Check sector cluster
    if (ticker, str(event_date), window) in sector_clusters:
        return "sector_cluster_move"

    if not row.get("had_catalyst_evidence"):
        return "no_evidence_no_filing"

    # Score-component-based rules
    components = _best_score_key(ticker, event_date_raw, score_components)
    if components:
        cat = components.get("catalyst_score")
        qual = components.get("quality_score")
        if classification == "near_threshold":
            if cat is not None and cat < 30:
                return "near_threshold_no_catalyst"
            return "near_threshold_scored"
        if cat is not None and cat < 30:
            return "low_catalyst_score"
        if qual is not None and qual < 40:
            return "low_quality_score"

    return "unknown"


def _build_enrichment(label: str) -> dict:
    return {
        "enriched_root_cause":  label,
        "root_cause_group":     _ROOT_CAUSE_GROUPS.get(label, "unknown"),
        "explanation_short":    _EXPLANATIONS.get(label, ""),
        "evidence_fields_used": _EVIDENCE_FIELDS.get(label, ""),
        "suggested_fix":        _SUGGESTED_FIXES.get(label, ""),
        "confidence":           _CONFIDENCE.get(label, "low"),
    }


def enrich_rows(
    rows: list[dict],
    conn: duckdb.DuckDBPyConnection,
) -> list[dict]:
    """Add 6 deterministic enrichment fields to every prediction row.

    Input rows come from build_rows() or are loaded from the prediction CSV.
    Returns a new list — input rows are not mutated.
    """
    score_components = _fetch_score_components(conn)
    earnings_dates   = _fetch_earnings_dates(conn)
    sector_map       = _fetch_sector_map(conn)
    sector_clusters  = _detect_sector_clusters(rows, sector_map)

    enriched = []
    for row in rows:
        label = _assign_root_cause(
            row,
            score_components=score_components,
            earnings_dates=earnings_dates,
            sector_clusters=sector_clusters,
        )
        enriched_row = {**row, **_build_enrichment(label)}
        enriched.append(enriched_row)
    return enriched
```

- [ ] **Step 5: Add `generate_enrichment_report` to `missed/root_cause_enrichment.py`**

Append after `enrich_rows`:

```python
def generate_enrichment_report(
    enriched_rows: list[dict],
    output_dir: str = "data/processed",
) -> tuple[Path, Path]:
    """Write enriched CSV and root-cause summary markdown.

    Returns (csv_path, md_path). Shadow-only — no scores written.
    """
    from datetime import date as date_cls
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = date_cls.today().isoformat()

    # CSV: all original fields + 6 enrichment fields
    if enriched_rows:
        base_cols = [c for c in enriched_rows[0].keys() if c not in _ENRICHMENT_EXTRA_COLS]
    else:
        base_cols = []
    all_cols = base_cols + _ENRICHMENT_EXTRA_COLS

    csv_path = out / _REPORT_ENRICHED_CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched_rows)

    # Markdown summary
    rc_counts = Counter(r["enriched_root_cause"] for r in enriched_rows)
    group_counts = Counter(r["root_cause_group"] for r in enriched_rows)

    lines: list[str] = [
        "# Root Cause Enrichment Report",
        "",
        f"Generated: {today} | Rows enriched: {len(enriched_rows)}",
        "",
        "> **Shadow-only: no production scores were changed.**",
        "",
        "---",
        "",
        "## Root Cause Group Summary",
        "",
        "| Group | Count |",
        "|-------|-------|",
    ]
    for group in ("data_gap", "scoring_gap", "feature_gap", "near_miss", "universe_gap", "unknown"):
        lines.append(f"| `{group}` | {group_counts.get(group, 0)} |")
    lines += [
        "",
        "## Detailed Root Cause Breakdown",
        "",
        "| Root Cause | Count | Suggested Fix |",
        "|------------|-------|---------------|",
    ]
    for label in _ROOT_CAUSE_GROUPS:
        count = rc_counts.get(label, 0)
        if count:
            fix = _SUGGESTED_FIXES.get(label, "")
            lines.append(f"| `{label}` | {count} | {fix} |")

    # Top enriched rows (true_miss + scored_missed + near_threshold, highest priority)
    key_rows = [r for r in enriched_rows if r.get("classification") in
                ("true_miss", "scored_missed", "near_threshold")]
    key_rows.sort(key=lambda r: -r.get("priority_score", 0))
    if key_rows:
        lines += [
            "",
            "## Top Enriched Rows (true_miss / scored_missed / near_threshold)",
            "",
            "| Ticker | Classification | Root Cause | Confidence | Explanation |",
            "|--------|----------------|------------|------------|-------------|",
        ]
        for r in key_rows[:20]:
            lines.append(
                f"| {r['ticker']} | `{r['classification']}` | `{r['enriched_root_cause']}`"
                f" | {r['confidence']} | {r['explanation_short']} |"
            )

    md_path = out / _REPORT_ENRICHED_MD
    md_path.write_text("\n".join(lines) + "\n")
    logger.info("Root-cause enrichment report: %s (%d rows)", md_path, len(enriched_rows))
    return csv_path, md_path
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_root_cause_enrichment.py::test_enrich_rows_attaches_all_six_fields -v 2>&1 | tail -10
```

Expected: PASS

- [ ] **Step 7: Run full test suite to check no regressions**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/ -x -q 2>&1 | tail -15
```

Expected: all existing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add missed/root_cause_enrichment.py tests/test_root_cause_enrichment.py
git commit -m "feat: add root_cause_enrichment module with stub + first test"
```

---

## Task 2: 8 Root-Cause Classification Tests

**Files:**
- Modify: `tests/test_root_cause_enrichment.py`

- [ ] **Step 1: Write the 7 remaining classification tests**

Append to `tests/test_root_cause_enrichment.py` after `test_enrich_rows_attaches_all_six_fields`:

```python
def test_incomplete_tier_gets_incomplete_fundamentals(conn):
    """tier_before_event=Incomplete → enriched_root_cause=incomplete_fundamentals."""
    from missed.root_cause_enrichment import enrich_rows
    row = _row(tier_before_event="Incomplete", classification="true_miss")
    enriched = enrich_rows([row], conn)
    assert enriched[0]["enriched_root_cause"] == "incomplete_fundamentals"
    assert enriched[0]["root_cause_group"] == "data_gap"
    assert enriched[0]["confidence"] == "high"


def test_universe_miss_gets_universe_not_seeded(conn):
    """classification=universe_miss → enriched_root_cause=universe_not_seeded."""
    from missed.root_cause_enrichment import enrich_rows
    row = _row(classification="universe_miss", was_in_universe=False, was_scored=False,
               score_before_event=None, tier_before_event=None)
    enriched = enrich_rows([row], conn)
    assert enriched[0]["enriched_root_cause"] == "universe_not_seeded"
    assert enriched[0]["root_cause_group"] == "universe_gap"


def test_unscored_mover_gets_pre_score_history(conn):
    """classification=unscored_mover → enriched_root_cause=pre_score_history."""
    from missed.root_cause_enrichment import enrich_rows
    row = _row(classification="unscored_mover", was_scored=False,
               score_before_event=None, tier_before_event=None)
    enriched = enrich_rows([row], conn)
    assert enriched[0]["enriched_root_cause"] == "pre_score_history"


def test_low_catalyst_score_triggers_low_catalyst_label(conn):
    """Scored row with catalyst_score < 30 → enriched_root_cause=low_catalyst_score."""
    from missed.root_cause_enrichment import enrich_rows
    score_date = (date.today() - timedelta(days=4)).isoformat()
    event_date = date.today() - timedelta(days=3)
    conn.execute(
        """INSERT INTO scores (id, run_id, ticker, as_of_date,
               catalyst_score, quality_score, momentum_score, cheap_score,
               total_score, tier)
           VALUES (?, ?, 'LOWCAT', ?, 20.0, 60.0, 50.0, 50.0, 38.0, 'Reject')""",
        [uuid.uuid4().hex[:16], uuid.uuid4().hex[:16], score_date],
    )
    row = _row(ticker="LOWCAT", event_date=event_date, classification="true_miss",
               tier_before_event="Reject", had_catalyst_evidence=True)
    enriched = enrich_rows([row], conn)
    assert enriched[0]["enriched_root_cause"] == "low_catalyst_score"
    assert enriched[0]["root_cause_group"] == "scoring_gap"


def test_near_threshold_with_low_catalyst(conn):
    """near_threshold row with catalyst_score < 30 → near_threshold_no_catalyst."""
    from missed.root_cause_enrichment import enrich_rows
    score_date = (date.today() - timedelta(days=4)).isoformat()
    event_date = date.today() - timedelta(days=3)
    conn.execute(
        """INSERT INTO scores (id, run_id, ticker, as_of_date,
               catalyst_score, quality_score, momentum_score, cheap_score,
               total_score, tier)
           VALUES (?, ?, 'NEARLOW', ?, 25.0, 55.0, 50.0, 50.0, 42.0, 'Reject')""",
        [uuid.uuid4().hex[:16], uuid.uuid4().hex[:16], score_date],
    )
    row = _row(ticker="NEARLOW", event_date=event_date, classification="near_threshold",
               score_before_event=42.0, tier_before_event="Reject", had_catalyst_evidence=True)
    enriched = enrich_rows([row], conn)
    assert enriched[0]["enriched_root_cause"] == "near_threshold_no_catalyst"
    assert enriched[0]["root_cause_group"] == "near_miss"


def test_near_threshold_with_high_catalyst(conn):
    """near_threshold row with catalyst_score >= 30 → near_threshold_scored."""
    from missed.root_cause_enrichment import enrich_rows
    score_date = (date.today() - timedelta(days=4)).isoformat()
    event_date = date.today() - timedelta(days=3)
    conn.execute(
        """INSERT INTO scores (id, run_id, ticker, as_of_date,
               catalyst_score, quality_score, momentum_score, cheap_score,
               total_score, tier)
           VALUES (?, ?, 'NEARHIGH', ?, 40.0, 55.0, 50.0, 50.0, 41.5, 'Reject')""",
        [uuid.uuid4().hex[:16], uuid.uuid4().hex[:16], score_date],
    )
    row = _row(ticker="NEARHIGH", event_date=event_date, classification="near_threshold",
               score_before_event=41.5, tier_before_event="Reject", had_catalyst_evidence=True)
    enriched = enrich_rows([row], conn)
    assert enriched[0]["enriched_root_cause"] == "near_threshold_scored"


def test_event_near_earnings_gets_missing_earnings_context(conn):
    """Event within 7 days of earnings event → missing_earnings_context."""
    from missed.root_cause_enrichment import enrich_rows
    event_date = date.today() - timedelta(days=3)
    earnings_date = event_date - timedelta(days=5)
    conn.execute(
        """INSERT INTO events (id, ticker, event_type, event_date)
           VALUES (?, 'EARNCO', 'earnings', ?)""",
        [uuid.uuid4().hex[:16], earnings_date.isoformat()],
    )
    row = _row(ticker="EARNCO", event_date=event_date, classification="true_miss",
               tier_before_event="Reject", had_catalyst_evidence=True)
    enriched = enrich_rows([row], conn)
    assert enriched[0]["enriched_root_cause"] == "missing_earnings_context"
    assert enriched[0]["root_cause_group"] == "feature_gap"


def test_sector_cluster_detected_when_three_peers_move(conn):
    """3+ tickers in same sector, same window, within 3 days → sector_cluster_move."""
    from missed.root_cause_enrichment import enrich_rows
    event_date = date.today() - timedelta(days=3)
    for ticker in ("PEER1", "PEER2", "PEER3"):
        conn.execute(
            """INSERT INTO companies (ticker, company_name, universe_tier, sector, is_active)
               VALUES (?, ?, 'primary', 'Technology', true)
               ON CONFLICT (ticker) DO UPDATE SET sector = 'Technology'""",
            [ticker, ticker],
        )
    rows = [
        _row(ticker="PEER1", event_date=event_date, window_days=5,
             classification="true_miss", tier_before_event="Reject"),
        _row(ticker="PEER2", event_date=event_date, window_days=5,
             classification="true_miss", tier_before_event="Reject"),
        _row(ticker="PEER3", event_date=event_date, window_days=5,
             classification="true_miss", tier_before_event="Reject"),
    ]
    enriched = enrich_rows(rows, conn)
    root_causes = {r["ticker"]: r["enriched_root_cause"] for r in enriched}
    assert root_causes["PEER1"] == "sector_cluster_move", root_causes
    assert root_causes["PEER2"] == "sector_cluster_move", root_causes
    assert root_causes["PEER3"] == "sector_cluster_move", root_causes
```

- [ ] **Step 2: Run all 8 tests to verify they fail predictably**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_root_cause_enrichment.py -v 2>&1 | tail -20
```

Expected: 7 failing tests, 1 passing (`test_enrich_rows_attaches_all_six_fields`).

- [ ] **Step 3: Verify all 8 tests pass after Task 1 implementation**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_root_cause_enrichment.py -v 2>&1 | tail -20
```

Expected: 8 PASSED

- [ ] **Step 4: Commit**

```bash
git add tests/test_root_cause_enrichment.py
git commit -m "test: add 8 root-cause enrichment classification tests"
```

---

## Task 3: CLI Command `missed enrich-root-causes`

**Files:**
- Modify: `main.py` (~line 575, after `missed_prediction_vs_actual`)

- [ ] **Step 1: Write a minimal integration test**

In `tests/test_root_cause_enrichment.py`, append:

```python
def test_generate_enrichment_report_writes_csv_and_md(tmp_path, conn):
    """generate_enrichment_report writes enriched CSV and markdown to output_dir."""
    from missed.root_cause_enrichment import enrich_rows, generate_enrichment_report
    rows = [_row(ticker="REPTEST")]
    enriched = enrich_rows(rows, conn)
    csv_path, md_path = generate_enrichment_report(enriched, output_dir=str(tmp_path))
    assert csv_path.exists(), "Enriched CSV must be written"
    assert md_path.exists(), "Enrichment markdown must be written"
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        from missed.root_cause_enrichment import _ENRICHMENT_EXTRA_COLS
        header = reader.fieldnames or []
        for col in _ENRICHMENT_EXTRA_COLS:
            assert col in header, f"Missing enrichment column: {col}"
    md = md_path.read_text()
    assert "# Root Cause Enrichment Report" in md
    assert "## Root Cause Group Summary" in md
    assert "## Detailed Root Cause Breakdown" in md
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_root_cause_enrichment.py::test_generate_enrichment_report_writes_csv_and_md -v 2>&1 | tail -10
```

Expected: FAIL (`generate_enrichment_report not importable` or missing `csv` import in test).

Note: add `import csv` at the top of `tests/test_root_cause_enrichment.py` alongside other imports.

- [ ] **Step 3: Run test after `generate_enrichment_report` is in place (from Task 1)**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_root_cause_enrichment.py -v 2>&1 | tail -10
```

Expected: all 9 tests PASS.

- [ ] **Step 4: Add CLI command to `main.py`**

In `main.py`, after the `missed_prediction_vs_actual` function (around line 575), insert:

```python
@missed.command("enrich-root-causes")
@click.option(
    "--input", "input_csv",
    default="data/processed/prediction_vs_actual_rows.csv",
    show_default=True,
    help="Path to prediction-vs-actual CSV (output of 'missed prediction-vs-actual').",
)
@click.option(
    "--output-dir", default="data/processed", show_default=True,
    help="Directory for enriched CSV and markdown report.",
)
def missed_enrich_root_causes(input_csv, output_dir):
    """Deterministic root-cause enrichment for prediction-vs-actual rows.

    Reads the prediction CSV, joins DB tables (scores components, fundamentals,
    events, companies), assigns 11 structured root-cause labels, and writes
    two artifacts: an enriched CSV and a markdown summary report.
    No LLM, no new data sources, no production scores changed.
    """
    import csv as csv_mod
    from datetime import date
    from missed.root_cause_enrichment import enrich_rows, generate_enrichment_report

    input_path = Path(input_csv)
    if not input_path.exists():
        raise click.ClickException(
            f"Input CSV not found: {input_csv}\n"
            "Run 'missed prediction-vs-actual' first to generate it."
        )

    with open(input_path, newline="") as f:
        reader = csv_mod.DictReader(f)
        raw_rows = list(reader)

    # Coerce types that were serialised as strings in the CSV
    for r in raw_rows:
        for numeric_field in ("return_value", "score_before_event", "priority_score"):
            if r.get(numeric_field) not in (None, "", "None"):
                try:
                    r[numeric_field] = float(r[numeric_field])
                except ValueError:
                    r[numeric_field] = None
            else:
                r[numeric_field] = None
        for bool_field in ("was_in_universe", "was_scored", "had_catalyst_evidence"):
            r[bool_field] = r.get(bool_field, "").lower() in ("true", "1", "yes")
        if r.get("event_date") not in (None, "", "None"):
            r["event_date"] = date.fromisoformat(r["event_date"])
        if r.get("window_days") not in (None, "", "None"):
            try:
                r["window_days"] = int(r["window_days"])
            except ValueError:
                r["window_days"] = None

    cfg, conn = _engine_setup()
    try:
        enriched = enrich_rows(raw_rows, conn)
        csv_path, md_path = generate_enrichment_report(enriched, output_dir=output_dir)
        click.echo("Root-cause enrichment report written:")
        click.echo(f"  Enriched CSV: {csv_path}")
        click.echo(f"  Markdown:     {md_path}")
        click.echo(f"  Rows enriched: {len(enriched)}")
    finally:
        conn.close()
```

Also add `from pathlib import Path` to the import at the top of the command if not already present — check if `Path` is already imported at module level (it is via `from pathlib import Path` in `main.py`).

- [ ] **Step 5: Smoke-test the CLI command end-to-end**

First generate the input CSV (if not already present):

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py missed prediction-vs-actual 2>&1 | tail -5
```

Then run enrichment:

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py missed enrich-root-causes 2>&1
```

Expected output:
```
Root-cause enrichment report written:
  Enriched CSV: data/processed/prediction_vs_actual_enriched_rows.csv
  Markdown:     data/processed/root_cause_enrichment_report.md
  Rows enriched: <N>
```

Verify markdown is non-empty and contains the expected sections:

```bash
grep "## Root Cause" data/processed/root_cause_enrichment_report.md
```

Expected: 3 lines containing the 3 section headings.

- [ ] **Step 6: Run full test suite**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/ -x -q 2>&1 | tail -15
```

Expected: all tests pass (count ≥ previous + 9 new).

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_root_cause_enrichment.py
git commit -m "feat: add missed enrich-root-causes CLI command"
```

---

## Task 4: Root Cause Summary Section in Prediction Report

**Files:**
- Modify: `missed/prediction_report.py`
- Modify: `tests/test_prediction_report.py`

- [ ] **Step 1: Update `test_report_contains_required_sections` to expect new heading**

In `tests/test_prediction_report.py`, find `test_report_contains_required_sections` and add the new heading to the `required` list:

```python
    required = [
        "# Prediction vs Actual Spike Report",
        "## Summary",
        "## 1-Day Spikes",
        "## 3d / 5d Spikes",
        "## Longer Windows (10d / 20d / 60d)",
        "## 52-Week Breakouts",
        "## Out-of-Universe Spikes",
        "## Near-Threshold Scores",
        "## No-Score Events",
        "## Root Cause Summary",  # NEW
    ]
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_prediction_report.py::test_report_contains_required_sections -v 2>&1 | tail -10
```

Expected: FAIL with `Missing section heading: '## Root Cause Summary'`

- [ ] **Step 3: Update `generate_prediction_report` in `missed/prediction_report.py`**

Add import at top of file (after existing imports):

```python
from missed.root_cause_enrichment import enrich_rows, _ROOT_CAUSE_GROUPS
```

In `generate_prediction_report`, after `rows = build_rows(conn, lookback_days=lookback_days)`, add:

```python
    enriched_rows = enrich_rows(rows, conn)
```

Replace subsequent references to `rows` in the report body with `enriched_rows` so the enrichment data is available.

Then, after the `_section_lines("No-Score Events", ...)` call and before `md_path = out / _REPORT_MD`, append the Root Cause Summary section:

```python
    from collections import Counter as _Counter
    rc_counts = _Counter(r.get("enriched_root_cause", "unknown") for r in enriched_rows)
    group_counts = _Counter(r.get("root_cause_group", "unknown") for r in enriched_rows)

    lines += [
        "---",
        "",
        "## Root Cause Summary",
        "",
        "| Root Cause Group | Count |",
        "|------------------|-------|",
    ]
    for group in ("data_gap", "scoring_gap", "feature_gap", "near_miss", "universe_gap", "unknown"):
        lines.append(f"| `{group}` | {group_counts.get(group, 0)} |")
    lines += [
        "",
        "| Root Cause | Count |",
        "|------------|-------|",
    ]
    for label in _ROOT_CAUSE_GROUPS:
        count = rc_counts.get(label, 0)
        if count:
            lines.append(f"| `{label}` | {count} |")
    lines.append("")
```

Also update the JSONL writer to use `enriched_rows` so the enrichment fields are included in the JSONL output:

```python
    jsonl_path = out / _REPORT_JSONL
    with open(jsonl_path, "w") as f:
        for r in enriched_rows:
            f.write(json.dumps(r, default=str) + "\n")
```

And update the CSV writer to use `enriched_rows` (the CSV writer uses `extrasaction="ignore"`, so the 6 extra enrichment fields will be silently dropped from the existing `_CSV_COLS`-filtered CSV — this is fine since the enriched CSV is the dedicated artifact).

- [ ] **Step 4: Run the updated prediction report test**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_prediction_report.py -v 2>&1 | tail -15
```

Expected: all 12 prediction report tests PASS.

- [ ] **Step 5: Run all tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/ -x -q 2>&1 | tail -15
```

Expected: all tests pass (≥ previous + 10 new).

- [ ] **Step 6: Smoke-test full pipeline end-to-end**

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py missed prediction-vs-actual 2>&1
```

Verify Root Cause Summary appears in the markdown:

```bash
grep -A 20 "## Root Cause Summary" data/processed/prediction_vs_actual_report.md
```

Expected: table with group counts visible.

- [ ] **Step 7: Commit**

```bash
git add missed/prediction_report.py tests/test_prediction_report.py
git commit -m "feat: embed root cause summary in prediction-vs-actual report"
```

---

## Self-Review

### Spec coverage

| Requirement | Task |
|-------------|------|
| `missed/root_cause_enrichment.py` module | Task 1 |
| 6 enrichment fields per row | Task 1, Step 3-4 |
| 11 root-cause labels, deterministic | Task 1 (constants + `_assign_root_cause`) |
| `prediction_vs_actual_enriched_rows.csv` artifact | Task 1 Step 5 |
| `root_cause_enrichment_report.md` artifact | Task 1 Step 5 |
| CLI `missed enrich-root-causes --input --output-dir` | Task 3 Step 4 |
| Root Cause Summary section in prediction-vs-actual report | Task 4 Step 3 |
| 8 classification tests | Task 2 (8 tests) + integration test in Task 3 |
| No LLM, no new data sources | All tasks — only existing DB tables |
| No production score mutations | `enrich_rows` and report functions are read-only |

### Placeholder scan

No "TBD", "TODO", or incomplete steps. All SQL, Python, and CLI commands are fully specified.

### Type consistency

- `enrich_rows(rows: list[dict], conn: duckdb.DuckDBPyConnection) -> list[dict]` — consistent across Task 1, 3, 4.
- `generate_enrichment_report(enriched_rows: list[dict], output_dir: str) -> tuple[Path, Path]` — consistent across Task 1 and Task 3.
- `_ENRICHMENT_EXTRA_COLS` referenced in Task 3 test matches the constant defined in Task 1.
- `_ROOT_CAUSE_GROUPS` imported from `missed.root_cause_enrichment` in Task 4 — consistent with Task 1 definition.

---

_All data from live DB at 2026-05-03. No migrations required — enrichment uses existing tables only._
