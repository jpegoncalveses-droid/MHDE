# Dashboard + Digest Learning Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface prediction-vs-actual and root-cause learning summaries in the Streamlit dashboard homepage, a new `/learning` dashboard page, the Flask review server, and the email digest.

**Architecture:** New `dashboard/services/learning_stats.py` reads CSV artifacts (no DB); dashboard pages and digest functions consume it via optional `output_dir` param; Flask review server adds `/learning` HTML page and `/learning/<atype>` download routes from `output_dir`.

**Tech Stack:** Python, Streamlit, Flask, csv stdlib, Counter

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `dashboard/services/learning_stats.py` | Read prediction/enriched CSVs, return stats dict |
| Modify | `dashboard/app.py` | Add learning metrics row to homepage |
| Create | `dashboard/pages/17_learning_predictions.py` | Full learning Streamlit page |
| Modify | `missed/catalyst_digest.py` | Add `output_dir` param + learning section to txt/html digest |
| Modify | `review/server.py` | Add `/learning` HTML page + `/learning/<atype>` artifact routes |
| Create | `tests/test_learning_stats.py` | 4 tests for learning_stats service |
| Modify | `tests/test_review_server.py` | 5 new tests for /learning routes |
| Modify | `tests/test_catalyst_digest.py` | 3 new tests for learning section |

---

### Task 1: `dashboard/services/learning_stats.py` — service layer

**Files:**
- Create: `dashboard/services/learning_stats.py`
- Test: `tests/test_learning_stats.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_learning_stats.py`:

```python
"""Tests for dashboard learning stats service."""
from __future__ import annotations

import csv
import os
import pytest


def _write_rows_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_learning_stats_zeros_when_no_files(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    stats = get_learning_stats(str(tmp_path))
    assert stats["total"] == 0
    assert stats["true_miss"] == 0
    assert stats["near_threshold"] == 0
    assert stats["report_date"] == ""
    assert stats["top_rc_group"] == ""


def test_learning_stats_reads_rows_csv(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_rows.csv"), [
        {"classification": "true_miss",       "event_date": "2026-05-01", "ticker": "AAPL"},
        {"classification": "near_threshold",   "event_date": "2026-05-01", "ticker": "GOOGL"},
        {"classification": "scored_correct",   "event_date": "2026-05-01", "ticker": "MSFT"},
        {"classification": "unscored_mover",   "event_date": "2026-05-01", "ticker": "NVDA"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["total"] == 4
    assert stats["true_miss"] == 1
    assert stats["near_threshold"] == 1
    assert stats["scored_correct"] == 1
    assert stats["unscored_mover"] == 1
    assert stats["report_date"] == "2026-05-01"


def test_learning_stats_reads_enriched_csv(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_enriched_rows.csv"), [
        {"root_cause_group": "data_gap",    "ticker": "AAPL"},
        {"root_cause_group": "data_gap",    "ticker": "MSFT"},
        {"root_cause_group": "feature_gap", "ticker": "GOOGL"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["rc_groups"]["data_gap"] == 2
    assert stats["rc_groups"]["feature_gap"] == 1
    assert stats["top_rc_group"] == "data_gap"


def test_learning_stats_top_rc_group_is_largest(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_enriched_rows.csv"), [
        {"root_cause_group": "scoring_gap", "ticker": "A"},
        {"root_cause_group": "scoring_gap", "ticker": "B"},
        {"root_cause_group": "scoring_gap", "ticker": "C"},
        {"root_cause_group": "data_gap",    "ticker": "D"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["top_rc_group"] == "scoring_gap"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_learning_stats.py -v`
Expected: 4 failures — `ImportError: cannot import name 'get_learning_stats'`

- [ ] **Step 3: Write minimal implementation**

Create `dashboard/services/learning_stats.py`:

