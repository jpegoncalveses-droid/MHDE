# KI-128 Weekday-Aware Recency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate weekend false-positive Telegram alerts in `pipelines/health_check.py` and the FX leg of `monitoring/pipeline_execution.py` (and `pipelines/freshness.py::check_fx_freshness`) without weakening real-outage detection.

**Architecture:** Introduce a single source of truth — `pipelines/market_calendar.py` — exposing four pure helpers (`trading_days_between`, `expected_equity_prediction_date`, `is_forex_closed`, `fx_close_floor`). The three callers gate their existing recency checks on these helpers. No new external dependencies; holidays remain operator-acknowledged per ADR-015.

**Tech Stack:** Python 3.12, DuckDB, pytest. Existing modules already accept injected `now` for deterministic tests.

**Branch:** `ki128-weekday-aware-recency` (already created; spec committed at `docs/superpowers/specs/2026-05-10-ki128-weekday-aware-recency-design.md`).

---

## File Structure

**Create:**
- `pipelines/market_calendar.py` — pure helpers, no I/O.
- `tests/pipelines/__init__.py` — empty marker.
- `tests/pipelines/test_market_calendar.py` — unit tests for the helpers.
- `tests/pipelines/test_health_check_weekend.py` — health_check weekend behavior.
- `tests/regression/test_pipeline_execution_weekend.py` — pipeline_execution FX weekend behavior.

**Modify:**
- `pipelines/freshness.py` — replace local `_trading_days_between` with import from `market_calendar`; update `check_fx_freshness` for forex-closed window.
- `pipelines/health_check.py` — `_check_equity` uses `expected_equity_prediction_date`; `_check_fx` adds forex-closed branch.
- `monitoring/pipeline_execution.py` — FX leg adds forex-closed branch (equity / crypto unchanged).
- `tests/equity/test_pipeline_freshness.py` — add forex-closed cases to FX section.
- `KNOWN_ISSUES.md` — move KI-128 to "Recently resolved".
- `DECISIONS.md` — append ADR-018.

---

## Task 1: Add `pipelines/market_calendar.py` with `trading_days_between`

This task moves the existing `_trading_days_between` helper out of `pipelines/freshness.py` into the new module verbatim (renamed to public). The freshness module's import is updated in the same commit so nothing breaks.

**Files:**
- Create: `pipelines/market_calendar.py`
- Create: `tests/pipelines/__init__.py`
- Create: `tests/pipelines/test_market_calendar.py`
- Modify: `pipelines/freshness.py:55-65` (remove `_trading_days_between`); `pipelines/freshness.py:85` (call site)

- [ ] **Step 1: Write the failing test for `trading_days_between`**

Create `tests/pipelines/__init__.py` (empty file).

Create `tests/pipelines/test_market_calendar.py`:

```python
"""Unit tests for pipelines/market_calendar.py — pure UTC helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone

from pipelines.market_calendar import trading_days_between


def test_trading_days_between_same_weekday():
    # Wed only.
    assert trading_days_between(date(2026, 5, 6), date(2026, 5, 6)) == 1


def test_trading_days_between_skips_weekend():
    # Fri 2026-05-08 → Mon 2026-05-11 inclusive: Fri + Mon = 2 trading days.
    assert trading_days_between(date(2026, 5, 8), date(2026, 5, 11)) == 2


def test_trading_days_between_full_week():
    # Mon → Fri inclusive = 5.
    assert trading_days_between(date(2026, 5, 4), date(2026, 5, 8)) == 5


def test_trading_days_between_empty_range():
    # start > end → 0.
    assert trading_days_between(date(2026, 5, 8), date(2026, 5, 4)) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/pipelines/test_market_calendar.py -v`
Expected: ImportError / ModuleNotFoundError on `pipelines.market_calendar`.

- [ ] **Step 3: Create `pipelines/market_calendar.py` with `trading_days_between`**

```python
"""Pure UTC helpers for market-clock decisions.

Single source of truth for weekday and forex-closed-window logic
across pipelines/health_check.py, monitoring/pipeline_execution.py,
and pipelines/freshness.py.

No DB. No network. No I/O. All callers must pass a tz-aware UTC
datetime as `now` so tests are deterministic.

See docs/superpowers/specs/2026-05-10-ki128-weekday-aware-recency-design.md
for the full design and DECISIONS.md ADR-018 for the rationale.
"""
from __future__ import annotations

from datetime import date, timedelta


def trading_days_between(start: date, end: date) -> int:
    """Inclusive Mon-Fri count between two dates. Returns 0 if start > end.

    Moved verbatim from pipelines/freshness.py during the KI-128 fix
    so all market-clock helpers live together.
    """
    if start > end:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days
```

- [ ] **Step 4: Update `pipelines/freshness.py` to import from the new module**

Edit `pipelines/freshness.py`. Remove lines 55-65 (the `_trading_days_between` definition) and update the call site at line 85.

Replace:

```python
def _trading_days_between(start: date, end: date) -> int:
    """Inclusive trading-day count (Mon-Fri) between two dates. start <= end."""
    if start > end:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days
```

with:

```python
from pipelines.market_calendar import trading_days_between
```

(add this import at the top of the file alongside the other imports).

Update the call site at line 85:

```python
trading_gap = _trading_days_between(latest + timedelta(days=1), today)
```

becomes:

```python
trading_gap = trading_days_between(latest + timedelta(days=1), today)
```

- [ ] **Step 5: Run new + existing freshness tests to verify nothing broke**

Run: `.venv/bin/python -m pytest tests/pipelines/test_market_calendar.py tests/equity/test_pipeline_freshness.py -v`
Expected: All pass — 4 new + the existing freshness suite green.

- [ ] **Step 6: Commit**

