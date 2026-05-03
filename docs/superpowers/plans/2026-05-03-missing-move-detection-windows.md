# Missing Move Detection Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 1d, 3d, and 10d spike-detection windows to the missed-opportunity detector so MHDE can surface both single-day spikes and short rolling accumulation moves.

**Architecture:** Two files grow: `missed/labels.py` gets three new threshold constants and three new event-type strings; `missed/detector.py` imports those constants and adds three `_detect_gains` calls before the existing 5d/20d/60d calls. The `candidate_outcomes` table gets two new forward-return columns (`forward_return_3d`, `forward_return_10d`) via a schema addition and migration v6; `outcomes/labels.py` computes them in `compute_forward_returns`; `outcomes/tracker.py` lists them as valid update fields. No scoring weights, no OpenAI, shadow/diagnostic only.

**Tech Stack:** Python 3.11, DuckDB, pytest

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `missed/labels.py` | Modify | Add `GAIN_1D_THRESHOLD`, `GAIN_3D_THRESHOLD`, `GAIN_10D_THRESHOLD`; add 3 event-type strings to `EVENT_TYPES` |
| `missed/detector.py` | Modify | Import 3 new thresholds; add `_detect_gains` calls for 1d/3d/10d before existing 5d call |
| `storage/schema.sql` | Modify | Add `forward_return_3d DOUBLE` and `forward_return_10d DOUBLE` to `candidate_outcomes` |
| `storage/migrations.py` | Modify | Add migration v6 to `ALTER TABLE candidate_outcomes ADD COLUMN` for both new columns |
| `outcomes/labels.py` | Modify | Add `(3, "forward_return_3d")` and `(10, "forward_return_10d")` to the days/key loop in `compute_forward_returns` |
| `outcomes/tracker.py` | Modify | Add `"forward_return_3d"` and `"forward_return_10d"` to the `fields` list in `update_forward_returns` |
| `tests/test_missed_detector.py` | Modify | Add 5 tests: 1d detected, 3d detected, 10d detected, no duplicate (ticker/date/type), insufficient history graceful |
| `tests/test_outcomes.py` | Modify | Add 1 test: `forward_return_3d`/`forward_return_10d` persisted by `update_forward_returns` |

---

### Task 1: Add new constants and event types to `missed/labels.py`