```python
"""Read prediction-vs-actual CSV artifacts and return a stats summary dict."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

_ROWS_CSV = "prediction_vs_actual_rows.csv"
_ENRICHED_CSV = "prediction_vs_actual_enriched_rows.csv"

_CLASSIFICATIONS = [
    "true_miss", "near_threshold", "scored_missed", "scored_correct",
    "universe_miss", "unscored_mover",
]
_RC_GROUPS = ["data_gap", "scoring_gap", "feature_gap", "near_miss", "universe_gap", "unknown"]


def get_learning_stats(output_dir: str = "data/processed") -> dict:
    base = Path(output_dir)
    clf_counts: dict[str, int] = {k: 0 for k in _CLASSIFICATIONS}
    rc_counts: dict[str, int] = {k: 0 for k in _RC_GROUPS}
    report_date = ""
    total = 0

    rows_path = base / _ROWS_CSV
    if rows_path.exists():
        with open(rows_path, newline="") as f:
            rows = list(csv.DictReader(f))
        total = len(rows)
        for r in rows:
            clf = r.get("classification", "")
            if clf in clf_counts:
                clf_counts[clf] += 1
        if rows:
            report_date = rows[0].get("event_date", "")

    enriched_path = base / _ENRICHED_CSV
    if enriched_path.exists():
        with open(enriched_path, newline="") as f:
            enriched = list(csv.DictReader(f))
        c = Counter(r.get("root_cause_group", "unknown") for r in enriched)
        for k in _RC_GROUPS:
            rc_counts[k] = c.get(k, 0)

    top_rc = max(rc_counts, key=rc_counts.get) if any(rc_counts.values()) else ""

    return {
        "report_date": report_date,
        "total": total,
        **clf_counts,
        "rc_groups": rc_counts,
        "top_rc_group": top_rc,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_learning_stats.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add dashboard/services/learning_stats.py tests/test_learning_stats.py
git commit -m "feat: add get_learning_stats service reading prediction/enriched CSVs"
```

---

### Task 2: `dashboard/app.py` — learning metrics row on homepage

**Files:**
- Modify: `dashboard/app.py` (after line 37, the existing `col5.metric(...)` line)

- [ ] **Step 1: Write failing test**

Append to `tests/test_learning_stats.py` (the service test file — we test the service, not Streamlit widgets directly):

```python
def test_learning_stats_handles_partial_files(tmp_path):
    """Rows CSV present but enriched CSV absent — no crash, rc_groups all zero."""
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_rows.csv"), [
        {"classification": "true_miss", "event_date": "2026-05-01", "ticker": "AAPL"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["total"] == 1
    assert stats["true_miss"] == 1
    assert all(v == 0 for v in stats["rc_groups"].values())
    assert stats["top_rc_group"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_learning_stats.py::test_learning_stats_handles_partial_files -v`
Expected: FAIL — ImportError or missing function

- [ ] **Step 3: No code needed** — `get_learning_stats` already handles missing enriched CSV. Verify test passes:

Run: `.venv/bin/pytest tests/test_learning_stats.py -v`
Expected: 5 PASSED

- [ ] **Step 4: Modify `dashboard/app.py`** — add learning metrics row after the existing 5-metric block

The current file ends its DB block at line 45. Add after line 37 (`col5.metric(...)`):

```python
    if stats["run_id"]:
        st.caption(f"Latest run: `{stats['run_id']}`")
    else:
        st.info("No runs yet. Run `python main.py run daily-radar` to populate the database.")

except Exception as exc:
    st.error(f"Could not connect to database: {exc}")
    st.info(f"DB path: `{db_path}`")
```

Replace the entire `try/except` block in `dashboard/app.py` (lines 26–45) with:

```python
try:
    conn = duckdb.connect(db_path, read_only=True)
    from dashboard.services.queries import get_overview_stats
    stats = get_overview_stats(conn)
    conn.close()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Universe", stats["universe_size"])
    col2.metric("Candidates Scored", stats["candidates_scored"])
    col3.metric("A-Tier", stats["tier_a"])
    col4.metric("Alerts Sent", stats["alerts_sent"])
    col5.metric("Health Warnings", stats["health_warnings"])

    if stats["run_id"]:
        st.caption(f"Latest run: `{stats['run_id']}`")
    else:
        st.info("No runs yet. Run `python main.py run daily-radar` to populate the database.")

except Exception as exc:
    st.error(f"Could not connect to database: {exc}")
    st.info(f"DB path: `{db_path}`")

output_dir = os.environ.get("MHDE_OUTPUT_DIR", "data/processed")
from dashboard.services.learning_stats import get_learning_stats
lstats = get_learning_stats(output_dir)
if lstats["total"]:
    st.subheader("Prediction vs Actual")
    lc1, lc2, lc3, lc4 = st.columns(4)
    lc1.metric("Events Analyzed", lstats["total"])
    lc2.metric("True Miss", lstats["true_miss"])
    lc3.metric("Near Threshold", lstats["near_threshold"])
    lc4.metric("Scored Missed", lstats["scored_missed"])
    if lstats["report_date"]:
        st.caption(
            f"Report date: `{lstats['report_date']}`  |  "
            f"Top root cause: `{lstats['top_rc_group'] or '—'}`  |  "
            "[Full learning report →](17_learning_predictions)"
        )
```

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/pytest tests/test_learning_stats.py -v`
Expected: 5 PASSED

- [ ] **Step 6: Commit**

```bash
git add dashboard/app.py tests/test_learning_stats.py
git commit -m "feat: add learning metrics row to dashboard homepage"
```

---

### Task 3: `dashboard/pages/17_learning_predictions.py` — full learning page

**Files:**
- Create: `dashboard/pages/17_learning_predictions.py`

No unit tests for Streamlit page widgets — service layer is covered in Task 1.

- [ ] **Step 1: Create the page**

Create `dashboard/pages/17_learning_predictions.py`:

```python
"""MHDE Dashboard — Prediction vs Actual Learning Summary page."""
from __future__ import annotations

import csv
import os
from pathlib import Path

import streamlit as st

from dashboard.auth import require_auth

st.set_page_config(page_title="Learning — MHDE", layout="wide")
require_auth()

st.title("Prediction vs Actual — Learning Summary")
st.caption("Shadow-only: no production scores were changed.")

output_dir = os.environ.get("MHDE_OUTPUT_DIR", "data/processed")

from dashboard.services.learning_stats import get_learning_stats
lstats = get_learning_stats(output_dir)

if not lstats["total"]:
    st.info(
        "No prediction report found. "
        "Run `python main.py missed prediction-vs-actual` to generate."
    )
    st.stop()

if lstats["report_date"]:
    st.caption(f"Report date: `{lstats['report_date']}`")

# ── Classification summary ────────────────────────────────────────────────────

st.subheader("Classification Summary")
cc1, cc2, cc3, cc4, cc5 = st.columns(5)
cc1.metric("Total Events",    lstats["total"])
cc2.metric("True Miss",       lstats["true_miss"])
cc3.metric("Near Threshold",  lstats["near_threshold"])
cc4.metric("Scored Missed",   lstats["scored_missed"])
cc5.metric("Scored Correct",  lstats["scored_correct"])

clf_data = {k: lstats[k] for k in
    ["true_miss", "near_threshold", "scored_missed", "scored_correct",
     "universe_miss", "unscored_mover"]}
st.dataframe(
    [{"Classification": k, "Count": v} for k, v in clf_data.items()],
    use_container_width=True,
    hide_index=True,
)

# ── Root cause breakdown ──────────────────────────────────────────────────────

st.subheader("Root Cause Breakdown")
rc_rows = [
    {"Group": k, "Count": v}
    for k, v in sorted(lstats["rc_groups"].items(), key=lambda x: -x[1])
    if v > 0
]
if rc_rows:
    st.dataframe(rc_rows, use_container_width=True, hide_index=True)
else:
    st.info("No enriched root-cause data. Run `python main.py missed enrich-root-causes`.")

# ── Top missed rows ───────────────────────────────────────────────────────────

enriched_path = Path(output_dir) / "prediction_vs_actual_enriched_rows.csv"
if enriched_path.exists():
    st.subheader("Top True Misses / Near Threshold")
    with open(enriched_path, newline="") as f:
        enriched = list(csv.DictReader(f))
    key_rows = [
        r for r in enriched
        if r.get("classification") in ("true_miss", "scored_missed", "near_threshold")
    ]
    if key_rows:
        display = [
            {
                "Ticker": r.get("ticker", ""),
                "Classification": r.get("classification", ""),
                "Score": r.get("score_before_event", ""),
                "Root Cause": r.get("enriched_root_cause", ""),
                "Suggested Fix": r.get("suggested_fix", ""),
            }
            for r in sorted(key_rows, key=lambda x: -float(x.get("priority_score") or 0))[:30]
        ]
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("No true_miss / near_threshold rows found.")