```bash
git add pipelines/market_calendar.py tests/pipelines/__init__.py tests/pipelines/test_market_calendar.py pipelines/freshness.py
git commit -m "$(cat <<'EOF'
feat(market_calendar): extract trading_days_between to shared module

KI-128 prep: introduces pipelines/market_calendar.py as the single
source of truth for market-clock helpers. Moves _trading_days_between
verbatim from pipelines/freshness.py and updates the only caller.
No behavior change.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `expected_equity_prediction_date` helper

**Files:**
- Modify: `pipelines/market_calendar.py` (add function)
- Modify: `tests/pipelines/test_market_calendar.py` (add tests)

- [ ] **Step 1: Write failing tests for `expected_equity_prediction_date`**

Append to `tests/pipelines/test_market_calendar.py`:

```python
from pipelines.market_calendar import expected_equity_prediction_date


def _utc(year: int, month: int, day: int, hour: int = 6) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def test_expected_equity_prediction_date_tuesday_returns_monday():
    # Tue 2026-05-12 06:00 UTC → Mon 2026-05-11.
    assert expected_equity_prediction_date(_utc(2026, 5, 12)) == date(2026, 5, 11)


def test_expected_equity_prediction_date_wednesday_returns_tuesday():
    assert expected_equity_prediction_date(_utc(2026, 5, 13)) == date(2026, 5, 12)


def test_expected_equity_prediction_date_thursday_returns_wednesday():
    assert expected_equity_prediction_date(_utc(2026, 5, 14)) == date(2026, 5, 13)


def test_expected_equity_prediction_date_friday_returns_thursday():
    assert expected_equity_prediction_date(_utc(2026, 5, 15)) == date(2026, 5, 14)


def test_expected_equity_prediction_date_saturday_returns_friday():
    # Sat 2026-05-16 → Fri 2026-05-15.
    assert expected_equity_prediction_date(_utc(2026, 5, 16)) == date(2026, 5, 15)


def test_expected_equity_prediction_date_sunday_returns_friday():
    # Sun 2026-05-17 → Fri 2026-05-15 (skip Sat).
    assert expected_equity_prediction_date(_utc(2026, 5, 17)) == date(2026, 5, 15)


def test_expected_equity_prediction_date_monday_returns_friday():
    # Mon 2026-05-18 → Fri 2026-05-15 (skip Sun + Sat).
    assert expected_equity_prediction_date(_utc(2026, 5, 18)) == date(2026, 5, 15)


def test_expected_equity_prediction_date_independent_of_hour():
    # Same day, different hour → same result.
    assert expected_equity_prediction_date(_utc(2026, 5, 18, 0)) == date(2026, 5, 15)
    assert expected_equity_prediction_date(_utc(2026, 5, 18, 23)) == date(2026, 5, 15)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/pipelines/test_market_calendar.py -v -k expected_equity`
Expected: ImportError on `expected_equity_prediction_date`.

- [ ] **Step 3: Implement `expected_equity_prediction_date`**

Append to `pipelines/market_calendar.py` (add `datetime` to the imports):

```python
from datetime import date, datetime, timedelta


def expected_equity_prediction_date(now: datetime) -> date:
    """Return the most recent Mon-Fri *strictly before* now.date().

    Equity ML predict runs at 00:15 UTC and writes
    prediction_date = "latest closed market day", which is the most
    recent weekday before today. By 06:00 UTC of any day, that's:

      Mon → Fri (Sat/Sun closed, so back to Fri)
      Tue → Mon
      Wed → Tue
      Thu → Wed
      Fri → Thu
      Sat → Fri
      Sun → Fri

    Replaces the literal `now.date() - 1` previously used in
    pipelines/health_check.py::_check_equity, which silently returned
    Sat or Sun on Sun/Mon mornings — neither has equity data because
    NYSE is closed.

    `now` must be tz-aware UTC; .date() is taken in the UTC frame.
    """
    cur = now.date() - timedelta(days=1)
    while cur.weekday() >= 5:  # Sat=5, Sun=6
        cur -= timedelta(days=1)
    return cur
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/pipelines/test_market_calendar.py -v`
Expected: All tests pass (4 from Task 1 + 8 new = 12 tests).

- [ ] **Step 5: Commit**

```bash
git add pipelines/market_calendar.py tests/pipelines/test_market_calendar.py
git commit -m "$(cat <<'EOF'
feat(market_calendar): add expected_equity_prediction_date

Returns the most recent Mon-Fri strictly before now.date(). Replaces
the literal `now.date() - 1` used by the equity health check, which
silently returned Sat/Sun on weekend mornings.

Part of KI-128 fix.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add `is_forex_closed` and `fx_close_floor` helpers

**Files:**
- Modify: `pipelines/market_calendar.py`
- Modify: `tests/pipelines/test_market_calendar.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/pipelines/test_market_calendar.py`:

```python
from pipelines.market_calendar import is_forex_closed, fx_close_floor


def _utc_full(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


# is_forex_closed: True iff Fri 22:00 UTC <= now < Sun 22:00 UTC.

def test_is_forex_closed_friday_before_close():
    # Fri 21:59 UTC → False.
    assert is_forex_closed(_utc_full(2026, 5, 15, 21, 59)) is False


def test_is_forex_closed_friday_at_close():
    # Fri 22:00 UTC → True (boundary inclusive on the lower side).
    assert is_forex_closed(_utc_full(2026, 5, 15, 22, 0)) is True


def test_is_forex_closed_saturday_noon():
    assert is_forex_closed(_utc_full(2026, 5, 16, 12, 0)) is True


def test_is_forex_closed_sunday_before_resume():
    # Sun 21:59 UTC → True.
    assert is_forex_closed(_utc_full(2026, 5, 17, 21, 59)) is True


def test_is_forex_closed_sunday_at_resume():
    # Sun 22:00 UTC → False (boundary exclusive on upper side).
    assert is_forex_closed(_utc_full(2026, 5, 17, 22, 0)) is False


def test_is_forex_closed_midweek_is_open():
    # Wed 12:00 UTC → False.
    assert is_forex_closed(_utc_full(2026, 5, 13, 12, 0)) is False


# fx_close_floor: returns the Fri 22:00 UTC of the active closure.

def test_fx_close_floor_saturday():
    # Sat 2026-05-16 12:00 → Fri 2026-05-15 22:00.
    assert fx_close_floor(_utc_full(2026, 5, 16, 12, 0)) == _utc_full(2026, 5, 15, 22, 0)


def test_fx_close_floor_sunday_before_resume():
    # Sun 2026-05-17 21:59 → Fri 2026-05-15 22:00.
    assert fx_close_floor(_utc_full(2026, 5, 17, 21, 59)) == _utc_full(2026, 5, 15, 22, 0)


def test_fx_close_floor_friday_after_close():
    # Fri 2026-05-15 22:30 → Fri 2026-05-15 22:00 (same day).
    assert fx_close_floor(_utc_full(2026, 5, 15, 22, 30)) == _utc_full(2026, 5, 15, 22, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/pipelines/test_market_calendar.py -v -k "forex_closed or close_floor"`
