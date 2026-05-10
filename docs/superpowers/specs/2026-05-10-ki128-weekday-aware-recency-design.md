# KI-128 — Weekday-aware recency for health_check and pipeline_execution

**Date:** 2026-05-10
**Tracks:** `KNOWN_ISSUES.md` KI-128
**Builds on:** ADR-015 (asymmetric pipeline_execution recency budgets)
**Produces:** ADR-018 (this decision, captured for future readers)

## Problem

Wall-clock recency budgets in `pipelines/health_check.py` and
`monitoring/pipeline_execution.py` (FX leg) ignore market closures and
fire false-positive Telegram alerts every weekend.

| Engine | Health check break | Pipeline-execution break |
|---|---|---|
| Equity | Sun 06:00 UTC, Mon 06:00 UTC (yesterday=Sat or Sun, NYSE closed) | none — 75h budget per ADR-015 |
| FX     | Fri 22:00 UTC → Sun 22:00 UTC (forex closed) | Fri 22:00 UTC → Sun 22:00 UTC (2h budget) |
| Crypto | none — 24/7 | none — 27h budget |

`pipelines/freshness.py::check_fx_freshness` has the same FX bug
(2h budget, no closure awareness). Equity freshness already routes
through `_trading_days_between` and is correct.

## Goals

- Weekend Telegram alerts stop firing when both pipelines are
  healthy.
- A real outage that *starts* during a closure (e.g. ingestion fails
  Friday 18:00 UTC) is still detected, not masked by the closure
  window.
- No new external dependency (no `pandas_market_calendars`).
- All gating logic is pure, UTC-only, and accepts an injected `now`
  for deterministic tests.

## Non-goals

- **NYSE / forex public holidays.** A holiday-extended weekend
  (Thanksgiving Friday, Memorial Day Monday, etc.) produces one
  warn the day after; operator acknowledges. Mirrors ADR-015's
  accepted trade-off — adding a holiday calendar weakens outage
  detection on normal weeks for a small false-alert reduction on
  ~10 days/year.
- **Configurability.** No env var, no kill-switch. Adding a toggle
  introduces a "forgot to set it back" failure surface and these
  market hours don't change.
- **Schema migration.** ADR-015 captures the longer-term option of
  adding `row_inserted_at TIMESTAMP` to `ml_predictions` and keying
  recency off real write time. Out of scope here.

## Design

### New module: `pipelines/market_calendar.py`

Pure helpers, no DB, no network, no I/O. All times UTC. The module
is the single source of truth for market-clock decisions across
`health_check`, `pipeline_execution`, and `freshness`.

```python
def trading_days_between(start: date, end: date) -> int:
    """Inclusive Mon-Fri count between two dates (start <= end).
    Moved verbatim from pipelines/freshness.py so all market-clock
    helpers live together.
    """

def expected_equity_prediction_date(now: datetime) -> date:
    """Return the most recent Mon-Fri *strictly before* now.date().

    Equity ML predict runs at 00:15 UTC and writes
    prediction_date = "latest closed market day", which is the most
    recent weekday before today. By 06:00 UTC of any day, that's:

      Mon 06:00 UTC → Fri (Sat/Sun closed, so back to Fri)
      Tue 06:00 UTC → Mon
      Wed 06:00 UTC → Tue
      Thu 06:00 UTC → Wed
      Fri 06:00 UTC → Thu
      Sat 06:00 UTC → Fri
      Sun 06:00 UTC → Fri

    Replaces the literal `now.date() - 1` used previously, which
    silently broke on Sun and Mon (returning Sat/Sun, neither of
    which has equity data).
    """

def is_forex_closed(now: datetime) -> bool:
    """True iff Fri 22:00 UTC <= now < Sun 22:00 UTC. Forex spot
    market is closed in this window.
    """

def fx_close_floor(now: datetime) -> datetime:
    """For a `now` inside is_forex_closed(now), return the Friday
    22:00:00 UTC of the closure that's currently active. Used as
    the lower bound the latest FX bar must satisfy during the
    weekend window.
    """
```

### Edit: `pipelines/health_check.py`

`_check_equity`:

```python
expected = market_calendar.expected_equity_prediction_date(_today_utc())
row = conn.execute(
    "SELECT COUNT(*) FROM ml_predictions WHERE prediction_date = ?",
    [expected],
).fetchone()
# remainder unchanged; expected replaces yesterday in messages too
```

`_check_fx`:

```python
now = _today_utc()
if market_calendar.is_forex_closed(now):
    floor = market_calendar.fx_close_floor(now)
    floor_naive = floor.replace(tzinfo=None)
    logger.info("fx forex-closed window — asserting latest >= %s", floor_naive)
    # Pass iff the latest FX bar is at or after the close floor.
    # If a real outage started before the close, latest will be
    # earlier than floor and we still alert.
    threshold_naive = floor_naive
else:
    threshold = now - timedelta(hours=2)
    threshold_naive = threshold.replace(tzinfo=None)
# remainder of the comparison unchanged
```

### Edit: `monitoring/pipeline_execution.py`

FX path only. Equity is already correct via the 75h budget.

Inside `_check_engine_pipeline` (or factored to a small `_fx_recency_ok`
helper), when `engine == "fx"`:

```python
if market_calendar.is_forex_closed(now):
    floor = market_calendar.fx_close_floor(now)
    logger.info("fx forex-closed window — asserting latest >= %s", floor)
    if latest_dt < floor:
        out["recency_ok"] = False
        out["reason"] = (
            f"latest {date_col}={latest} predates forex close floor "
            f"{floor.isoformat()} — outage during closed window"
        )
    # else: pass without applying the 2h budget
else:
    age = now - latest_dt
    if age > RECENCY_BUDGET[engine]:
        out["recency_ok"] = False
        ...  # existing message
```

Equity / crypto branches keep the existing `age > RECENCY_BUDGET`
gate.

### Edit: `pipelines/freshness.py`

`check_fx_freshness`: identical pattern to health_check fx —
inside `is_forex_closed(now)`, the `is_fresh` decision is
`latest >= fx_close_floor(now)`; outside, the existing 2h budget.

`check_equity_freshness`: unchanged behaviorally; the import line
moves from local `_trading_days_between` to
`market_calendar.trading_days_between`.

## Consumers and call graph

```
                ┌────────────────────────────────────────┐
                │  pipelines/market_calendar.py          │
                │  trading_days_between,                 │
                │  expected_equity_prediction_date,      │
                │  is_forex_closed, fx_close_floor       │
                └─────────┬─────────┬─────────┬──────────┘
                          │         │         │
              ┌───────────┘         │         └───────────┐
              ▼                     ▼                     ▼
   pipelines/health_check.py   monitoring/             pipelines/
   (_check_equity,             pipeline_execution.py   freshness.py
    _check_fx)                 (FX leg)                (check_*_freshness)
```

`monitoring/cross_artifact.py` calls `health_check` internals and
inherits the fix transparently — no edits.

## Test plan

All tests use injected `now` (datetime, UTC) for determinism. The
existing modules already accept this.

### `tests/pipelines/test_market_calendar.py` (new)

Pure unit tests of the new helpers:

- `expected_equity_prediction_date` (most recent Mon-Fri strictly
  before now.date()):
  - Fri 06:00 UTC → Thu.
  - Sat 06:00 UTC → Fri.
  - Sun 06:00 UTC → Fri.
  - Mon 06:00 UTC → Fri.
  - Tue 06:00 UTC → Mon.
  - Wed 06:00 UTC → Tue.
  - Thu 06:00 UTC → Wed.
- `is_forex_closed`:
  - Fri 21:59 UTC → False.
  - Fri 22:00 UTC → True.
  - Sat 12:00 UTC → True.
  - Sun 21:59 UTC → True.
  - Sun 22:00 UTC → False.
- `fx_close_floor`:
  - Sat 12:00 UTC → Fri 22:00 UTC of that week.
  - Sun 21:59 UTC → Fri 22:00 UTC of that week.
- `trading_days_between` parity with the old implementation
  (sanity check after the move).

### `tests/equity/test_health_weekday_aware.py` (new)

Patch `pipelines.health_check._today_utc` to a controlled UTC datetime.