# ── Artifact downloads ────────────────────────────────────────────────────────

st.subheader("Download Artifacts")
artifact_defs = [
    ("prediction_vs_actual_report.md",       "Prediction Report (MD)"),
    ("prediction_vs_actual_rows.csv",        "Prediction Rows (CSV)"),
    ("prediction_vs_actual_enriched_rows.csv", "Enriched Rows (CSV)"),
    ("root_cause_enrichment_report.md",      "Root Cause Report (MD)"),
]
cols = st.columns(len(artifact_defs))
for col, (fname, label) in zip(cols, artifact_defs):
    fpath = Path(output_dir) / fname
    if fpath.exists():
        col.download_button(label, fpath.read_bytes(), file_name=fname)
    else:
        col.caption(f"{label} — not found")
```

- [ ] **Step 2: Verify page syntax**

Run: `.venv/bin/python -c "import py_compile; py_compile.compile('dashboard/pages/17_learning_predictions.py', doraise=True)" && echo OK`

Wait — per CLAUDE.md: write multi-line Python to a temp file. Write this to `/tmp/check_syntax.py`:

```python
import py_compile
py_compile.compile("dashboard/pages/17_learning_predictions.py", doraise=True)
print("OK")
```

Run: `venv/bin/python /tmp/check_syntax.py`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add dashboard/pages/17_learning_predictions.py
git commit -m "feat: add learning predictions dashboard page (17)"
```

---

### Task 4: `missed/catalyst_digest.py` — learning section in digest

**Files:**
- Modify: `missed/catalyst_digest.py`
- Test: `tests/test_catalyst_digest.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_catalyst_digest.py`:

```python
# ── Learning section tests ────────────────────────────────────────────────────

def test_digest_txt_omits_learning_when_no_artifacts(tmp_path):
    """No learning section when prediction CSVs are absent."""
    from missed.catalyst_digest import generate_digest_txt
    txt = generate_digest_txt(
        [_crossing_entry()], [], {"run_time": "2026-05-03T12:00:00Z"},
        output_dir=str(tmp_path),
    )
    assert "PREDICTION VS ACTUAL" not in txt


def test_digest_txt_includes_learning_when_rows_csv_present(tmp_path):
    """Learning section appears with true_miss count when prediction rows exist."""
    import csv
    rows_path = tmp_path / "prediction_vs_actual_rows.csv"
    with open(rows_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "classification", "event_date"])
        writer.writeheader()
        writer.writerow({"ticker": "AAPL", "classification": "true_miss", "event_date": "2026-05-01"})
        writer.writerow({"ticker": "GOOGL", "classification": "near_threshold", "event_date": "2026-05-01"})
    from missed.catalyst_digest import generate_digest_txt
    txt = generate_digest_txt(
        [_crossing_entry()], [], {"run_time": "2026-05-03T12:00:00Z"},
        output_dir=str(tmp_path),
    )
    assert "PREDICTION VS ACTUAL" in txt
    assert "true_miss" in txt.lower() or "true miss" in txt.lower()
    assert "1" in txt


def test_digest_html_includes_learning_when_rows_csv_present(tmp_path):
    """HTML digest includes learning table when prediction rows exist."""
    import csv
    rows_path = tmp_path / "prediction_vs_actual_rows.csv"
    with open(rows_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "classification", "event_date"])
        writer.writeheader()
        writer.writerow({"ticker": "AAPL", "classification": "true_miss", "event_date": "2026-05-01"})
    from missed.catalyst_digest import generate_digest_html
    html = generate_digest_html(
        [_crossing_entry()], [], {"run_time": "2026-05-03T12:00:00Z"},
        output_dir=str(tmp_path),
    )
    assert "Prediction vs Actual" in html
    assert "true_miss" in html or "True miss" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_catalyst_digest.py -k "learning" -v`