Expected: ImportError on the two new symbols.

- [ ] **Step 3: Implement the helpers**

Append to `pipelines/market_calendar.py`:

```python
# Forex spot trades roughly Sun 22:00 UTC → Fri 22:00 UTC. The
# closed window is the rest. Lower bound inclusive, upper exclusive
# so a `now` exactly at Sun 22:00 UTC is treated as open.
_FRIDAY = 4
_SATURDAY = 5
_SUNDAY = 6
_FOREX_CLOSE_HOUR_UTC = 22


def is_forex_closed(now: datetime) -> bool:
    """True iff Fri 22:00 UTC <= now < Sun 22:00 UTC.

    `now` must be tz-aware UTC.
    """
    wd = now.weekday()
    if wd == _SATURDAY:
        return True
    if wd == _FRIDAY and now.hour >= _FOREX_CLOSE_HOUR_UTC:
        return True
    if wd == _SUNDAY and now.hour < _FOREX_CLOSE_HOUR_UTC:
        return True
    return False


def fx_close_floor(now: datetime) -> datetime:
    """Return the Friday 22:00:00 UTC of the closure that contains
    `now`. Caller is expected to pass a `now` for which
    `is_forex_closed(now)` is True; behavior outside that window is
    undefined.

    The closure floor is the lower bound the latest FX bar must
    satisfy during the closed window for the data to count as
    healthy (no outage during the close).
    """
    wd = now.weekday()
    # Days back to the most recent Friday: Fri=0, Sat=1, Sun=2.
    days_back = (wd - _FRIDAY) % 7
    floor_date = (now - timedelta(days=days_back)).date()
    return datetime(
        floor_date.year, floor_date.month, floor_date.day,
        _FOREX_CLOSE_HOUR_UTC, 0, 0, tzinfo=timezone.utc,
    )
```

Add `timezone` to the imports at the top of the file:

```python
from datetime import date, datetime, timedelta, timezone
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/pipelines/test_market_calendar.py -v`
Expected: All tests pass (12 from earlier + 9 new = 21).

- [ ] **Step 5: Commit**

```bash
git add pipelines/market_calendar.py tests/pipelines/test_market_calendar.py
git commit -m "$(cat <<'EOF'
feat(market_calendar): add is_forex_closed and fx_close_floor

Closed window: Fri 22:00 UTC <= now < Sun 22:00 UTC. Floor is the
Friday 22:00 UTC of the active closure — used as the lower bound
the latest FX bar must satisfy during the closed window so that
real outages starting before the close are still detected.

Part of KI-128 fix.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Update `pipelines/freshness.py::check_fx_freshness` for forex-closed window

**Files:**
- Modify: `pipelines/freshness.py:124-148` (`check_fx_freshness`)
- Modify: `tests/equity/test_pipeline_freshness.py` (append new cases)

- [ ] **Step 1: Write failing tests**

Append to `tests/equity/test_pipeline_freshness.py`:

```python
# ──────────────────────────────────────────────────────────────────────
# FX freshness — forex-closed window (KI-128)
# ──────────────────────────────────────────────────────────────────────