- Sat 06:00 UTC, Fri's row exists → equity passes.
- Sun 06:00 UTC, Fri's row exists → equity passes (regression: was failing).
- Mon 06:00 UTC, Fri's row exists → equity passes (regression).
- Mon 06:00 UTC, Fri's row missing → equity fails (outage detection
  preserved).
- Tue 06:00 UTC, Mon's row exists → equity passes.
- Tue 06:00 UTC, Mon's row missing → equity fails.

FX cases (use `_check_fx` direct or full `run_health_check` plus
fixture data):

- Sat 12:00 UTC, latest = Fri 21:55 UTC → fx passes.
- Sat 12:00 UTC, latest = Wed 10:00 UTC → fx fails (outage during
  closed window).
- Sun 23:00 UTC (forex resumed), latest = Sun 22:30 UTC → fx passes.
- Sun 23:00 UTC, latest = Fri 21:55 UTC → fx fails (post-resume,
  back on the 2h budget — regression: would have masked the outage).
- Mon 12:00 UTC, latest 1h old → fx passes.

Crypto cases: unchanged behavior.

### `tests/regression/test_pipeline_execution_weekend.py` (new)

Companion to `test_pipeline_execution_baseline.py`. Use the
existing `temp_db` fixture and patch `now`.

- Sat 12:00 UTC, FX latest = Fri 21:55 UTC → fx leg ok.
- Sat 12:00 UTC, FX latest = Wed 10:00 UTC → fx leg flagged with
  "outage during closed window".
- Sun 23:30 UTC, FX latest = Fri 21:55 UTC → fx leg flagged on the
  2h budget (closure ended at 22:00).
- Mon 06:00 UTC, equity latest = Fri → equity leg ok (existing 75h
  budget already covers this; pinned as a regression to ensure the
  refactor doesn't change it).

### `tests/equity/test_data_freshness.py` (extend)

Add forex-window cases for `check_fx_freshness` mirroring the
health_check FX matrix above.

### Existing tests

- `tests/regression/test_pipeline_execution_baseline.py` —
  unchanged. Already pins `now=datetime.utcnow()...`; behavior
  must remain stable.
- `tests/equity/test_monitoring.py::_seed_minimal_health_data` —
  may become flaky on Sun/Mon CI runs *after* the fix (it seeds
  yesterday's row, but the equity check now expects last weekday).
  Confirm and pin `now` if needed.

## Backwards compatibility

- Telegram message format for `_check_equity` keeps the same
  shape; `yesterday` in messages becomes `expected` (still a
  date). `cross_artifact.py` parses the count from the detail
  string with a regex (`tests/equity/test_monitoring.py:391`
  shows the format) — verify the message still matches.
- `pipeline_execution` `MonitorResult.metrics` dict keeps the
  same keys (`recency_ok`, `count_ok`, `latest`, `n_latest`,
  `n_avg`, `reason`). No downstream consumer change.
- `RECENCY_BUDGET["fx"]` stays at 2h. The closed-window branch
  is a separate gate, not a budget change.

## ADR-018 (to be written)

Captures: weekday/forex-window aware gates in
`pipelines/health_check.py` and `monitoring/pipeline_execution.py`
(FX leg) and `pipelines/freshness.py` (FX leg). Holidays remain
operator-acknowledged (mirrors ADR-015). Single source of truth in
`pipelines/market_calendar.py`. References ADR-015 for the
budget-asymmetry precedent.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Holiday alerts confuse operator | Documented in ADR-018; KNOWN_ISSUES KI-128 entry updated to point at the ADR for the accepted trade-off. |
| Sun→Mon boundary at 22:00 UTC: forex resumes but the first post-resume `fx_predict` row may be ~55min away (hourly at :05). | In a healthy system the first post-resume predict at Sun 22:05 UTC writes a fresh row, and by the time the hourly monitor runs at Sun 23:00 UTC, age ≈ 55min < 2h budget → ok. If an outage is in flight, `latest < fx_close_floor` catches it immediately at Sun 22:01 UTC. No additional grace period needed. |
| Refactor breaks `_trading_days_between` callers | Only one caller verified via grep (`pipelines/freshness.py:85`). Move + update import in same commit. |
| Tests rely on real `datetime.now()` | Existing modules already accept injected `now`. New tests patch `_today_utc` for `health_check` and pass `now=` for `pipeline_execution`. |