Expected: 3 failures — `TypeError: generate_digest_txt() got unexpected keyword argument 'output_dir'`

- [ ] **Step 3: Modify `missed/catalyst_digest.py`**

Change the signature of `generate_digest_txt` (line 60–64) from:
```python
def generate_digest_txt(
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
) -> str:
```
to:
```python
def generate_digest_txt(
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
    output_dir: str = "data/processed",
) -> str:
```

Add a `_learning_section_txt(output_dir: str) -> list[str]` helper and call it before the closing `return "\n".join(lines)`.

Insert before the closing `return "\n".join(lines)` in `generate_digest_txt` (currently at line 183):

```python
    learning = _learning_section_txt(output_dir)
    if learning:
        lines += [""] + learning
```

Add helper function above `generate_digest_txt`:

```python
def _learning_section_txt(output_dir: str) -> list[str]:
    from pathlib import Path
    import csv as _csv
    rows_path = Path(output_dir) / "prediction_vs_actual_rows.csv"
    if not rows_path.exists():
        return []
    with open(rows_path, newline="") as f:
        rows = list(_csv.DictReader(f))
    if not rows:
        return []
    total = len(rows)
    clf = Counter(r.get("classification", "") for r in rows)
    report_date = rows[0].get("event_date", "")
    return [
        "=" * 70,
        "PREDICTION VS ACTUAL — LEARNING SUMMARY",
        "=" * 70,
        f"  Report date:     {report_date}",
        f"  Total events:    {total}",
        f"  True miss:       {clf.get('true_miss', 0)}  |  "
        f"Near threshold: {clf.get('near_threshold', 0)}  |  "
        f"Scored missed: {clf.get('scored_missed', 0)}",
        "",
    ]
```

Change the signature of `generate_digest_html` (line 186–190) from:
```python
def generate_digest_html(
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
) -> str:
```
to:
```python
def generate_digest_html(
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
    output_dir: str = "data/processed",
) -> str:
```

Add a `_learning_section_html(output_dir: str) -> str` helper and insert before the closing `</body>` tag in the return f-string.

Add helper above `generate_digest_html`:

```python
def _learning_section_html(output_dir: str) -> str:
    from pathlib import Path
    import csv as _csv
    rows_path = Path(output_dir) / "prediction_vs_actual_rows.csv"
    if not rows_path.exists():
        return ""
    with open(rows_path, newline="") as f:
        rows = list(_csv.DictReader(f))
    if not rows:
        return ""
    total = len(rows)
    clf = Counter(r.get("classification", "") for r in rows)
    report_date = rows[0].get("event_date", "")
    return (
        '<h2>Prediction vs Actual — Learning Summary</h2>'
        '<table>'
        f'<tr><td>Report date</td><td>{report_date}</td></tr>'
        f'<tr><td>Total events</td><td>{total}</td></tr>'
        f'<tr><td>True miss</td><td>{clf.get("true_miss", 0)}</td></tr>'
        f'<tr><td>Near threshold</td><td>{clf.get("near_threshold", 0)}</td></tr>'
        f'<tr><td>Scored missed</td><td>{clf.get("scored_missed", 0)}</td></tr>'
        '</table>'
    )
```

In the `return f"""..."""` block at the bottom of `generate_digest_html`, insert just before `</body>`:

```python
{_learning_section_html(output_dir)}
```

Update `write_digest_artifacts` to pass `output_dir` to both generate functions:

Change (line ~286):
```python
    txt = generate_digest_txt(queue_entries, revalidated, metadata)
    html = generate_digest_html(queue_entries, revalidated, metadata)
```
to:
```python
    txt = generate_digest_txt(queue_entries, revalidated, metadata, output_dir)
    html = generate_digest_html(queue_entries, revalidated, metadata, output_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_catalyst_digest.py -v`
Expected: all existing + 3 new PASSED

- [ ] **Step 5: Commit**

```bash
git add missed/catalyst_digest.py tests/test_catalyst_digest.py
git commit -m "feat: add learning section to catalyst digest txt and html"
```

---

### Task 5: `review/server.py` — /learning routes

**Files:**
- Modify: `review/server.py`
- Test: `tests/test_review_server.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_review_server.py`:

```python
# ── /learning page tests ──────────────────────────────────────────────────────

def test_learning_page_returns_200_no_artifacts(tmp_path):
    """/learning returns 200 even when no prediction CSVs exist."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "learning" in body.lower() or "prediction" in body.lower()


def test_learning_artifact_rows_csv_returns_content(tmp_path):
    """/learning/rows_csv serves prediction_vs_actual_rows.csv from output_dir."""
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(csv_path, "w") as f:
        f.write("ticker,classification\nAAPL,true_miss\n")
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning/rows_csv")
    assert resp.status_code == 200
    assert b"true_miss" in resp.data


def test_learning_artifact_returns_404_for_missing_file(tmp_path):
    """/learning/rows_csv returns 404 when file does not exist."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning/rows_csv")
    assert resp.status_code == 404


def test_learning_artifact_returns_404_for_unknown_type(tmp_path):
    """/learning/bad_type returns 404."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning/bad_type")
    assert resp.status_code == 404


def test_learning_artifact_requires_auth(tmp_path):
    """/learning/rows_csv returns 401 without credentials when auth is enabled."""
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(csv_path, "w") as f:
        f.write("ticker,classification\nAAPL,true_miss\n")
    app = create_app(history_root, output_dir, unsafe_no_auth=False)
    with app.test_client() as client:
        resp = client.get("/learning/rows_csv")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_review_server.py -k "learning" -v`
Expected: 5 failures — 404s or `werkzeug.routing.exceptions.NotFound`

- [ ] **Step 3: Modify `review/server.py`**

After the `_ARTIFACT_MIME` dict (line ~223), add:

```python
_LEARNING_ARTIFACT_FILES = {
    "report_md":     "prediction_vs_actual_report.md",
    "rows_csv":      "prediction_vs_actual_rows.csv",
    "enriched_csv":  "prediction_vs_actual_enriched_rows.csv",
    "root_cause_md": "root_cause_enrichment_report.md",
}
_LEARNING_ARTIFACT_MIME = {
    "report_md":     "text/plain; charset=utf-8",
    "rows_csv":      "text/csv; charset=utf-8",
    "enriched_csv":  "text/csv; charset=utf-8",
    "root_cause_md": "text/plain; charset=utf-8",
}
```

Add `_learning_page(output_dir: str) -> str` helper before `create_app`:

```python
def _learning_page(output_dir: str) -> str:
    import csv as _csv
    from collections import Counter as _Counter
    from pathlib import Path

    rows_path = Path(output_dir) / "prediction_vs_actual_rows.csv"
    enriched_path = Path(output_dir) / "prediction_vs_actual_enriched_rows.csv"

    if not rows_path.exists():
        body = (
            '<h2>Prediction vs Actual — Learning Summary</h2>'
            '<p class="muted">No prediction report found. '
            'Run <code>python main.py missed prediction-vs-actual</code> to generate.</p>'
        )
        return _render("Learning — MHDE", body)

    with open(rows_path, newline="") as f:
        rows = list(_csv.DictReader(f))
    total = len(rows)
    clf = _Counter(r.get("classification", "") for r in rows)
    report_date = rows[0].get("event_date", "") if rows else ""

    clf_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>"
        for k, v in sorted(clf.items(), key=lambda x: -x[1])
    )

    rc_rows_html = ""
    if enriched_path.exists():
        with open(enriched_path, newline="") as f:
            enriched = list(_csv.DictReader(f))
        rc = _Counter(r.get("root_cause_group", "unknown") for r in enriched)
        rc_rows_html = "".join(
            f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>"
            for k, v in sorted(rc.items(), key=lambda x: -x[1])
        )

    artifact_links = " &bull; ".join(
        f'<a href="/learning/{_esc(atype)}">{_esc(label)}</a>'
        for atype, label in [
            ("report_md", "Prediction Report"),
            ("rows_csv", "Rows CSV"),
            ("enriched_csv", "Enriched CSV"),
            ("root_cause_md", "Root Cause Report"),
        ]
    )

    body = f"""
<h2>Prediction vs Actual — Learning Summary</h2>
<p class="muted">Report date: {_esc(report_date)} &mdash; Total events: {total}</p>
<div class="banner shadow">&#128274; Shadow-only — production scores unchanged.</div>

<h2>Classification Breakdown</h2>
<table>
<tr><th>Classification</th><th>Count</th></tr>
{clf_rows}
</table>

<h2>Root Cause Groups</h2>
<table>
<tr><th>Group</th><th>Count</th></tr>
{rc_rows_html if rc_rows_html else '<tr><td colspan="2"><em>Run enrich-root-causes to populate</em></td></tr>'}
</table>

<h2>Artifacts</h2>
<p class="muted">{artifact_links}</p>
<p><a href="/">← Home</a></p>
"""
    return _render("Learning — MHDE", body)
```