def test_fx_freshness_during_close_with_pre_close_bar_is_fresh(temp_db):
    # Sat 2026-05-16 12:00 UTC; latest bar Fri 21:55 UTC (last bar
    # before close). is_fresh because latest >= fx_close_floor (Fri 22:00).
    # Wait — Fri 21:55 is BEFORE Fri 22:00, but the actual last bar
    # written at the moment of close lands at Fri 21:00 (top of
    # hour) since the FX hourly schedule fires at :05 reading the
    # most recent completed hour. Use Fri 21:00 as the "last bar
    # before close".
    from datetime import datetime as _dt
    now = _dt(2026, 5, 16, 12, 0, 0)
    bar = _dt(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert rep.is_fresh, f"expected fresh during close window with pre-close bar; msg={rep.message}"


def test_fx_freshness_during_close_with_outage_in_flight_is_stale(temp_db):
    # Sat 12:00 UTC; latest bar Wed 10:00 UTC — outage started long
    # before forex closed; latest is BEFORE fx_close_floor.
    from datetime import datetime as _dt
    now = _dt(2026, 5, 16, 12, 0, 0)
    bar = _dt(2026, 5, 13, 10, 0, 0)  # Wed 10:00 UTC
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert not rep.is_fresh, "outage during close window must still be flagged"


def test_fx_freshness_post_resume_with_stale_data_is_stale(temp_db):
    # Sun 23:00 UTC — closed window ended at Sun 22:00. 2h budget
    # active. latest = Fri 21:00 UTC (older than 2h) → stale.
    from datetime import datetime as _dt
    now = _dt(2026, 5, 17, 23, 0, 0)
    bar = _dt(2026, 5, 15, 21, 0, 0)
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, gbpeur_close, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [bar, bar.date(), bar.strftime("%A"), bar.hour, 1.18, "OK"],
    )
    rep = check_fx_freshness(temp_db, now=now)
    assert not rep.is_fresh
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/equity/test_pipeline_freshness.py -v -k "freshness_during_close or freshness_post_resume"`
Expected: At least one fails because `check_fx_freshness` doesn't yet branch on the closed window — the `freshness_during_close_with_pre_close_bar_is_fresh` test fails (current behavior: 39h-old bar is stale on a 2h budget).

- [ ] **Step 3: Update `check_fx_freshness` to branch on the closed window**

In `pipelines/freshness.py`, **extend** the existing import line that
Task 1 added (`from pipelines.market_calendar import trading_days_between`)
into the multi-name form:

```python
from pipelines.market_calendar import (
    trading_days_between,
    is_forex_closed,
    fx_close_floor,
)
```

(Verify with `grep -n "from pipelines.market_calendar" pipelines/freshness.py`
that there is exactly one such import line afterward — no duplicate.)

Replace the body of `check_fx_freshness` (lines ~124-148) with:

```python
def check_fx_freshness(
    conn: duckdb.DuckDBPyConnection,
    now: Optional[datetime] = None,
    max_hours: int = 2,
) -> FreshnessReport:
    now = now or datetime.now(tz=timezone.utc).replace(tzinfo=None)
    row = conn.execute("SELECT MAX(datetime_utc) FROM fx_prices_hourly").fetchone()
    latest = row[0] if row else None

    if latest is None:
        return FreshnessReport(
            engine="fx", is_fresh=False, latest=None, age=None,
            age_str="n/a", threshold=f"{max_hours}h",
            message="fx_prices_hourly is empty",
        )

    # `now` enters tz-naive (the existing contract); helpers expect
    # tz-aware UTC. Convert at the boundary, branch, then return.
    now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    if is_forex_closed(now_aware):
        floor = fx_close_floor(now_aware).replace(tzinfo=None)
        is_fresh = latest >= floor
        age = now - latest
        msg = (
            f"FX fx_prices_hourly latest={latest} during forex-closed "
            f"window; floor={floor} (KI-128)"
        )
        return FreshnessReport(
            engine="fx", is_fresh=is_fresh, latest=latest, age=age,
            age_str=_format_age(age), threshold=f"forex-closed floor {floor}",
            message=msg,
        )

    age = now - latest
    is_fresh = age <= timedelta(hours=max_hours)
    msg = (f"FX fx_prices_hourly latest={latest} "
           f"(age={_format_age(age)}; threshold={max_hours}h)")
    return FreshnessReport(
        engine="fx", is_fresh=is_fresh, latest=latest, age=age,
        age_str=_format_age(age), threshold=f"{max_hours}h",
        message=msg,
    )
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `.venv/bin/python -m pytest tests/equity/test_pipeline_freshness.py -v`
Expected: All tests pass (existing FX cases still work; 3 new cases pass).

- [ ] **Step 5: Commit**