**Files:**
- Modify: `missed/labels.py`
- Test: `tests/test_missed_detector.py` (append 1 test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_missed_detector.py`:

```python
def test_new_detection_labels_present():
    """New threshold constants and event types are present in labels module."""
    from missed.labels import (
        GAIN_1D_THRESHOLD, GAIN_3D_THRESHOLD, GAIN_10D_THRESHOLD, EVENT_TYPES
    )
    assert GAIN_1D_THRESHOLD == pytest.approx(0.05)
    assert GAIN_3D_THRESHOLD == pytest.approx(0.08)
    assert GAIN_10D_THRESHOLD == pytest.approx(0.12)
    assert "gain_1d_5pct" in EVENT_TYPES
    assert "gain_3d_8pct" in EVENT_TYPES
    assert "gain_10d_12pct" in EVENT_TYPES
```

(The `import pytest` is already at the top of that file — verify before running.)

- [ ] **Step 2: Run the test to verify it fails**

```bash
venv/bin/python -m pytest tests/test_missed_detector.py::test_new_detection_labels_present -v
```

Expected: FAIL — `ImportError: cannot import name 'GAIN_1D_THRESHOLD'`

- [ ] **Step 3: Update `missed/labels.py`**

Make two targeted edits.

**Edit 1:** Replace the `EVENT_TYPES` block (currently lines 68–74):

```python
EVENT_TYPES: list[str] = [
    "gain_1d_5pct",
    "gain_3d_8pct",
    "gain_5d_10pct",
    "gain_10d_12pct",
    "gain_20d_20pct",
    "gain_60d_30pct",
    "52wk_high_breakout",
    "gap_up",
]
```

**Edit 2:** Replace the thresholds block (currently lines 76–79):

```python
# Thresholds for detection
GAIN_1D_THRESHOLD = 0.05
GAIN_3D_THRESHOLD = 0.08
GAIN_5D_THRESHOLD = 0.10
GAIN_10D_THRESHOLD = 0.12
GAIN_20D_THRESHOLD = 0.20
GAIN_60D_THRESHOLD = 0.30
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
venv/bin/python -m pytest tests/test_missed_detector.py::test_new_detection_labels_present -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add missed/labels.py tests/test_missed_detector.py
git commit -m "feat: add 1d/3d/10d threshold constants and event types to missed/labels.py"
```

---

### Task 2: Add 1d/3d/10d detection to `missed/detector.py` and new detector tests

**Files:**
- Modify: `missed/detector.py`
- Modify: `tests/test_missed_detector.py` (append 5 tests)

- [ ] **Step 1: Write the 5 failing tests**

Append to `tests/test_missed_detector.py`:

```python
def test_gain_1d_5pct_detected(conn):
    """Ticker rising +6% in 1 calendar day → gain_1d_5pct event detected."""
    from missed.detector import detect_missed_opportunities
    ticker = "UP1D"
    _company(conn, ticker)
    today = date.today()
    for i in range(10, 2, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(2, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 106.0)  # +6% > 5%

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker and e["event_type"] == "gain_1d_5pct"]
    assert len(matching) >= 1, f"Expected gain_1d_5pct for {ticker}, got {[e['event_type'] for e in events if e['ticker'] == ticker]}"


def test_gain_3d_8pct_detected(conn):
    """Ticker rising +10% over 3 calendar days → gain_3d_8pct event detected."""
    from missed.detector import detect_missed_opportunities
    ticker = "UP3D"
    _company(conn, ticker)
    today = date.today()
    for i in range(15, 5, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(5, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 110.0)  # +10% > 8%

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker and e["event_type"] == "gain_3d_8pct"]
    assert len(matching) >= 1, f"Expected gain_3d_8pct for {ticker}, got {[e['event_type'] for e in events if e['ticker'] == ticker]}"


def test_gain_10d_12pct_detected(conn):
    """Ticker rising +15% over 10 calendar days → gain_10d_12pct event detected."""
    from missed.detector import detect_missed_opportunities
    ticker = "UP10D"
    _company(conn, ticker)
    today = date.today()
    for i in range(30, 12, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(12, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 115.0)  # +15% > 12%

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker and e["event_type"] == "gain_10d_12pct"]
    assert len(matching) >= 1, f"Expected gain_10d_12pct for {ticker}, got {[e['event_type'] for e in events if e['ticker'] == ticker]}"


def test_no_duplicate_event_for_same_ticker_date_window(conn):
    """detect_missed_opportunities never returns two events with the same (ticker, event_date, event_type)."""
    from missed.detector import detect_missed_opportunities
    ticker = "NODUP"
    _company(conn, ticker)
    today = date.today()
    for i in range(30, 12, -1):
        _price(conn, ticker, today - timedelta(days=i), 100.0)
    for i in range(12, 0, -1):
        _price(conn, ticker, today - timedelta(days=i), 120.0)

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker]
    triples = [(e["ticker"], e["event_date"], e["event_type"]) for e in matching]
    assert len(triples) == len(set(triples)), f"Duplicate (ticker, date, type) events: {triples}"


def test_insufficient_history_handled_gracefully(conn):
    """Ticker with only 1 day of prices causes no crash and no events (no prior reference)."""
    from missed.detector import detect_missed_opportunities
    ticker = "SPARSE"
    _company(conn, ticker)
    today = date.today()
    _price(conn, ticker, today - timedelta(days=1), 200.0)  # only 1 data point

    events = detect_missed_opportunities(conn, lookback_days=30)
    matching = [e for e in events if e["ticker"] == ticker]
    assert len(matching) == 0, f"No events expected for single-day history, got {matching}"