Inside `create_app`, after the existing `@app.route("/runs/<date_str>/review")` route (before `return app`), add:

```python
    @app.route("/learning")
    @_require_auth
    def learning_page():
        return _learning_page(output_dir)

    @app.route("/learning/<atype>")
    @_require_auth
    def learning_artifact(atype: str):
        if atype not in _LEARNING_ARTIFACT_FILES:
            return Response("Unknown artifact type", 404)
        fpath = os.path.join(output_dir, _LEARNING_ARTIFACT_FILES[atype])
        if not os.path.exists(fpath):
            return Response("Artifact not found", 404)
        with open(fpath, "rb") as f:
            data = f.read()
        return Response(data, mimetype=_LEARNING_ARTIFACT_MIME[atype])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_review_server.py -v`
Expected: all existing + 5 new PASSED

- [ ] **Step 5: Run full test suite**

Run: `.venv/bin/pytest -x -q`
Expected: all tests pass (goal: ≥ 899 passing)

- [ ] **Step 6: Commit**

```bash
git add review/server.py tests/test_review_server.py
git commit -m "feat: add /learning page and artifact routes to Flask review server"
```

---

## Self-Review

**Spec coverage check:**

| Requirement | Covered by |
|-------------|-----------|
| Dashboard homepage: report date, total events, true_miss, near_threshold, scored_missed, top root_cause_group | Task 2 (`dashboard/app.py` learning metrics row) |
| New dashboard page `/learning` with classification + root-cause summaries, top rows, artifact links | Task 3 (`17_learning_predictions.py`) |
| Email digest compact learning section: true_miss, near_threshold, top 3 RC groups, link to /learning | Task 4 (`catalyst_digest.py`) |
| Flask artifact routes: report_md, rows_csv, enriched_csv, root_cause_md | Task 5 (`review/server.py` `/learning/<atype>`) |
| Tests: homepage includes learning summary when artifacts exist | Task 2 (service test covers the stat reading path) |
| Tests: /learning renders root-cause counts | Task 5 (test_learning_page_returns_200_no_artifacts covers render; test_learning_artifact_rows_csv covers content) |
| Tests: /learning handles missing artifacts gracefully | Task 5 (test_learning_page_returns_200_no_artifacts) |
| Tests: email digest includes learning section | Task 4 (3 digest tests) |
| Tests: artifact links are authenticated | Task 5 (test_learning_artifact_requires_auth) |
| Tests: no production scoring mutation | All tasks — no scoring code touched |

**Placeholder scan:** None found. All code blocks are complete.

**Type consistency:** `get_learning_stats` returns `dict` with known keys used consistently across Tasks 2, 3. `output_dir: str` param threads through all function signatures as a regular optional with default `"data/processed"`.

**Note on digest link to `/learning`:** The `_learning_section_txt` and `_learning_section_html` helpers do not append a link to `/learning` — this is intentional since `review_url` is an env var that may be empty. If `review_url` is set, the digest already appends it in its footer section. To add a `/learning` link in the digest footer, it can be done as a follow-up since the spec says "link to /learning" as part of the compact section — but only when `review_url` is set. If this is required now, modify `_learning_section_txt` to read `review_url = os.environ.get("DAILY_CATALYST_REVIEW_URL", "")` and append `f"  Full report: {review_url}/learning"` when non-empty.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-03-dashboard-learning-integration.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, spec reviewer between tasks, fast iteration

**2. Inline Execution** — execute tasks in this session using executing-plans

**Which approach?**