```bash
git add pipelines/freshness.py tests/equity/test_pipeline_freshness.py
git commit -m "$(cat <<'EOF'
fix(freshness): forex-closed window aware FX freshness check (KI-128)

check_fx_freshness now branches on is_forex_closed: inside the
window (Fri 22:00 UTC -> Sun 22:00 UTC) it asserts latest >=
fx_close_floor (Fri 22:00 UTC of the active closure) so real
outages during the close are still detected; outside the window
the existing 2h budget applies.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Update `pipelines/health_check.py::_check_equity` to use `expected_equity_prediction_date`

**Files:**
- Modify: `pipelines/health_check.py:34-51` (`_check_equity`)
- Create: `tests/pipelines/test_health_check_weekend.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/pipelines/test_health_check_weekend.py`:

```python
"""KI-128: weekday-aware health check behavior.

Pins the regression that pipelines/health_check.py::_check_equity
must NOT alert on Sun/Mon mornings when Friday's equity row exists
(the ML predict pipeline writes prediction_date = last closed
market day, which is Friday from Sat 00:15 UTC through Tue 00:14
UTC).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch


def _utc(year, month, day, hour=6) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def _patch_now(now: datetime):
    return patch("pipelines.health_check._today_utc", return_value=now)


def test_equity_check_ok_on_saturday_with_friday_row(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 16)):  # Saturday
        result = _check_equity(temp_db)
    assert result.ok, f"expected ok on Sat with Fri row; detail={result.detail}"


def test_equity_check_ok_on_sunday_with_friday_row(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 17)):  # Sunday — KI-128 regression
        result = _check_equity(temp_db)
    assert result.ok, f"expected ok on Sun with Fri row; detail={result.detail}"


def test_equity_check_ok_on_monday_with_friday_row(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 18)):  # Monday — KI-128 regression
        result = _check_equity(temp_db)
    assert result.ok, f"expected ok on Mon with Fri row; detail={result.detail}"


def test_equity_check_fails_on_monday_when_friday_row_missing(temp_db):
    """Outage detection must still work — empty predictions on Mon
    means the Friday fire never produced a row."""
    from pipelines.health_check import _check_equity
    with _patch_now(_utc(2026, 5, 18)):
        result = _check_equity(temp_db)
    assert not result.ok
    assert "no rows" in result.detail.lower() or "expected" in result.detail.lower()


def test_equity_check_ok_on_tuesday_with_monday_row(temp_db):
    from pipelines.health_check import _check_equity
    mon = date(2026, 5, 18)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [mon],
    )
    with _patch_now(_utc(2026, 5, 19)):  # Tuesday
        result = _check_equity(temp_db)
    assert result.ok


def test_equity_check_fails_on_tuesday_when_monday_row_missing(temp_db):
    from pipelines.health_check import _check_equity
    fri = date(2026, 5, 15)
    temp_db.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
        "horizon, predicted_probability, prediction_threshold) "
        "VALUES ('AAA', ?, 'm1', '5d', 0.6, 0.05)",
        [fri],
    )
    with _patch_now(_utc(2026, 5, 19)):  # Tuesday but only Fri row exists
        result = _check_equity(temp_db)
    assert not result.ok
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/pipelines/test_health_check_weekend.py -v -k equity`
Expected: `test_equity_check_ok_on_sunday_with_friday_row`, `test_equity_check_ok_on_monday_with_friday_row` FAIL (current code uses `yesterday` literal — fails for Sun/Mon when only Fri row exists). The `fails_*` tests may already pass.

- [ ] **Step 3: Update `_check_equity` to use the helper**

Edit `pipelines/health_check.py`. At the top of the file, add the import alongside existing ones:

```python
from pipelines.market_calendar import expected_equity_prediction_date
```

Replace the body of `_check_equity` (lines 34-51):

```python
def _check_equity(conn: duckdb.DuckDBPyConnection) -> CheckResult:
    """Equity predict runs at 00:15 UTC and writes prediction_date = latest
    closed market day. By 06:00 UTC we expect rows for the most recent
    weekday strictly before today (Fri on Sat/Sun/Mon mornings; Mon on
    Tue; etc.). See KI-128 / ADR-018 for the weekday gate.
    """
    expected = expected_equity_prediction_date(_today_utc())
    row = conn.execute(
        "SELECT COUNT(*) FROM ml_predictions WHERE prediction_date = ?",
        [expected],
    ).fetchone()
    n = row[0] if row else 0
    if n > 0:
        return CheckResult("equity", True, f"{n} predictions for {expected}")
    latest = conn.execute(
        "SELECT MAX(prediction_date) FROM ml_predictions"
    ).fetchone()[0]
    return CheckResult(
        "equity", False,
        f"no rows for expected={expected}; latest prediction_date={latest}",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/pipelines/test_health_check_weekend.py -v -k equity`
Expected: All 6 equity tests pass.

- [ ] **Step 5: Run cross_artifact tests to verify the detail-string regex still matches**

The cross_artifact monitor parses `_check_equity`'s detail string with a regex (`tests/equity/test_monitoring.py:391`). Confirm the message format change ("predictions for {expected}" replacing "predictions for {yesterday}") still parses.

Run: `.venv/bin/python -m pytest tests/equity/test_monitoring.py -v -k cross_artifact`
Expected: All cross_artifact tests pass.

The regex in `monitoring/cross_artifact.py:45` is
`r"^(\d+) predictions for (\d{4}-\d{2}-\d{2})$"` — it matches on the
date *format* (YYYY-MM-DD) not the variable name, so the new
`f"{n} predictions for {expected}"` string will match. The line-17
comment in `cross_artifact.py` still says `{yesterday}`; leave it
for the operator to update post-merge (cosmetic).

- [ ] **Step 6: Commit**

```bash
git add pipelines/health_check.py tests/pipelines/test_health_check_weekend.py
git commit -m "$(cat <<'EOF'
fix(health_check): weekday-aware equity recency (KI-128)

_check_equity now expects the most recent Mon-Fri strictly before
today (delegating to market_calendar.expected_equity_prediction_date)
instead of the literal `now - 1d`. Eliminates Sun/Mon morning
false-positive Telegram alerts. Outage detection preserved: a
missing Friday row on Monday morning still fails the check.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## CHECKPOINT 1 — pause for operator review

Stop here, summarize what's done (Tasks 1-5), and wait for operator approval before continuing to the FX leg of `health_check` and `pipeline_execution`. Reasoning: the equity fix is the higher-confidence change; the FX forex-closed branching is more nuanced and a sanity check from the operator is worth it before propagating to two more callers.

---

## Task 6: Update `pipelines/health_check.py::_check_fx` for forex-closed window

**Files:**
- Modify: `pipelines/health_check.py:76-94` (`_check_fx`)
- Modify: `tests/pipelines/test_health_check_weekend.py` (add FX cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/pipelines/test_health_check_weekend.py`:

```python
def test_fx_check_ok_on_saturday_with_pre_close_bar(temp_db):
    """KI-128: forex closed Fri 22:00 UTC → Sun 22:00 UTC. With a
    bar at Fri 21:00 UTC the check must pass."""
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC, naive
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 16, 12)):  # Sat 12:00 UTC
        result = _check_fx(temp_db)
    assert result.ok, f"expected ok during close with pre-close bar; detail={result.detail}"


def test_fx_check_ok_on_sunday_evening_with_pre_close_bar(temp_db):
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 17, 21)):  # Sun 21:00 UTC, still closed
        result = _check_fx(temp_db)
    assert result.ok


def test_fx_check_fails_during_close_with_outage_in_flight(temp_db):
    """Real outage starting before the close: latest predates fx_close_floor."""
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 13, 10, 0, 0)  # Wed 10:00 UTC
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 16, 12)):  # Sat 12:00 UTC
        result = _check_fx(temp_db)
    assert not result.ok
    assert "floor" in result.detail.lower() or "predates" in result.detail.lower() or "older" in result.detail.lower()


def test_fx_check_fails_post_resume_with_stale_data(temp_db):
    """Sun 23:00 UTC — closed window ended at Sun 22:00. 2h budget
    active. Stale Friday bar must alert."""
    from pipelines.health_check import _check_fx
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(_utc(2026, 5, 17, 23)):  # Sun 23:00 UTC, post-resume
        result = _check_fx(temp_db)
    assert not result.ok


def test_fx_check_ok_midweek_with_recent_bar(temp_db):
    """Sanity: existing 2h-budget behavior unchanged outside the window."""
    from pipelines.health_check import _check_fx
    now = _utc(2026, 5, 13, 12)  # Wed 12:00 UTC
    fx_dt = (now - timedelta(hours=1)).replace(tzinfo=None)
    temp_db.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
        "direction, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
        [fx_dt],
    )
    with _patch_now(now):
        result = _check_fx(temp_db)
    assert result.ok
```

Add `from datetime import timedelta` to the imports at the top of the test file if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/pipelines/test_health_check_weekend.py -v -k "fx_check"`
Expected: The two `_ok_*_with_pre_close_bar` tests fail (current 2h budget rejects a 39h-old bar).

- [ ] **Step 3: Update `_check_fx`**

Edit `pipelines/health_check.py`. Update the existing `from pipelines.market_calendar import` line to include the FX helpers:

```python
from pipelines.market_calendar import (
    expected_equity_prediction_date,
    is_forex_closed,
    fx_close_floor,
)
```

Replace `_check_fx` (lines 76-94) with:

```python
def _check_fx(conn: duckdb.DuckDBPyConnection) -> CheckResult:
    """FX predict runs hourly. Outside the forex-closed window we expect
    a prediction within the last 2 hours. Inside Fri 22:00 UTC →
    Sun 22:00 UTC the latest must be at or after the close floor
    (Fri 22:00 UTC of the active closure) — preserves outage
    detection during the close. See KI-128 / ADR-018.
    """
    now = _today_utc()
    row = conn.execute(
        "SELECT MAX(datetime_utc) FROM fx_ml_predictions"
    ).fetchone()
    latest = row[0] if row else None

    if is_forex_closed(now):
        floor_naive = fx_close_floor(now).replace(tzinfo=None)
        logger.info(
            "fx forex-closed window — asserting latest >= %s (KI-128)",
            floor_naive,
        )
        if latest is not None and latest >= floor_naive:
            sig_row = conn.execute(
                "SELECT signal_type, datetime_utc FROM fx_signals "
                "ORDER BY datetime_utc DESC LIMIT 1"
            ).fetchone()
            latest_sig = f"{sig_row[0]} @ {sig_row[1]}" if sig_row else "no signal"
            return CheckResult(
                "fx", True,
                f"latest bar {latest} UTC (forex-closed; floor={floor_naive}); "
                f"latest signal: {latest_sig}",
            )
        return CheckResult(
            "fx", False,
            f"latest prediction {latest} predates forex-close floor "
            f"{floor_naive} — outage during closed window",
        )

    threshold = now - timedelta(hours=2)
    threshold_naive = threshold.replace(tzinfo=None)
    if latest is not None and latest >= threshold_naive:
        sig_row = conn.execute(
            "SELECT signal_type, datetime_utc FROM fx_signals "
            "ORDER BY datetime_utc DESC LIMIT 1"
        ).fetchone()
        latest_sig = f"{sig_row[0]} @ {sig_row[1]}" if sig_row else "no signal"
        return CheckResult("fx", True, f"latest bar {latest} UTC; latest signal: {latest_sig}")
    return CheckResult(
        "fx", False,
        f"latest prediction {latest} is older than 2h (threshold {threshold_naive})",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/pipelines/test_health_check_weekend.py -v`
Expected: all 11 tests pass (6 equity + 5 fx).

- [ ] **Step 5: Run cross_artifact + monitoring suite to confirm no breakage**

Run: `.venv/bin/python -m pytest tests/equity/test_monitoring.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add pipelines/health_check.py tests/pipelines/test_health_check_weekend.py
git commit -m "$(cat <<'EOF'
fix(health_check): forex-closed window aware FX check (KI-128)

_check_fx now branches on is_forex_closed: inside Fri 22:00 UTC ->
Sun 22:00 UTC the latest fx_ml_predictions row must be at or after
fx_close_floor (Fri 22:00 UTC of the active closure); outside the
window the existing 2h budget applies. Eliminates the Fri 22:00 UTC
-> Sun 22:00 UTC weekend false-positive while preserving outage
detection during the close.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Update `monitoring/pipeline_execution.py` FX leg for forex-closed window

**Files:**
- Modify: `monitoring/pipeline_execution.py:61-156` (`_check_engine_pipeline`)
- Create: `tests/regression/test_pipeline_execution_weekend.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/regression/test_pipeline_execution_weekend.py`:

```python
"""KI-128 regression: pipeline_execution FX leg honors the forex-closed
window. Equity 75h budget already covers the weekend per ADR-015 and
is pinned here as a regression to ensure the refactor doesn't change it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