```

- [ ] **Step 2: Run the 5 new tests to verify they fail**

```bash
venv/bin/python -m pytest \
  tests/test_missed_detector.py::test_gain_1d_5pct_detected \
  tests/test_missed_detector.py::test_gain_3d_8pct_detected \
  tests/test_missed_detector.py::test_gain_10d_12pct_detected \
  tests/test_missed_detector.py::test_no_duplicate_event_for_same_ticker_date_window \
  tests/test_missed_detector.py::test_insufficient_history_handled_gracefully \
  -v
```

Expected: the 3 detection tests FAIL (event_type not returned), the 2 structural tests may PASS (they're resilient to missing types). That's fine — commit them as-is.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_missed_detector.py
git commit -m "test: add 5 failing tests for 1d/3d/10d move detection windows"
```

- [ ] **Step 4: Update `missed/detector.py`**

Replace the entire file with the following (only changes: new imports and 3 added `_detect_gains` calls at the start of `detect_missed_opportunities`):

```python
"""Missed-opportunity detector.

Scans prices_daily for significant price moves and annotates each detected event
with universe/scoring context that existed BEFORE the move.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

import duckdb

from missed.labels import (
    GAIN_1D_THRESHOLD,
    GAIN_3D_THRESHOLD,
    GAIN_5D_THRESHOLD,
    GAIN_10D_THRESHOLD,
    GAIN_20D_THRESHOLD,
    GAIN_60D_THRESHOLD,
)

logger = logging.getLogger("mhde.missed.detector")


def detect_missed_opportunities(
    conn: duckdb.DuckDBPyConnection,
    lookback_days: int = 90,
) -> list[dict]:
    """
    Scan prices_daily for significant moves in the past lookback_days.
    Returns a list of event dicts — does NOT write to DB (caller decides to persist).
    """
    cutoff = date.today() - timedelta(days=lookback_days)
    events: list[dict] = []

    _detect_gains(conn, cutoff, 1, GAIN_1D_THRESHOLD, "gain_1d_5pct", events)
    _detect_gains(conn, cutoff, 3, GAIN_3D_THRESHOLD, "gain_3d_8pct", events)
    _detect_gains(conn, cutoff, 5, GAIN_5D_THRESHOLD, "gain_5d_10pct", events)
    _detect_gains(conn, cutoff, 10, GAIN_10D_THRESHOLD, "gain_10d_12pct", events)
    _detect_gains(conn, cutoff, 20, GAIN_20D_THRESHOLD, "gain_20d_20pct", events)
    _detect_gains(conn, cutoff, 60, GAIN_60D_THRESHOLD, "gain_60d_30pct", events)
    _detect_52wk_breakouts(conn, cutoff, events)

    return events


def _detect_gains(
    conn: duckdb.DuckDBPyConnection,
    cutoff: date,
    window: int,
    threshold: float,
    event_type: str,
    events: list[dict],
) -> None:
    rows = conn.execute(
        """
        SELECT p_now.ticker,
               p_now.trade_date AS event_date,
               p_before.close   AS ref_price,
               p_now.close      AS peak_price
        FROM prices_daily p_now
        JOIN prices_daily p_before
          ON p_now.ticker = p_before.ticker
         AND p_before.trade_date = (
             SELECT MAX(trade_date) FROM prices_daily
              WHERE ticker = p_now.ticker
                AND trade_date <= p_now.trade_date - INTERVAL (? || ' days')
         )
        WHERE p_now.trade_date >= ?
          AND p_before.close > 0
          AND (p_now.close - p_before.close) / p_before.close >= ?
        ORDER BY p_now.ticker, p_now.trade_date
        """,
        [window, cutoff.isoformat(), threshold],
    ).fetchall()

    for ticker, event_date, ref_price, peak_price in rows:
        if isinstance(event_date, str):
            event_date = date.fromisoformat(event_date)
        return_value = (peak_price - ref_price) / ref_price * 100
        events.append(_build_event(conn, ticker, event_date, event_type,
                                   return_value, window, ref_price, peak_price))


def _detect_52wk_breakouts(
    conn: duckdb.DuckDBPyConnection,
    cutoff: date,
    events: list[dict],
) -> None:
    rows = conn.execute(
        """
        SELECT p.ticker, p.trade_date, p.close,
               (SELECT MAX(ph.close) FROM prices_daily ph
                 WHERE ph.ticker = p.ticker
                   AND ph.trade_date < p.trade_date
                   AND ph.trade_date >= p.trade_date - INTERVAL '252 days') AS prior_high
        FROM prices_daily p
        WHERE p.trade_date >= ?
        ORDER BY p.ticker, p.trade_date
        """,
        [cutoff.isoformat()],
    ).fetchall()

    seen: set[str] = set()
    for ticker, event_date, close, prior_high in rows:
        if prior_high is None or close is None:
            continue
        if isinstance(event_date, str):
            event_date = date.fromisoformat(event_date)
        if close > prior_high and ticker not in seen:
            seen.add(ticker)
            return_val = (close - prior_high) / prior_high * 100
            events.append(_build_event(conn, ticker, event_date, "52wk_high_breakout",
                                       return_val, 252, prior_high, close))


def _build_event(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
    event_type: str,
    return_value: float,
    window_days: int,
    ref_price: float,
    peak_price: float,
) -> dict:
    was_in_universe = _was_in_universe(conn, ticker)
    was_scored, score_before, tier_before = _score_before_event(conn, ticker, event_date)
    had_catalyst = _had_catalyst_evidence(conn, ticker, event_date)

    return {
        "event_id": uuid.uuid4().hex[:16],
        "ticker": ticker,
        "event_date": event_date,
        "event_type": event_type,
        "return_value": round(return_value, 2),
        "window_days": window_days,
        "reference_price": ref_price,
        "peak_price": peak_price,
        "was_in_universe": was_in_universe,
        "was_scored": was_scored,
        "score_before_event": score_before,
        "tier_before_event": tier_before,
        "was_rejected": tier_before == "Reject" if tier_before else False,
        "was_incomplete": tier_before == "Incomplete" if tier_before else False,
        "had_catalyst_evidence": had_catalyst,
    }


def _was_in_universe(conn: duckdb.DuckDBPyConnection, ticker: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE ticker = ?", [ticker]
    ).fetchone()
    return bool(row and row[0] > 0)


def _score_before_event(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
) -> tuple[bool, float | None, str | None]:
    row = conn.execute(
        """SELECT total_score, tier FROM scores
           WHERE ticker = ? AND as_of_date < ?
           ORDER BY as_of_date DESC LIMIT 1""",
        [ticker, event_date.isoformat()],
    ).fetchone()
    if row:
        return True, row[0], row[1]
    return False, None, None


def _had_catalyst_evidence(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
) -> bool:
    row = conn.execute(
        """SELECT COUNT(*) FROM filings
           WHERE ticker = ? AND filing_date < ?
             AND filing_date >= ?""",
        [ticker, event_date.isoformat(),
         (event_date - timedelta(days=60)).isoformat()],
    ).fetchone()
    return bool(row and row[0] > 0)


def cluster_events(events: list[dict]) -> list[dict]:
    """
    Deduplicate overlapping detections of the same price move.

    Groups events by (ticker, event_type) and merges any whose event_dates
    are within window_days of each other (they represent overlapping detection
    windows on the same underlying move).

    Returns one event per cluster — the one with the highest return_value —
    enriched with cluster metadata: cluster_id, raw_detection_count,
    event_window_start, event_window_end.
    """
    if not events:
        return []

    from itertools import groupby

    key_fn = lambda e: (e["ticker"], e["event_type"])
    sorted_events = sorted(events, key=lambda e: (e["ticker"], e["event_type"], e["event_date"]))

    result: list[dict] = []
    for (ticker, event_type), group_iter in groupby(sorted_events, key=key_fn):
        group = list(group_iter)
        clusters: list[list[dict]] = []
        current_cluster: list[dict] = [group[0]]

        for evt in group[1:]:
            prev = current_cluster[-1]
            window = evt.get("window_days", 20)
            delta = (evt["event_date"] - prev["event_date"]).days
            if delta <= window:
                current_cluster.append(evt)
            else:
                clusters.append(current_cluster)
                current_cluster = [evt]
        clusters.append(current_cluster)

        for cluster in clusters:
            primary = max(cluster, key=lambda e: e["return_value"])
            enriched = dict(primary)
            enriched["cluster_id"] = uuid.uuid4().hex[:16]
            enriched["raw_detection_count"] = len(cluster)
            enriched["event_window_start"] = min(e["event_date"] for e in cluster)
            enriched["event_window_end"] = max(e["event_date"] for e in cluster)
            result.append(enriched)

    return result


def persist_events(conn: duckdb.DuckDBPyConnection, events: list[dict]) -> int:
    """Write detected events to missed_opportunity_events table. Returns insert count."""
    inserted = 0
    for e in events:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO missed_opportunity_events
                   (event_id, ticker, event_date, event_type, return_value, window_days,
                    reference_price, peak_price, was_in_universe, was_scored,
                    score_before_event, tier_before_event, was_rejected, was_incomplete,
                    had_catalyst_evidence, investigation_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                [e["event_id"], e["ticker"], e["event_date"].isoformat(),
                 e["event_type"], e["return_value"], e["window_days"],
                 e.get("reference_price"), e.get("peak_price"),
                 e["was_in_universe"], e["was_scored"],
                 e.get("score_before_event"), e.get("tier_before_event"),
                 e.get("was_rejected", False), e.get("was_incomplete", False),
                 e["had_catalyst_evidence"]],
            )
            inserted += 1
        except Exception as exc:
            logger.warning("Failed to persist event %s: %s", e.get("event_id"), exc)
    return inserted
```