def _seed_active_models(conn):
    """Insert one active model per engine so n_active > 0 in each."""
    conn.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('eq', '20d', 0.10, '/tmp/x', true)"
    )
    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('cr', '5d', 0.10, '/tmp/x', true)"
    )
    conn.execute(
        "INSERT INTO fx_ml_model_runs (model_id, direction, horizon, "
        "target_pips, model_path, is_active) "
        "VALUES ('fx_m1', 'up', '24h', 20, '/tmp/x', true)"
    )


def _seed_baseline_predictions(conn, eq_date: date, cr_date: date, fx_dt: datetime):
    """Seed enough baseline rows so the 14-day baseline doesn't trip
    the count check. We're testing recency, not row counts."""
    # Equity 14-day history: 30 rows/day for 14 days ending eq_date.
    for d_offset in range(15):
        d = eq_date - timedelta(days=d_offset)
        for i in range(30):
            conn.execute(
                "INSERT INTO ml_predictions (ticker, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'eq', '20d', 0.6, 0.10)",
                [f"T{i}", d],
            )
    # Crypto 14-day history.
    for d_offset in range(15):
        d = cr_date - timedelta(days=d_offset)
        for i in range(30):
            conn.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'cr', '5d', 0.6, 0.10)",
                [f"S{i}USDT", d],
            )
    # FX 14-day baseline of hourly rows ending at fx_dt. Use 5/day to
    # stay above the n_avg > 5 gate.
    for d_offset in range(15):
        for h in range(5):
            ts = fx_dt - timedelta(days=d_offset, hours=h)
            conn.execute(
                "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
                "direction, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
                [ts],
            )


def test_pipeline_execution_fx_ok_during_close_with_pre_close_bar(temp_db):
    """Sat 12:00 UTC, latest FX bar at Fri 21:00 UTC — must pass."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)  # Fri
    cr_date = date(2026, 5, 16)  # Sat
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)  # Sat
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["fx"]["recency_ok"] is True, (
        f"fx recency must pass during forex close with pre-close bar; "
        f"reason={result.metrics['fx'].get('reason')}"
    )


def test_pipeline_execution_fx_fails_during_close_with_outage(temp_db):
    """Sat 12:00 UTC, latest FX bar at Wed 10:00 UTC — outage in flight."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)
    cr_date = date(2026, 5, 16)
    fx_dt = datetime(2026, 5, 13, 10, 0, 0)  # Wed
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["fx"]["recency_ok"] is False
    reason = result.metrics["fx"].get("reason", "")
    assert "closed window" in reason.lower() or "floor" in reason.lower() or "predates" in reason.lower(), (
        f"reason should call out outage during closed window; got {reason!r}"
    )