- [ ] **Step 5: Run all detector tests**

```bash
venv/bin/python -m pytest tests/test_missed_detector.py tests/test_missed_deduplication.py -v 2>&1 | tail -25
```

Expected: all tests pass (existing 11 + new 6 = 17 detector tests, 11 deduplication tests)

- [ ] **Step 6: Run full suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 862 passed (856 + 6 new)

- [ ] **Step 7: Commit**

```bash
git add missed/detector.py tests/test_missed_detector.py
git commit -m "feat: add 1d/3d/10d spike detection windows to missed-opportunity detector"
```

---

### Task 3: Add `forward_return_3d`/`forward_return_10d` to schema, migration, and outcomes

**Files:**
- Modify: `storage/schema.sql` (add 2 columns to `candidate_outcomes`)
- Modify: `storage/migrations.py` (add migration v6)
- Modify: `outcomes/labels.py` (add 3d/10d to `compute_forward_returns` loop)
- Modify: `outcomes/tracker.py` (add 3d/10d to `fields` list in `update_forward_returns`)
- Modify: `tests/test_outcomes.py` (append 1 test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_outcomes.py`:

```python
def test_update_forward_returns_3d_10d(conn):
    """forward_return_3d and forward_return_10d can be persisted via update_forward_returns."""
    create_outcome_record(conn, "run002", "NVDA", date.today(), "A", 85.0, 200.0)
    candidate_id = conn.execute(
        "SELECT candidate_id FROM candidate_outcomes WHERE ticker = 'NVDA'"
    ).fetchone()[0]
    update_forward_returns(conn, candidate_id, {
        "forward_return_3d": 0.09,
        "forward_return_10d": 0.14,
    })
    row = conn.execute(
        "SELECT forward_return_3d, forward_return_10d FROM candidate_outcomes WHERE candidate_id = ?",
        [candidate_id],
    ).fetchone()
    assert row[0] == pytest.approx(0.09)
    assert row[1] == pytest.approx(0.14)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
venv/bin/python -m pytest tests/test_outcomes.py::test_update_forward_returns_3d_10d -v
```

Expected: FAIL — column `forward_return_3d` does not exist.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_outcomes.py
git commit -m "test: add failing test for forward_return_3d/10d in candidate_outcomes"
```

- [ ] **Step 4: Update `storage/schema.sql`**

Find the `candidate_outcomes` table (around line 217). The current column block ends with:

```sql
    forward_return_120d DOUBLE,
```

Add the two new columns directly after `forward_return_1d DOUBLE,` and before `forward_return_5d DOUBLE,` so the columns are in ascending order:

Replace this section:

```sql
    forward_return_1d DOUBLE,
    forward_return_5d DOUBLE,
    forward_return_20d DOUBLE,
    forward_return_60d DOUBLE,
    forward_return_120d DOUBLE,
```

With:

```sql
    forward_return_1d DOUBLE,
    forward_return_3d DOUBLE,
    forward_return_5d DOUBLE,
    forward_return_10d DOUBLE,
    forward_return_20d DOUBLE,
    forward_return_60d DOUBLE,
    forward_return_120d DOUBLE,
```

- [ ] **Step 5: Add migration v6 to `storage/migrations.py`**

Replace:

```python
_CURRENT_VERSION = 5
```

With:

```python
_CURRENT_VERSION = 6
```

And append this block at the end of `run_migrations`, after the `if current < 5:` block:

```python
    if current < 6:
        existing_cols = {
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'candidate_outcomes'"
            ).fetchall()
        }
        for col in ("forward_return_3d", "forward_return_10d"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE candidate_outcomes ADD COLUMN {col} DOUBLE")
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (6, 'Add forward_return_3d and forward_return_10d to candidate_outcomes') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v6: forward_return_3d/10d on candidate_outcomes")
```

- [ ] **Step 6: Update `outcomes/labels.py`**

In `compute_forward_returns`, replace the forward-return loop (currently lines 52–57):

```python
    for days, key in [(1, "forward_return_1d"), (5, "forward_return_5d"),
                      (20, "forward_return_20d"), (60, "forward_return_60d"),
                      (120, "forward_return_120d")]:
        v = ret_at(days)
        if v is not None:
            result[key] = v
```

With:

```python
    for days, key in [
        (1, "forward_return_1d"),
        (3, "forward_return_3d"),
        (5, "forward_return_5d"),
        (10, "forward_return_10d"),
        (20, "forward_return_20d"),
        (60, "forward_return_60d"),
        (120, "forward_return_120d"),
    ]:
        v = ret_at(days)
        if v is not None:
            result[key] = v
```

- [ ] **Step 7: Update `outcomes/tracker.py`**

In `update_forward_returns`, replace the `fields` list (currently lines 43–49):

```python
    fields = [
        "forward_return_1d", "forward_return_5d", "forward_return_20d",
        "forward_return_60d", "forward_return_120d",
        "max_drawdown_20d", "max_drawdown_60d",
        "max_runup_20d", "max_runup_60d",
        "hit_10pct_before_down_10pct", "hit_20pct_before_down_10pct",
    ]
```

With:

```python
    fields = [
        "forward_return_1d", "forward_return_3d", "forward_return_5d",
        "forward_return_10d", "forward_return_20d",
        "forward_return_60d", "forward_return_120d",
        "max_drawdown_20d", "max_drawdown_60d",
        "max_runup_20d", "max_runup_60d",
        "hit_10pct_before_down_10pct", "hit_20pct_before_down_10pct",
    ]
```

- [ ] **Step 8: Run the outcomes test**

```bash
venv/bin/python -m pytest tests/test_outcomes.py -v
```

Expected: all 7 tests pass (6 existing + 1 new)

- [ ] **Step 9: Run full suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 863 passed

- [ ] **Step 10: Commit**

```bash
git add storage/schema.sql storage/migrations.py outcomes/labels.py outcomes/tracker.py tests/test_outcomes.py
git commit -m "feat: add forward_return_3d/10d to candidate_outcomes schema, migration v6, and outcomes tracker"
```

---

### Task 4: Final verification

**Files:** None — verification only.

- [ ] **Step 1: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: **863 passed** (856 baseline + 6 detector + 1 outcomes)

- [ ] **Step 2: Verify new event types are detected in the DB**

```bash
venv/bin/python main.py data universe-stats
```

Expected: prints universe stats without error. (No live detection run needed — the detector is shadow-only.)

- [ ] **Step 3: Verify labels import**

Create file `.claude/local_scripts/verify_new_labels.py`:

```python
#!/usr/bin/env python
from missed.labels import (
    GAIN_1D_THRESHOLD, GAIN_3D_THRESHOLD, GAIN_10D_THRESHOLD, EVENT_TYPES
)
print(f"GAIN_1D_THRESHOLD: {GAIN_1D_THRESHOLD}")
print(f"GAIN_3D_THRESHOLD: {GAIN_3D_THRESHOLD}")
print(f"GAIN_10D_THRESHOLD: {GAIN_10D_THRESHOLD}")
new_types = [t for t in EVENT_TYPES if t in ("gain_1d_5pct", "gain_3d_8pct", "gain_10d_12pct")]
print(f"New event types in EVENT_TYPES: {new_types}")
assert len(new_types) == 3, f"Expected 3 new types, got {new_types}"
print("OK — all 3 new labels present")
```

Run:
```bash
venv/bin/python .claude/local_scripts/verify_new_labels.py
```

Expected:
```
GAIN_1D_THRESHOLD: 0.05
GAIN_3D_THRESHOLD: 0.08
GAIN_10D_THRESHOLD: 0.12
New event types in EVENT_TYPES: ['gain_1d_5pct', 'gain_3d_8pct', 'gain_10d_12pct']
OK — all 3 new labels present
```

- [ ] **Step 4: Final git log**

```bash
git log --oneline -6
```

Expected: 4 new commits covering labels, detector, schema/migration/outcomes, plus previous work.

---

## Self-Review

### 1. Spec coverage

| Requirement | Task |
|---|---|
| Add event labels: gain_1d_5pct, gain_3d_8pct, gain_10d_12pct | Task 1 (labels.py) |
| Detect 1d, 3d, 10d rolling gains from prices_daily | Task 2 (detector.py) |
| Preserve existing 5d, 20d, 60d detection | Task 2 (all calls preserved, just 3 new calls added) |
| Deterministic ordering | Task 2 (1d→3d→5d→10d→20d→60d→52wk, fixed order) |
| Avoid duplicate event IDs | Task 2 (test verifies no (ticker, date, type) triple repeats) |
| Add forward_return_3d and forward_return_10d | Task 3 (schema.sql + migration v6 + tracker) |
| Preserve existing data | Task 3 (migration uses ALTER TABLE ADD COLUMN with existence check) |
| Missed event reports include new types | No change needed — report.py reads from missed_opportunity_events generically, all event types auto-included |
| Move episode/spike attribution artifacts recognize new windows | No change needed — attribution.py works at root-cause level, not event-type level |
| Test: 1d +5% detected | Task 2 (test_gain_1d_5pct_detected) |
| Test: 3d +8% detected | Task 2 (test_gain_3d_8pct_detected) |
| Test: 10d +12% detected | Task 2 (test_gain_10d_12pct_detected) |
| Test: existing 5d/20d/60d still pass | Task 2 (existing test_gain_5d_10pct_detected etc. preserved) |
| Test: no duplicate event for same ticker/date/window | Task 2 (test_no_duplicate_event_for_same_ticker_date_window) |
| Test: insufficient history graceful | Task 2 (test_insufficient_history_handled_gracefully) |
| Run full tests | Task 4 |
| No scoring weight changes | ✅ no files in scoring/ or features/ touched |
| No OpenAI calls | ✅ |
| Shadow/diagnostic only | ✅ persist_events uses INSERT OR IGNORE, detector returns list without auto-persisting |

### 2. Placeholder scan

None found. All code is complete in every step.

### 3. Type consistency

- `GAIN_1D_THRESHOLD = 0.05`, `GAIN_3D_THRESHOLD = 0.08`, `GAIN_10D_THRESHOLD = 0.12` — used as `threshold: float` in `_detect_gains` ✅
- `_detect_gains(conn, cutoff, 1, GAIN_1D_THRESHOLD, "gain_1d_5pct", events)` — matches signature `(conn, cutoff, window: int, threshold: float, event_type: str, events: list[dict])` ✅
- `forward_return_3d` and `forward_return_10d` named consistently across schema.sql, migrations.py, tracker.py, labels.py ✅
- Migration v6 guard uses `if current < 6:` and bumps `_CURRENT_VERSION = 6` ✅