def test_pipeline_execution_fx_fails_post_resume_with_stale_data(temp_db):
    """Sun 23:00 UTC — post-resume, 2h budget active. Stale Fri bar fails."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)
    cr_date = date(2026, 5, 17)
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 17, 23, 0, 0, tzinfo=timezone.utc)
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["fx"]["recency_ok"] is False


def test_pipeline_execution_equity_ok_on_monday_morning(temp_db):
    """Pinned regression: ADR-015's 75h equity budget covers Mon morning
    when the latest prediction_date is Friday. Must remain unchanged
    by the KI-128 refactor."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)  # Fri
    cr_date = date(2026, 5, 18)  # Mon
    fx_dt = datetime(2026, 5, 18, 5, 0, 0)
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 18, 6, 0, 0, tzinfo=timezone.utc)  # Mon 06:00
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["equity"]["recency_ok"] is True, (
        f"equity 75h budget should still cover Mon 06:00 with Fri data; "
        f"reason={result.metrics['equity'].get('reason')}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/regression/test_pipeline_execution_weekend.py -v`
Expected: `_fx_ok_during_close_with_pre_close_bar` fails (current FX 2h budget rejects a 39h-old bar).

- [ ] **Step 3: Update `_check_engine_pipeline` for the FX forex-closed branch**

Edit `monitoring/pipeline_execution.py`. Add the import at the top alongside existing ones:

```python
from pipelines.market_calendar import is_forex_closed, fx_close_floor
```

Inside `_check_engine_pipeline`, replace the recency block (lines 104-114):

```python
    # Recency check
    if isinstance(latest, datetime):
        latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    else:  # date
        latest_dt = datetime.combine(latest, datetime.min.time(), tzinfo=timezone.utc)
    age = now - latest_dt
    if age > RECENCY_BUDGET[engine]:
        out["recency_ok"] = False
        out["reason"] = (
            f"latest {date_col}={latest} is {age} old, threshold "
            f"{RECENCY_BUDGET[engine]}"
        )
```

with:

```python
    # Recency check.
    if isinstance(latest, datetime):
        latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    else:  # date
        latest_dt = datetime.combine(latest, datetime.min.time(), tzinfo=timezone.utc)

    if engine == "fx" and is_forex_closed(now):
        floor = fx_close_floor(now)
        logger.info(
            "fx forex-closed window — asserting latest >= %s (KI-128)",
            floor.isoformat(),
        )
        if latest_dt < floor:
            out["recency_ok"] = False
            out["reason"] = (
                f"latest {date_col}={latest} predates forex-close floor "
                f"{floor.isoformat()} — outage during closed window"
            )
        # else: fx healthy during the close; skip the 2h budget.
    else:
        age = now - latest_dt
        if age > RECENCY_BUDGET[engine]:
            out["recency_ok"] = False
            out["reason"] = (
                f"latest {date_col}={latest} is {age} old, threshold "
                f"{RECENCY_BUDGET[engine]}"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/regression/test_pipeline_execution_weekend.py tests/regression/test_pipeline_execution_baseline.py -v`
Expected: all pass — new weekend tests + the existing baseline regression suite green.

- [ ] **Step 5: Run the full monitoring test suite**

Run: `.venv/bin/python -m pytest tests/equity/test_monitoring.py tests/regression -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add monitoring/pipeline_execution.py tests/regression/test_pipeline_execution_weekend.py
git commit -m "$(cat <<'EOF'
fix(monitor): forex-closed window aware FX recency (KI-128)

pipeline_execution._check_engine_pipeline FX branch now defers to
market_calendar.is_forex_closed: inside Fri 22:00 UTC -> Sun 22:00 UTC
the latest must satisfy >= fx_close_floor (Fri 22:00 UTC of the
active closure); outside the window the existing 2h RECENCY_BUDGET
applies. Equity / crypto branches unchanged. Mirrors the same gate
applied in pipelines/health_check.py and pipelines/freshness.py.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Add ADR-018 + update KNOWN_ISSUES.md

**Files:**
- Modify: `DECISIONS.md` (append ADR-018)
- Modify: `KNOWN_ISSUES.md` (move KI-128 to "Recently resolved")

- [ ] **Step 1: Append ADR-018 to DECISIONS.md**

Find the end of the file (after ADR-017) and append:

```markdown


---

## ADR-018 — Weekday/forex-closed gates for health_check + pipeline_execution

**Date:** 2026-05-10
**Session:** KI-128 fix
**Status:** Active
**Builds on:** ADR-015 (asymmetric pipeline_execution recency budgets)

**Context.** ADR-015 widened equity's `pipeline_execution`
RECENCY_BUDGET to 75h to absorb the Fri→Tue weekend roll. Two
sibling code paths still tripped on weekends:

- `pipelines/health_check.py::_check_equity` queried for
  `prediction_date = (now - 1d).date()`, which silently asked for
  Sat or Sun rows on Sun/Mon mornings — neither exists because
  NYSE is closed.
- `pipelines/health_check.py::_check_fx`,
  `monitoring/pipeline_execution.py` (FX leg), and
  `pipelines/freshness.py::check_fx_freshness` used a fixed 2h
  budget that fails through the entire forex weekend close
  (Fri 22:00 UTC → Sun 22:00 UTC).

Result: a predictable Telegram false alert every weekend.

**Decision.** Add `pipelines/market_calendar.py` as a single source
of truth and gate the three call sites on its helpers:

| Helper | Used by |
|---|---|
| `expected_equity_prediction_date(now)` — most recent Mon-Fri strictly before `now.date()` | `_check_equity` |
| `is_forex_closed(now)` — True iff Fri 22:00 UTC ≤ now < Sun 22:00 UTC | `_check_fx`, `pipeline_execution` FX leg, `check_fx_freshness` |
| `fx_close_floor(now)` — Fri 22:00 UTC of the active closure | same three call sites |
| `trading_days_between(start, end)` — moved from `freshness.py` | `check_equity_freshness` |

During `is_forex_closed(now)`, the gate becomes
`latest >= fx_close_floor(now)` instead of the wall-clock budget.
This preserves outage detection during the close (a real ingestion
failure that started before Fri 22:00 UTC still fails the gate).

**Holidays remain operator-acknowledged.** Mirrors ADR-015's
trade-off. NYSE-closed Fridays (Thanksgiving, Good Friday) and
Mondays (MLK, Memorial Day) will produce one warn the day after
because the helper expects a "weekday", not a "trading day". Adding
a holiday calendar (e.g. `pandas_market_calendars`) was rejected:

- Adds a runtime dependency for ~10 noise-suppressed days/year.
- The holiday list itself drifts (markets occasionally announce
  closures) and would need maintenance.
- A holiday warn is informational anyway — operator notes the
  date and moves on.

**Why not raise FX `RECENCY_BUDGET` to 50h to absorb the weekend?**
Weakens active-hours outage detection: a real FX ingestion failure
on Tuesday wouldn't alert until Thursday. The gate-on-window
approach keeps the active-hours budget at 2h.

**Why not add `row_inserted_at TIMESTAMP` and key recency off
write time?** Same answer as ADR-015: better long-term, requires
schema migration, out of scope here.

**Configurability.** No env var. Adding a kill-switch creates a
"forgot to set it back" failure surface and these market hours
don't change.
```

- [ ] **Step 2: Update KNOWN_ISSUES.md**

Two edits in `KNOWN_ISSUES.md`:

1. At line 3, decrement the open count: `**4 open observations**` → `**3 open observations**` (and remove `KI-128` from the parenthetical list).

2. Move the entire `### KI-128 — Health check thresholds don't account for weekend market closure` block (lines ~108-135) from its current position into the "Recently resolved" section. Replace its body with:

```markdown
### KI-128 — Health check thresholds don't account for weekend market closure

**Resolved 2026-05-10.** Fixed via ADR-018. Added
`pipelines/market_calendar.py` with `expected_equity_prediction_date`,
`is_forex_closed`, and `fx_close_floor` helpers. Three callers gate
their existing recency checks on these helpers:

- `pipelines/health_check.py::_check_equity` — uses
  `expected_equity_prediction_date(now)` instead of the literal
  `now - 1d`. No more Sun/Mon false positives.
- `pipelines/health_check.py::_check_fx` and
  `monitoring/pipeline_execution.py` (FX leg) and
  `pipelines/freshness.py::check_fx_freshness` — branch on
  `is_forex_closed(now)`; inside the window the gate is
  `latest >= fx_close_floor(now)`, outside it's the existing 2h
  budget. No more Fri 22:00 UTC → Sun 22:00 UTC false positives.

**Accepted limitation.** US market holidays still produce one warn
the day after (Thanksgiving Friday, MLK Monday, etc.). Mirrors
ADR-015's trade-off — a holiday calendar adds dependency surface
for a small noise reduction and weakens active-day outage
detection.
```

- [ ] **Step 3: Run a sanity test to make sure docs aren't malformed**

Run: `head -10 /home/jpcg/MHDE/KNOWN_ISSUES.md`
Expected: open-count line says "3 open observations" and the file is well-formed.

- [ ] **Step 4: Commit**

```bash
git add DECISIONS.md KNOWN_ISSUES.md
git commit -m "$(cat <<'EOF'
docs(decisions,known_issues): ADR-018 + KI-128 -> resolved

ADR-018 captures the weekday/forex-closed gates added in this
session. References ADR-015 for the budget-asymmetry precedent.
Holidays remain operator-acknowledged.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Final verification

**Files:** none modified.

- [ ] **Step 1: Run the full pytest suite for affected packages**

Run: `.venv/bin/python -m pytest tests/pipelines tests/equity/test_pipeline_freshness.py tests/equity/test_monitoring.py tests/regression -v 2>&1 | tail -40`
Expected: all green. No new failures introduced.

- [ ] **Step 2: Run the ML smoke health-check via the CLI script**

Run: `MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python .claude/local_scripts/test_health_check.py 2>&1 | tail -20`
Expected: Exits cleanly. (Real-world output depends on current DB state — operator inspects.)

- [ ] **Step 3: Append a session-log entry**

Append to `SESSION_LOG.md` a brief entry describing: branch name, files touched, KI-128 → resolved, ADR-018 written, before-merge state. Follow the format of the most recent entries.

- [ ] **Step 4: Commit the session log + push the branch**

```bash
git add SESSION_LOG.md
git commit -m "$(cat <<'EOF'
docs(session-log): KI-128 weekday-aware recency fix

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push -u origin ki128-weekday-aware-recency
```

(Stop here — do NOT merge. Operator reviews the branch and approves merge.)

---

## CHECKPOINT 2 — pre-merge operator review

The branch is ready for review. Summarize:

- **9 commits**: spec → market_calendar (3 helpers) → freshness FX → health_check equity → CHECKPOINT 1 → health_check FX → pipeline_execution FX → ADR-018 + KI-128 → session log.
- **Test count**: ~21 new market_calendar tests + ~11 health_check_weekend tests + ~4 pipeline_execution_weekend tests + 3 freshness FX tests = ~39 new tests.
- **Files modified**: `pipelines/market_calendar.py` (new), `pipelines/freshness.py`, `pipelines/health_check.py`, `monitoring/pipeline_execution.py`, `DECISIONS.md`, `KNOWN_ISSUES.md`, `SESSION_LOG.md`. Plus 4 test files.
- **Behavior**: weekend false positives eliminated; outage detection preserved during closures; holidays remain operator-acknowledged per ADR-015's precedent.

Wait for operator approval before merging.
