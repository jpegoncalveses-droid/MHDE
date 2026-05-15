# BTC Regime Classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a daily-computed, rules-based BTC market regime classifier
(bull / neutral / bear), persisted to DuckDB and surfaced on the dashboard
top of the Crypto tab — observability only, no trading impact.

**Architecture:** Pure-function classifier in `monitoring/btc_regime.py`
reads BTCUSDT from `crypto_prices_daily`, computes five indicators
(50d/200d MAs, drawdown-from-ATH, 30d realized vol, 30d log-price slope),
applies three rules to label the day, persists to a new
`crypto_regime_daily` table. systemd timer runs daily at 00:30 UTC.
Dashboard reads the latest row + last 90d for a banner + shaded chart at
the top of the Crypto tab.

**Tech Stack:** Python 3, DuckDB, pandas/altair (existing
`dashboard/components/charts.py` conventions), pytest, systemd user
units (existing monitor-service pattern).

**Spec:** `docs/superpowers/specs/2026-05-15-btc-regime-classifier-design.md`
(commit `e8f910e` on branch `feat-btc-regime-classifier`).

---

## File Structure

```
NEW:
  monitoring/btc_regime.py
  systemd/mhde-monitor-btc-regime.service
  systemd/mhde-monitor-btc-regime.timer
  dashboard/components/regime_banner.py
  tests/crypto/test_btc_regime.py
  tests/dashboard/test_regime_queries.py
  tests/dashboard/test_regime_banner.py
  .claude/local_scripts/backfill_btc_2020.py
  .claude/local_scripts/backfill_btc_regime_history.py
  data/processed/btc_regime_history.parquet      (output of backfill — committed)
  data/processed/btc_regime_validation.md        (output of backfill — committed)

MODIFY:
  crypto/schema.py                  + SCHEMA_CRYPTO_REGIME_DAILY, append to ALL_SCHEMAS
  main.py                           + @monitor.command("btc-regime") subcommand
  dashboard/services/queries.py     + load_latest_btc_regime, load_btc_regime_history
  dashboard/app.py                  + render regime panel at top of tab_crypto (line 401)
  DATABASE_SCHEMA.md                + ### crypto_regime_daily subsection (Crypto ML group)
  INFRASTRUCTURE.md                 + entry for mhde-monitor-btc-regime.timer
  OPERATIONS.md                     + deploy steps subsection
  SESSION_LOG.md                    + session entry (final task)

ALREADY DONE (commit e8f910e):
  docs/superpowers/specs/2026-05-15-btc-regime-classifier-design.md
```

---

## Task 1: Add `crypto_regime_daily` table to schema

**Files:**
- Modify: `crypto/schema.py` (append a new schema constant before `ALL_SCHEMAS = [...]` and a new entry inside the list)
- Test: `tests/crypto/test_btc_regime.py` (NEW — initial single test creates the file)

**Why:** `create_all_tables()` is the single entry point for schema creation.
Adding the schema constant + list entry is the minimal change; an
in-memory DuckDB test confirms the table is created with the right
columns.

- [ ] **Step 1: Write the failing test**

Create `tests/crypto/test_btc_regime.py`:

```python
"""Unit tests for monitoring.btc_regime — BTC market regime classifier."""
from __future__ import annotations

import duckdb

from crypto.schema import create_all_tables


def test_crypto_regime_daily_table_is_created():
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    cols = conn.execute(
        "SELECT column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_name = 'crypto_regime_daily' "
        "ORDER BY ordinal_position"
    ).fetchall()
    assert cols == [
        ("trade_date", "DATE"),
        ("regime", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("indicators_json", "VARCHAR"),
        ("computed_at", "TIMESTAMP"),
    ]
    pk_cols = conn.execute(
        "SELECT constraint_column_names "
        "FROM duckdb_constraints() "
        "WHERE table_name = 'crypto_regime_daily' "
        "AND constraint_type = 'PRIMARY KEY'"
    ).fetchone()
    assert pk_cols == (["trade_date"],)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py::test_crypto_regime_daily_table_is_created -v`
Expected: FAIL — table `crypto_regime_daily` does not exist.

- [ ] **Step 3: Add schema constant and register it**

In `crypto/schema.py`, immediately before the `ALL_SCHEMAS = [` line, add:

```python
SCHEMA_CRYPTO_REGIME_DAILY = """
CREATE TABLE IF NOT EXISTS crypto_regime_daily (
    trade_date      DATE PRIMARY KEY,
    regime          VARCHAR NOT NULL,
    confidence      DOUBLE  NOT NULL,
    indicators_json VARCHAR NOT NULL,
    computed_at     TIMESTAMP NOT NULL
);
"""
```

Append `SCHEMA_CRYPTO_REGIME_DAILY,` as the final entry inside the
`ALL_SCHEMAS = [...]` list.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Document the table in DATABASE_SCHEMA.md**

Add a new subsection at the end of the Crypto ML group (after
`### crypto_signal_exclusions` or whichever is currently last in the
group):

```markdown
### `crypto_regime_daily`

Daily BTC market regime classification — observability-only signal for
the 30-day validation window. No consumer in trading / predict paths.

PK: `trade_date`. Columns: `regime` ('bull'|'neutral'|'bear'),
`confidence` (0.0/0.33/0.67/1.0), `indicators_json` (JSON: indicator
values + which rules fired + which rule-set was evaluated),
`computed_at` (audit, not data).

**Writer:** `monitoring/btc_regime.py` (called by
`main.py monitor btc-regime`, scheduled by
`mhde-monitor-btc-regime.timer` daily at 00:30 UTC).
**Reader:** `dashboard/services/queries.py:load_latest_btc_regime`,
`load_btc_regime_history`.
```

- [ ] **Step 6: Commit**

```bash
git add crypto/schema.py tests/crypto/test_btc_regime.py DATABASE_SCHEMA.md
git commit -m "feat(crypto): add crypto_regime_daily table"
```

---

## Task 2: Add `RegimeIndicators` and `RegimeResult` dataclasses

**Files:**
- Create: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Dataclasses are the data contract for the rest of the module.
Defining them first lets every subsequent test import them.

- [ ] **Step 1: Write the failing test**

Append to `tests/crypto/test_btc_regime.py`:

```python
from datetime import date

from monitoring.btc_regime import RegimeIndicators, RegimeResult


def test_regime_indicators_dataclass_fields():
    ind = RegimeIndicators(
        close=100.0,
        ma_50=98.0,
        ma_200=90.0,
        drawdown_from_ath=-0.05,
        realized_vol_30d=0.4,
        slope_30d=0.2,
    )
    assert ind.close == 100.0
    assert ind.ma_50 == 98.0
    assert ind.drawdown_from_ath == -0.05


def test_regime_result_dataclass_fields():
    ind = RegimeIndicators(
        close=100.0, ma_50=98.0, ma_200=90.0,
        drawdown_from_ath=-0.05,
        realized_vol_30d=0.4, slope_30d=0.2,
    )
    res = RegimeResult(
        as_of=date(2026, 5, 15),
        regime="bull",
        confidence=1.0,
        rules_fired={
            "price_above_ma200": True,
            "ma50_above_ma200": True,
            "drawdown_shallow": True,
        },
        rules_evaluated_against="bull",
        indicators=ind,
    )
    assert res.regime == "bull"
    assert res.confidence == 1.0
    assert res.rules_evaluated_against == "bull"
    assert res.indicators.close == 100.0


def test_regime_indicators_accepts_none_for_optional_fields():
    ind = RegimeIndicators(
        close=100.0,
        ma_50=None, ma_200=None,
        drawdown_from_ath=0.0,
        realized_vol_30d=None, slope_30d=None,
    )
    assert ind.ma_50 is None
    assert ind.realized_vol_30d is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: 3 new tests FAIL with `ModuleNotFoundError: monitoring.btc_regime`.

- [ ] **Step 3: Create the module with dataclasses**

Create `monitoring/btc_regime.py`:

```python
"""BTC market regime classifier — observability only, no trading impact.

Rules-based v1 (interpretable, no ML). Reads BTCUSDT from
crypto_prices_daily, computes five indicators (50d/200d MA,
drawdown-from-ATH, 30d realized vol, 30d log-price slope), applies
three rules to label the day bull / neutral / bear, persists to
crypto_regime_daily.

Spec: docs/superpowers/specs/2026-05-15-btc-regime-classifier-design.md
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from typing import Literal, Optional

import duckdb

logger = logging.getLogger("mhde.monitoring.btc_regime")

Regime = Literal["bull", "neutral", "bear"]

# Rule thresholds (per spec).
BULL_DRAWDOWN_FLOOR = -0.15   # bull rule: drawdown > -0.15 (within 15% of ATH)
BEAR_DRAWDOWN_CEILING = -0.25  # bear rule: drawdown < -0.25 (more than 25% below ATH)


@dataclass(frozen=True)
class RegimeIndicators:
    """Raw indicator values for one day."""
    close: float
    ma_50: Optional[float]
    ma_200: Optional[float]
    drawdown_from_ath: float
    realized_vol_30d: Optional[float]
    slope_30d: Optional[float]


@dataclass(frozen=True)
class RegimeResult:
    """One day's classification with full audit trail."""
    as_of: date
    regime: Regime
    confidence: float
    rules_fired: dict[str, bool]
    rules_evaluated_against: Regime
    indicators: RegimeIndicators
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): scaffold btc_regime module with dataclasses"
```

---

## Task 3: Implement `compute_indicators()` — MA50 and MA200

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Moving averages are independent indicators. Test on constant
series (MA == close) and a linear ramp (MA == mean of last N), and
verify None when there is insufficient history. Implement once for both
windows.

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
from monitoring.btc_regime import compute_indicators


def _series(start: date, closes: list[float]) -> list[tuple[date, float]]:
    from datetime import timedelta
    return [(start + timedelta(days=i), c) for i, c in enumerate(closes)]


def test_ma50_on_constant_series_equals_constant():
    prices = _series(date(2026, 1, 1), [100.0] * 60)
    ind = compute_indicators(prices)
    assert ind.ma_50 == 100.0


def test_ma200_on_known_series_is_mean_of_last_200():
    # Linear ramp 1..400. ma_200 over the last 200 days = mean of 201..400 = 300.5
    prices = _series(date(2025, 1, 1), [float(i) for i in range(1, 401)])
    ind = compute_indicators(prices)
    assert ind.ma_200 == 300.5


def test_ma50_returns_none_under_50_days():
    prices = _series(date(2026, 1, 1), [100.0] * 49)
    ind = compute_indicators(prices)
    assert ind.ma_50 is None


def test_ma200_returns_none_under_200_days():
    prices = _series(date(2026, 1, 1), [100.0] * 199)
    ind = compute_indicators(prices)
    assert ind.ma_200 is None


def test_compute_indicators_close_is_last_price():
    prices = _series(date(2026, 1, 1), [1.0, 2.0, 3.0])
    ind = compute_indicators(prices)
    assert ind.close == 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: 5 new tests FAIL — `compute_indicators` not defined.

- [ ] **Step 3: Implement compute_indicators (MA + close only for now)**

Append to `monitoring/btc_regime.py`:

```python
def compute_indicators(
    prices: list[tuple[date, float]],
) -> RegimeIndicators:
    """Pure function. Input: ascending list of (date, close).
       Output: indicators evaluated at the last day in the series.

    Returns None for any indicator that requires more history than
    provided. The caller is responsible for interpreting None (typically
    "insufficient lookback — classify as neutral, confidence 0.0").
    """
    if not prices:
        raise ValueError("compute_indicators requires at least 1 price")
    closes = [c for _, c in prices]
    last_close = closes[-1]

    ma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    ma_200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None

    # Placeholders — implemented in later tasks. Drawdown defaults to 0.0
    # (i.e., "at ATH") so it's a stable value during the build-up of the
    # other indicator implementations.
    drawdown_from_ath = 0.0
    realized_vol_30d: Optional[float] = None
    slope_30d: Optional[float] = None

    return RegimeIndicators(
        close=last_close,
        ma_50=ma_50,
        ma_200=ma_200,
        drawdown_from_ath=drawdown_from_ath,
        realized_vol_30d=realized_vol_30d,
        slope_30d=slope_30d,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all tests PASS (9 total at this point).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): compute MA50/MA200 in btc_regime indicators"
```

---

## Task 4: `compute_indicators()` — drawdown from all-time high

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Drawdown is one of the three regime rules. Signed,
in `[-1.0, 0.0]`. At ATH = 0.0; below ATH = negative. ATH is the maximum
close seen anywhere in the input series (no rolling window — full
history is the design choice per spec).

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
def test_drawdown_at_ath_is_zero():
    # Strictly increasing → last close IS the ATH.
    prices = _series(date(2026, 1, 1), [100.0 + i for i in range(50)])
    ind = compute_indicators(prices)
    assert ind.drawdown_from_ath == 0.0


def test_drawdown_below_ath_is_negative():
    # Peak at 100, then drop 20% to 80.
    closes = [100.0] * 30 + [80.0]
    prices = _series(date(2026, 1, 1), closes)
    ind = compute_indicators(prices)
    assert ind.drawdown_from_ath == -0.20


def test_drawdown_ranges_correctly_for_half_off():
    closes = [100.0] + [50.0]
    prices = _series(date(2026, 1, 1), closes)
    ind = compute_indicators(prices)
    assert ind.drawdown_from_ath == -0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: the three new drawdown tests FAIL (current placeholder returns 0.0
for the 20%-drop case too).

- [ ] **Step 3: Replace placeholder with real drawdown**

In `monitoring/btc_regime.py`, inside `compute_indicators`, replace
`drawdown_from_ath = 0.0` with:

```python
    ath = max(closes)
    drawdown_from_ath = (last_close - ath) / ath if ath > 0 else 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all tests PASS (12 total).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): compute drawdown-from-ATH indicator"
```

---

## Task 5: `compute_indicators()` — 30d realized volatility

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Realized vol is informational (not in the rules) but persisted
for dashboard surfacing. Computed as annualized standard deviation of
daily log returns over the last 30 days. Requires ≥31 prices (30 log
returns).

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
def test_realized_vol_zero_on_constant_prices():
    prices = _series(date(2026, 1, 1), [100.0] * 35)
    ind = compute_indicators(prices)
    assert ind.realized_vol_30d == 0.0


def test_realized_vol_positive_on_oscillating_prices():
    # Daily +/- 1% oscillation → non-zero vol.
    closes = []
    p = 100.0
    for i in range(40):
        p *= 1.01 if i % 2 == 0 else 0.99
        closes.append(p)
    prices = _series(date(2026, 1, 1), closes)
    ind = compute_indicators(prices)
    assert ind.realized_vol_30d is not None
    assert ind.realized_vol_30d > 0.0


def test_realized_vol_returns_none_under_31_prices():
    prices = _series(date(2026, 1, 1), [100.0] * 30)
    ind = compute_indicators(prices)
    assert ind.realized_vol_30d is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: the three new vol tests FAIL (placeholder returns None always,
so `assert ind.realized_vol_30d == 0.0` fails).

- [ ] **Step 3: Implement realized vol**

In `monitoring/btc_regime.py`, inside `compute_indicators`, replace the
`realized_vol_30d: Optional[float] = None` line with:

```python
    realized_vol_30d: Optional[float] = None
    if len(closes) >= 31:
        log_returns = [
            math.log(closes[-i] / closes[-i - 1])
            for i in range(1, 31)
        ]
        mean_lr = sum(log_returns) / len(log_returns)
        variance = sum((lr - mean_lr) ** 2 for lr in log_returns) / len(log_returns)
        realized_vol_30d = math.sqrt(variance) * math.sqrt(365)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all tests PASS (15 total).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): compute 30d realized vol indicator"
```

---

## Task 6: `compute_indicators()` — 30d log-price slope (annualized)

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Slope is informational, not in the rules. Linear regression of
`log(close)` against day index over the last 30 days; result multiplied
by 365 for an annualized drift figure (a value of `0.50` reads as
"+50%/year drift if the recent regime persists").

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
def test_slope_zero_on_flat_prices():
    prices = _series(date(2026, 1, 1), [100.0] * 30)
    ind = compute_indicators(prices)
    assert ind.slope_30d == 0.0


def test_slope_positive_on_uptrend():
    # Exponential uptrend exp(0.001 * t) → annualized slope ≈ 0.001 * 365 = 0.365
    closes = [math.exp(0.001 * i) for i in range(30)]
    prices = _series(date(2026, 1, 1), closes)
    ind = compute_indicators(prices)
    assert ind.slope_30d is not None
    assert abs(ind.slope_30d - 0.365) < 0.01


def test_slope_negative_on_downtrend():
    closes = [math.exp(-0.001 * i) for i in range(30)]
    prices = _series(date(2026, 1, 1), closes)
    ind = compute_indicators(prices)
    assert ind.slope_30d is not None
    assert ind.slope_30d < 0.0


def test_slope_returns_none_under_30_prices():
    prices = _series(date(2026, 1, 1), [100.0] * 29)
    ind = compute_indicators(prices)
    assert ind.slope_30d is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: the four new slope tests FAIL.

- [ ] **Step 3: Implement slope**

In `monitoring/btc_regime.py`, inside `compute_indicators`, replace
`slope_30d: Optional[float] = None` with:

```python
    slope_30d: Optional[float] = None
    if len(closes) >= 30:
        window = closes[-30:]
        log_window = [math.log(c) for c in window]
        n = len(log_window)
        x_mean = (n - 1) / 2.0          # mean of [0, 1, ..., n-1]
        y_mean = sum(log_window) / n
        num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(log_window))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope_per_day = num / den if den > 0 else 0.0
        slope_30d = slope_per_day * 365
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all tests PASS (19 total).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): compute 30d log-price slope indicator"
```

---

## Task 7: `classify()` — full bull, full bear, zero-rules neutral

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** The simplest classifier cases first. Indicators are constructed
directly (not via `compute_indicators`) so the test focuses purely on
the rule logic.

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
from monitoring.btc_regime import classify


def _ind(close, ma_50, ma_200, dd, vol=0.5, slope=0.0):
    return RegimeIndicators(
        close=close, ma_50=ma_50, ma_200=ma_200,
        drawdown_from_ath=dd,
        realized_vol_30d=vol, slope_30d=slope,
    )


def test_classify_full_bull():
    ind = _ind(close=110.0, ma_50=105.0, ma_200=100.0, dd=-0.05)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.regime == "bull"
    assert res.confidence == 1.0
    assert res.rules_evaluated_against == "bull"
    assert res.rules_fired == {
        "price_above_ma200": True,
        "ma50_above_ma200": True,
        "drawdown_shallow": True,
    }


def test_classify_full_bear():
    ind = _ind(close=80.0, ma_50=85.0, ma_200=100.0, dd=-0.35)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.regime == "bear"
    assert res.confidence == 1.0
    assert res.rules_evaluated_against == "bear"
    assert res.rules_fired == {
        "price_below_ma200": True,
        "ma50_below_ma200": True,
        "drawdown_deep": True,
    }


def test_classify_zero_rules_is_neutral_conf_zero():
    # price > ma200 but ma_50 < ma_200, drawdown moderate → 1 bull + 0 bear rules?
    # We want truly 0-0. Use exactly-on-boundary so no strict rule fires:
    ind = _ind(close=100.0, ma_50=100.0, ma_200=100.0, dd=-0.20)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.regime == "neutral"
    assert res.confidence == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: 3 new tests FAIL — `classify` not defined.

- [ ] **Step 3: Implement classify (core logic)**

Append to `monitoring/btc_regime.py`:

```python
_BULL_RULE_KEYS = ("price_above_ma200", "ma50_above_ma200", "drawdown_shallow")
_BEAR_RULE_KEYS = ("price_below_ma200", "ma50_below_ma200", "drawdown_deep")


def _bull_rules(ind: RegimeIndicators) -> dict[str, bool]:
    if ind.ma_200 is None or ind.ma_50 is None:
        return {k: False for k in _BULL_RULE_KEYS}
    return {
        "price_above_ma200": ind.close > ind.ma_200,
        "ma50_above_ma200": ind.ma_50 > ind.ma_200,
        "drawdown_shallow": ind.drawdown_from_ath > BULL_DRAWDOWN_FLOOR,
    }


def _bear_rules(ind: RegimeIndicators) -> dict[str, bool]:
    if ind.ma_200 is None or ind.ma_50 is None:
        return {k: False for k in _BEAR_RULE_KEYS}
    return {
        "price_below_ma200": ind.close < ind.ma_200,
        "ma50_below_ma200": ind.ma_50 < ind.ma_200,
        "drawdown_deep": ind.drawdown_from_ath < BEAR_DRAWDOWN_CEILING,
    }


def classify(ind: RegimeIndicators, *, as_of: date) -> RegimeResult:
    """Apply the rule set; return regime + confidence + which rules fired."""
    bull = _bull_rules(ind)
    bear = _bear_rules(ind)
    bull_met = sum(bull.values())
    bear_met = sum(bear.values())

    if bull_met == 3:
        regime: Regime = "bull"
        evaluated: Regime = "bull"
        confidence = 1.0
        rules_fired = bull
    elif bear_met == 3:
        regime = "bear"
        evaluated = "bear"
        confidence = 1.0
        rules_fired = bear
    else:
        regime = "neutral"
        # Tie-breaker: default to bull. Reported in rules_evaluated_against.
        if bear_met > bull_met:
            evaluated = "bear"
            confidence = bear_met / 3
            rules_fired = bear
        else:
            evaluated = "bull"
            confidence = bull_met / 3
            rules_fired = bull

    return RegimeResult(
        as_of=as_of,
        regime=regime,
        confidence=confidence,
        rules_fired=rules_fired,
        rules_evaluated_against=evaluated,
        indicators=ind,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all tests PASS (22 total).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): implement btc_regime classify() core logic"
```

---

## Task 8: `classify()` — partial-fire neutral and tie-breaker

**Files:**
- Modify: `monitoring/btc_regime.py` (no change expected — logic already
  covers these; this task is regression-coverage)
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Confirm 2/3 partials and the 1-1 tie behave as specified.
If any test fails, the bug is in Task 7's implementation — fix there.

- [ ] **Step 1: Write the tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
def test_classify_two_of_three_bull_rules():
    # price > ma200 ✓, ma50 > ma200 ✓, drawdown -0.18 (below -0.15 → bull fails)
    ind = _ind(close=110.0, ma_50=105.0, ma_200=100.0, dd=-0.18)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.regime == "neutral"
    assert abs(res.confidence - 2 / 3) < 1e-9
    assert res.rules_evaluated_against == "bull"
    assert res.rules_fired == {
        "price_above_ma200": True,
        "ma50_above_ma200": True,
        "drawdown_shallow": False,
    }


def test_classify_two_of_three_bear_rules():
    # price < ma200 ✓, ma50 < ma200 ✓, drawdown -0.20 (above -0.25 → bear fails)
    ind = _ind(close=90.0, ma_50=95.0, ma_200=100.0, dd=-0.20)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.regime == "neutral"
    assert abs(res.confidence - 2 / 3) < 1e-9
    assert res.rules_evaluated_against == "bear"
    assert res.rules_fired == {
        "price_below_ma200": True,
        "ma50_below_ma200": True,
        "drawdown_deep": False,
    }


def test_classify_tie_one_bull_one_bear_defaults_to_bull():
    # price > ma200 (bull) + ma50 < ma200 (bear) + dd -0.20 (neither) → 1-1 tie.
    ind = _ind(close=110.0, ma_50=95.0, ma_200=100.0, dd=-0.20)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.regime == "neutral"
    assert abs(res.confidence - 1 / 3) < 1e-9
    assert res.rules_evaluated_against == "bull"
```

- [ ] **Step 2: Run tests**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all PASS (25 total) — Task 7's logic already handles these.

- [ ] **Step 3: Commit**

```bash
git add tests/crypto/test_btc_regime.py
git commit -m "test(monitoring): cover btc_regime partial-fire and tie-breaker cases"
```

---

## Task 9: `classify()` — boundary conditions (strict operators)

**Files:**
- Modify: `monitoring/btc_regime.py` (no change expected; regression)
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** The spec uses strict `>` and `<` everywhere. Exact-threshold
values must NOT fire the rule. These tests pin that behavior in case a
future change accidentally relaxes the operator.

- [ ] **Step 1: Write the tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
def test_classify_price_equal_to_ma200_does_not_fire_bull_rule():
    ind = _ind(close=100.0, ma_50=105.0, ma_200=100.0, dd=-0.05)
    res = classify(ind, as_of=date(2026, 5, 15))
    # Only ma50>ma200 and drawdown_shallow fire; price==ma200 does not.
    assert res.rules_fired["price_above_ma200"] is False
    assert res.regime == "neutral"


def test_classify_ma50_equal_to_ma200_does_not_fire_bull_rule():
    ind = _ind(close=110.0, ma_50=100.0, ma_200=100.0, dd=-0.05)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.rules_fired["ma50_above_ma200"] is False


def test_classify_drawdown_exactly_minus_15pct_does_not_fire_bull():
    ind = _ind(close=110.0, ma_50=105.0, ma_200=100.0, dd=-0.15)
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.rules_fired["drawdown_shallow"] is False


def test_classify_drawdown_exactly_minus_25pct_does_not_fire_bear():
    ind = _ind(close=90.0, ma_50=95.0, ma_200=100.0, dd=-0.25)
    res = classify(ind, as_of=date(2026, 5, 15))
    # bear rule "drawdown_deep" is dd < -0.25; exactly -0.25 is NOT deep.
    assert res.rules_fired["drawdown_deep"] is False
```

- [ ] **Step 2: Run tests**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all PASS (29 total).

- [ ] **Step 3: Commit**

```bash
git add tests/crypto/test_btc_regime.py
git commit -m "test(monitoring): pin strict-operator boundary behavior in btc_regime"
```

---

## Task 10: `classify()` — insufficient history is neutral with 0 confidence

**Files:**
- Modify: `monitoring/btc_regime.py` (no change; regression)
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** When `ma_200` is None (fewer than 200 prices), `_bull_rules` /
`_bear_rules` already return all-False, so `classify` should return
neutral / conf 0.0 / bull-evaluated (per the tie-breaker default at 0-0).

- [ ] **Step 1: Write the test**

Append to `tests/crypto/test_btc_regime.py`:

```python
def test_classify_insufficient_history_returns_neutral_conf_zero():
    ind = RegimeIndicators(
        close=100.0,
        ma_50=None, ma_200=None,
        drawdown_from_ath=-0.05,
        realized_vol_30d=None, slope_30d=None,
    )
    res = classify(ind, as_of=date(2026, 5, 15))
    assert res.regime == "neutral"
    assert res.confidence == 0.0
    assert res.rules_evaluated_against == "bull"
```

- [ ] **Step 2: Run tests**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all PASS (30 total).

- [ ] **Step 3: Commit**

```bash
git add tests/crypto/test_btc_regime.py
git commit -m "test(monitoring): pin neutral-on-insufficient-history behavior"
```

---

## Task 11: `compute_history()` — iterate classify() across a price series

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Backfill needs to label every historical day. Days before day
200 get `regime='neutral'` / `confidence=0.0` because `ma_200` is None.
Idempotent: running twice on the same prices produces identical output.

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
from monitoring.btc_regime import compute_history


def test_compute_history_returns_one_result_per_day():
    prices = _series(date(2025, 1, 1), [100.0 + i * 0.01 for i in range(250)])
    results = compute_history(prices)
    assert len(results) == 250


def test_compute_history_first_199_days_are_neutral_zero_conf():
    prices = _series(date(2025, 1, 1), [100.0 + i * 0.01 for i in range(250)])
    results = compute_history(prices)
    for r in results[:199]:
        assert r.regime == "neutral"
        assert r.confidence == 0.0
        assert r.indicators.ma_200 is None


def test_compute_history_is_idempotent():
    prices = _series(date(2025, 1, 1), [100.0 + i * 0.01 for i in range(250)])
    r1 = compute_history(prices)
    r2 = compute_history(prices)
    assert r1 == r2


def test_compute_history_each_result_dated_correctly():
    from datetime import timedelta
    start = date(2025, 1, 1)
    prices = _series(start, [100.0 + i * 0.01 for i in range(50)])
    results = compute_history(prices)
    for i, r in enumerate(results):
        assert r.as_of == start + timedelta(days=i)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: 4 new tests FAIL — `compute_history` not defined.

- [ ] **Step 3: Implement compute_history**

Append to `monitoring/btc_regime.py`:

```python
def compute_history(
    prices: list[tuple[date, float]],
) -> list[RegimeResult]:
    """Run classify() at each day. The result for day i is computed from
    prices[:i+1] — i.e., only the prices available up to and including
    that day. No look-ahead.
    """
    out: list[RegimeResult] = []
    for i in range(len(prices)):
        window = prices[: i + 1]
        ind = compute_indicators(window)
        as_of = prices[i][0]
        out.append(classify(ind, as_of=as_of))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all tests PASS (34 total).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): implement compute_history for btc_regime backfill"
```

---

## Task 12: Transition behavior tests (no-smoothing v1)

**Files:**
- Modify: `monitoring/btc_regime.py` (no change expected)
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** V1 explicitly does not smooth single-day regime flickers. These
tests document and lock that decision: a single bearish day in an
otherwise bullish series produces a 'bear' label on that day.

- [ ] **Step 1: Write the tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
def test_consecutive_bear_days_all_marked_bear():
    # Construct: long bull run, then sharp 30% drop persists for 5 days,
    # such that every one of those 5 days satisfies all 3 bear rules.
    closes = [100.0 + i * 0.5 for i in range(220)]   # bull build-up
    peak = closes[-1]
    drop = peak * 0.65                                # ~35% drawdown
    closes.extend([drop] * 5)
    prices = _series(date(2024, 1, 1), closes)
    results = compute_history(prices)
    last_five = results[-5:]
    assert all(r.regime == "bear" for r in last_five), (
        [r.regime for r in last_five]
    )


def test_single_day_flicker_is_not_smoothed():
    # Bull run, single anomalous day with deep drawdown, then bull resumes.
    # The single anomalous day classifies on its own — no smoothing.
    closes = [100.0 + i * 0.5 for i in range(220)]
    peak = closes[-1]
    closes.append(peak * 0.6)            # one-day -40% (≈ -40% drawdown)
    closes.append(peak * 1.01)            # resume above peak
    prices = _series(date(2024, 1, 1), closes)
    results = compute_history(prices)
    # The third-to-last result is the deep-drop day.
    assert results[-2].regime == "bear"
    # The next day is back into bull (drawdown is now ~0 vs new ATH).
    assert results[-1].regime == "bull"
```

- [ ] **Step 2: Run tests**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all PASS (36 total).

- [ ] **Step 3: Commit**

```bash
git add tests/crypto/test_btc_regime.py
git commit -m "test(monitoring): document no-smoothing transition behavior"
```

---

## Task 13: `persist_today()` — insert + UPSERT into `crypto_regime_daily`

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Daily timer needs a write path. DuckDB UPSERT via `INSERT ... ON
CONFLICT (trade_date) DO UPDATE SET ...`. `indicators_json` is a JSON
serialization of indicators + rules_fired + rules_evaluated_against.

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
from monitoring.btc_regime import persist_today


def _conn():
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    return conn


def _sample_result(d: date = date(2026, 5, 15), regime: Regime = "bull") -> RegimeResult:
    ind = RegimeIndicators(
        close=81048.9, ma_50=78550.21, ma_200=71204.15,
        drawdown_from_ath=-0.05,
        realized_vol_30d=0.612, slope_30d=0.18,
    )
    return RegimeResult(
        as_of=d, regime=regime, confidence=1.0,
        rules_fired={"price_above_ma200": True, "ma50_above_ma200": True, "drawdown_shallow": True},
        rules_evaluated_against="bull",
        indicators=ind,
    )


# `Regime` type symbol imported for the helper above.
from monitoring.btc_regime import Regime  # noqa: E402


def test_persist_today_inserts_new_row():
    conn = _conn()
    persist_today(conn, _sample_result())
    rows = conn.execute(
        "SELECT trade_date, regime, confidence FROM crypto_regime_daily"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == date(2026, 5, 15)
    assert rows[0][1] == "bull"
    assert rows[0][2] == 1.0


def test_persist_today_upserts_existing_row():
    conn = _conn()
    persist_today(conn, _sample_result(regime="bull"))
    persist_today(conn, _sample_result(regime="bear"))  # same date, new label
    rows = conn.execute(
        "SELECT trade_date, regime FROM crypto_regime_daily"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "bear"


def test_persist_today_indicators_json_roundtrip():
    conn = _conn()
    persist_today(conn, _sample_result())
    raw = conn.execute(
        "SELECT indicators_json FROM crypto_regime_daily"
    ).fetchone()[0]
    payload = json.loads(raw)
    assert payload["close"] == 81048.9
    assert payload["rules_evaluated_against"] == "bull"
    assert payload["rules_fired"]["price_above_ma200"] is True
    assert payload["realized_vol_30d"] == 0.612
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: 3 new tests FAIL — `persist_today` not defined.

- [ ] **Step 3: Implement persist_today**

Append to `monitoring/btc_regime.py`:

```python
def _result_to_json(result: RegimeResult) -> str:
    payload = {
        "close": result.indicators.close,
        "ma_50": result.indicators.ma_50,
        "ma_200": result.indicators.ma_200,
        "drawdown_from_ath": result.indicators.drawdown_from_ath,
        "realized_vol_30d": result.indicators.realized_vol_30d,
        "slope_30d": result.indicators.slope_30d,
        "rules_fired": result.rules_fired,
        "rules_evaluated_against": result.rules_evaluated_against,
    }
    return json.dumps(payload)


def persist_today(
    conn: duckdb.DuckDBPyConnection,
    result: RegimeResult,
) -> None:
    """UPSERT one row into crypto_regime_daily."""
    conn.execute(
        """
        INSERT INTO crypto_regime_daily
            (trade_date, regime, confidence, indicators_json, computed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (trade_date) DO UPDATE SET
            regime = EXCLUDED.regime,
            confidence = EXCLUDED.confidence,
            indicators_json = EXCLUDED.indicators_json,
            computed_at = EXCLUDED.computed_at
        """,
        [
            result.as_of,
            result.regime,
            result.confidence,
            _result_to_json(result),
            datetime.now(timezone.utc),
        ],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all PASS (39 total).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): persist btc_regime results into crypto_regime_daily"
```

---

## Task 14: `run_daily()` + `main()` end-to-end

**Files:**
- Modify: `monitoring/btc_regime.py`
- Test: `tests/crypto/test_btc_regime.py` (append)

**Why:** Wire `compute_indicators` + `classify` + `persist_today` into a
single entry point that takes a DB path, reads BTCUSDT from
`crypto_prices_daily`, classifies the latest day, and writes the row.

- [ ] **Step 1: Write the failing tests**

Append to `tests/crypto/test_btc_regime.py`:

```python
from monitoring.btc_regime import run_daily


def test_run_daily_writes_one_row_for_latest_date(tmp_path):
    db_path = tmp_path / "test_mhde.duckdb"
    conn = duckdb.connect(str(db_path))
    create_all_tables(conn)
    # Seed BTCUSDT in crypto_prices_daily with 220 days of bull-shaped data.
    from datetime import timedelta
    start = date(2024, 1, 1)
    for i in range(220):
        d = start + timedelta(days=i)
        close = 50000.0 + i * 100.0
        conn.execute(
            "INSERT INTO crypto_prices_daily "
            "(symbol, trade_date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["BTCUSDT", d, close, close, close, close, 0.0],
        )
    conn.close()

    result = run_daily(str(db_path))
    assert result.as_of == start + timedelta(days=219)
    assert result.regime == "bull"

    conn = duckdb.connect(str(db_path))
    rows = conn.execute(
        "SELECT trade_date, regime FROM crypto_regime_daily"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "bull"


def test_run_daily_raises_when_no_btc_prices(tmp_path):
    import pytest

    db_path = tmp_path / "empty.duckdb"
    conn = duckdb.connect(str(db_path))
    create_all_tables(conn)
    conn.close()

    with pytest.raises(ValueError, match="No BTCUSDT prices"):
        run_daily(str(db_path))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: 2 new tests FAIL — `run_daily` not defined.

- [ ] **Step 3: Implement run_daily and main**

Append to `monitoring/btc_regime.py`:

```python
def _load_btc_prices(conn: duckdb.DuckDBPyConnection) -> list[tuple[date, float]]:
    rows = conn.execute(
        "SELECT trade_date, close FROM crypto_prices_daily "
        "WHERE symbol = 'BTCUSDT' "
        "ORDER BY trade_date ASC"
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def run_daily(db_path: str) -> RegimeResult:
    """Load BTCUSDT, compute regime for the latest date, persist.
    Returns the RegimeResult that was written."""
    conn = duckdb.connect(db_path)
    try:
        prices = _load_btc_prices(conn)
        if not prices:
            raise ValueError(
                "No BTCUSDT prices in crypto_prices_daily — "
                "run crypto backfill-prices first."
            )
        ind = compute_indicators(prices)
        result = classify(ind, as_of=prices[-1][0])
        persist_today(conn, result)
        return result
    finally:
        conn.close()


def main() -> int:
    """CLI entry point for `main.py monitor btc-regime`."""
    db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
    result = run_daily(db_path)
    logger.info(
        "BTC regime %s: %s (confidence %.2f, evaluated_against=%s, rules=%s)",
        result.as_of, result.regime, result.confidence,
        result.rules_evaluated_against, result.rules_fired,
    )
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/crypto/test_btc_regime.py -v`
Expected: all PASS (41 total).

- [ ] **Step 5: Commit**

```bash
git add monitoring/btc_regime.py tests/crypto/test_btc_regime.py
git commit -m "feat(monitoring): wire btc_regime run_daily() + main() entry points"
```

---

## Task 15: Register `monitor btc-regime` subcommand in `main.py`

**Files:**
- Modify: `main.py` (add a `@monitor.command("btc-regime")` block after the
  existing monitor commands around line 2580)

**Why:** Makes the systemd unit's `ExecStart` line work
(`venv/bin/python main.py monitor btc-regime`).

- [ ] **Step 1: Add the subcommand**

In `main.py`, find the block around line 2574-2580:

```python
@monitor.command("smoke")
def monitor_smoke():
    from monitoring import smoke_test
    raise SystemExit(smoke_test.main())


@monitor.command("streamlit-freshness")
def monitor_streamlit_freshness():
    from monitoring import streamlit_freshness
```

Immediately after the existing `@monitor.command("streamlit-freshness")`
block and its function body, add:

```python
@monitor.command("btc-regime")
def monitor_btc_regime():
    """BTC market regime classifier (observability only) — daily."""
    from monitoring import btc_regime
    raise SystemExit(btc_regime.main())
```

(If a different monitor command turns out to be the last one in the file,
add this new block immediately after that one — the order doesn't matter
for the CLI, only for readability.)

- [ ] **Step 2: Smoke-test the CLI registration**

Run: `venv/bin/python main.py monitor --help 2>&1 | grep btc-regime`
Expected: a line listing `btc-regime` in the available subcommands.

- [ ] **Step 3: Smoke-test against the real DB (read-only sanity)**

Run: `venv/bin/python main.py monitor btc-regime 2>&1 | tail -10`
Expected: a single log line of the form `BTC regime YYYY-MM-DD: ...`
and exit code 0. If `crypto_prices_daily` has BTCUSDT data (it does —
770 rows from 2024-04-05 to 2026-05-14), the call writes a row to
`crypto_regime_daily` for the latest trade date.

If exit is non-zero with "No BTCUSDT prices", that's an environment
issue (DB path), not a code bug — verify `MHDE_DB_PATH` env var.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat(cli): register monitor btc-regime subcommand"
```

---

## Task 16: systemd service + timer units

**Files:**
- Create: `systemd/mhde-monitor-btc-regime.service`
- Create: `systemd/mhde-monitor-btc-regime.timer`

**Why:** Daily 00:30 UTC execution. Pattern lifted verbatim from
`mhde-monitor-phase0-calibration.{service,timer}` with module-specific
fields swapped in.

- [ ] **Step 1: Create the service file**

Create `systemd/mhde-monitor-btc-regime.service` with content:

```ini
[Unit]
Description=MHDE monitor: BTC market regime classifier (daily)
After=network.target

[Service]
Type=oneshot
User=jpcg
WorkingDirectory=/home/jpcg/MHDE
Environment=MHDE_DB_PATH=/home/jpcg/MHDE/data/mhde.duckdb
ExecStart=/home/jpcg/MHDE/venv/bin/python main.py monitor btc-regime
StandardOutput=append:/home/jpcg/MHDE/data/logs/monitor_btc_regime.log
StandardError=append:/home/jpcg/MHDE/data/logs/monitor_btc_regime.log
```

- [ ] **Step 2: Create the timer file**

Create `systemd/mhde-monitor-btc-regime.timer` with content:

```ini
[Unit]
Description=MHDE monitor: BTC regime — daily 00:30 UTC

[Timer]
OnCalendar=*-*-* 00:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Verify the units parse**

Run: `systemd-analyze verify systemd/mhde-monitor-btc-regime.service systemd/mhde-monitor-btc-regime.timer 2>&1`
Expected: empty output (exit 0). Any warnings/errors must be fixed
before committing — `systemd-analyze verify` is the canonical gate.

- [ ] **Step 4: Commit**

```bash
git add systemd/mhde-monitor-btc-regime.service systemd/mhde-monitor-btc-regime.timer
git commit -m "feat(systemd): add mhde-monitor-btc-regime service + timer (00:30 UTC daily)"
```

---

## Task 17: Local backfill scripts (BTC prices to 2020, regime history)

**Files:**
- Create: `.claude/local_scripts/backfill_btc_2020.py`
- Create: `.claude/local_scripts/backfill_btc_regime_history.py`

**Why:** One-off operator scripts. `.claude/local_scripts/` is gitignored
by convention (these files exist on disk to be runnable but are NOT
committed to git history; the spec and plan document their existence
and behavior).

- [ ] **Step 1: Verify .claude/local_scripts/ is gitignored**

Run: `git check-ignore -v .claude/local_scripts/foo.py 2>&1`
Expected: a line showing which .gitignore rule applies. If nothing is
returned, add `.claude/local_scripts/` to `.gitignore` before continuing.

- [ ] **Step 2: Write backfill_btc_2020.py**

Create `.claude/local_scripts/backfill_btc_2020.py`:

```python
"""One-off: backfill BTCUSDT daily candles from 2020-01-01 to today
into crypto_prices_daily via the existing BinanceClient.

Idempotent — ON CONFLICT DO NOTHING ensures re-runs add nothing new.

Usage:
    venv/bin/python .claude/local_scripts/backfill_btc_2020.py
"""
from __future__ import annotations

import os
import sys
from datetime import date

import duckdb

sys.path.insert(0, "/home/jpcg/MHDE")

from crypto.ingestion.backfill_ohlcv import backfill_symbol  # type: ignore
from crypto.schema import create_all_tables  # type: ignore


def main() -> int:
    db_path = os.environ.get("MHDE_DB_PATH", "/home/jpcg/MHDE/data/mhde.duckdb")
    conn = duckdb.connect(db_path)
    try:
        create_all_tables(conn)
        # backfill_symbol API: (conn, symbol, start_date, end_date)
        # Confirm exact signature by reading crypto/ingestion/backfill_ohlcv.py
        # before running. If it takes a list, wrap.
        backfill_symbol(conn, "BTCUSDT", start=date(2020, 1, 1))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM crypto_prices_daily WHERE symbol = 'BTCUSDT'"
        ).fetchone()[0]
        print(f"BTCUSDT rows in crypto_prices_daily: {cnt}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

Implementation note: read `crypto/ingestion/backfill_ohlcv.py` to confirm
the exact public function name and signature; this script is a thin
wrapper around whatever existing entry point performs the backfill for a
single symbol from a given start date.

- [ ] **Step 3: Write backfill_btc_regime_history.py**

Create `.claude/local_scripts/backfill_btc_regime_history.py`:

```python
"""One-off: read BTCUSDT history from crypto_prices_daily, label every
day via monitoring.btc_regime, write:

    * data/processed/btc_regime_history.parquet  (overwrite)
    * UPSERT every row into crypto_regime_daily
    * data/processed/btc_regime_validation.md    (overwrite)

Usage:
    venv/bin/python .claude/local_scripts/backfill_btc_regime_history.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date

import duckdb
import pandas as pd

sys.path.insert(0, "/home/jpcg/MHDE")

from monitoring.btc_regime import (
    _load_btc_prices, classify, compute_indicators, compute_history, persist_today,
)


PARQUET_PATH = "/home/jpcg/MHDE/data/processed/btc_regime_history.parquet"
VALIDATION_PATH = "/home/jpcg/MHDE/data/processed/btc_regime_validation.md"


KNOWN_PERIODS = [
    ("2021 bull peak", date(2021, 11, 1), date(2021, 11, 30)),
    ("2022 bear bottom", date(2022, 11, 1), date(2022, 12, 31)),
    ("2023 recovery start", date(2023, 1, 1), date(2023, 3, 31)),
    ("2024 chop", date(2024, 4, 1), date(2024, 9, 30)),
    ("2025 bull", date(2025, 1, 1), date(2025, 6, 30)),
    ("current pullback", date(2026, 1, 1), date(2026, 5, 15)),
]


def _flatten_to_rows(results):
    rows = []
    for r in results:
        rows.append({
            "trade_date": r.as_of,
            "close": r.indicators.close,
            "ma_50": r.indicators.ma_50,
            "ma_200": r.indicators.ma_200,
            "drawdown_from_ath": r.indicators.drawdown_from_ath,
            "realized_vol_30d": r.indicators.realized_vol_30d,
            "slope_30d": r.indicators.slope_30d,
            "regime": r.regime,
            "confidence": r.confidence,
            "rule_price_above_ma200": r.rules_fired.get("price_above_ma200", r.rules_fired.get("price_below_ma200", False)),
            "rule_ma50_above_ma200": r.rules_fired.get("ma50_above_ma200", r.rules_fired.get("ma50_below_ma200", False)),
            "rule_drawdown_shallow": r.rules_fired.get("drawdown_shallow", r.rules_fired.get("drawdown_deep", False)),
            "rules_evaluated_against": r.rules_evaluated_against,
        })
    return rows


def _build_validation_md(df: pd.DataFrame) -> str:
    parts: list[str] = []
    parts.append("# BTC Regime Classifier — Validation Report\n")
    parts.append(f"Generated by `backfill_btc_regime_history.py` over the full "
                 f"BTCUSDT history in `crypto_prices_daily`.\n\n"
                 f"Total days: **{len(df)}** "
                 f"({df['trade_date'].min()} → {df['trade_date'].max()})\n")

    parts.append("## 1. Day counts per regime\n")
    counts = df["regime"].value_counts()
    parts.append("| Regime | Days | % |")
    parts.append("|---|---:|---:|")
    for regime in ("bull", "neutral", "bear"):
        n = int(counts.get(regime, 0))
        pct = 100.0 * n / len(df)
        parts.append(f"| {regime} | {n} | {pct:.1f}% |")
    parts.append("")

    parts.append("## 2. Transition matrix (yesterday → today)\n")
    transitions = Counter()
    prev = None
    for r in df["regime"].tolist():
        if prev is not None:
            transitions[(prev, r)] += 1
        prev = r
    parts.append("| From \\ To | bull | neutral | bear |")
    parts.append("|---|---:|---:|---:|")
    for src in ("bull", "neutral", "bear"):
        cells = " | ".join(str(transitions.get((src, dst), 0))
                            for dst in ("bull", "neutral", "bear"))
        parts.append(f"| {src} | {cells} |")
    parts.append("")

    parts.append("## 3. Spot-check sections\n")
    for name, lo, hi in KNOWN_PERIODS:
        sub = df[(df["trade_date"] >= lo) & (df["trade_date"] <= hi)]
        if sub.empty:
            parts.append(f"### {name} ({lo} → {hi})\n_No data in this range._\n")
            continue
        cnt = sub["regime"].value_counts()
        parts.append(f"### {name} ({lo} → {hi})\n")
        parts.append("| Regime | Days |")
        parts.append("|---|---:|")
        for r in ("bull", "neutral", "bear"):
            parts.append(f"| {r} | {int(cnt.get(r, 0))} |")
        parts.append("")

    parts.append("## 4. Flagged boundary cases\n")
    # Single-day flickers: regime differs from both neighbours.
    regimes = df["regime"].tolist()
    dates = df["trade_date"].tolist()
    flickers: list[tuple[date, str, str]] = []
    for i in range(1, len(regimes) - 1):
        if regimes[i - 1] == regimes[i + 1] and regimes[i] != regimes[i - 1]:
            flickers.append((dates[i], regimes[i], regimes[i - 1]))
    if not flickers:
        parts.append("_No single-day flickers detected._\n")
    else:
        parts.append("| Date | Single-day | Surrounding |")
        parts.append("|---|---|---|")
        for d, mid, neighbour in flickers[:50]:
            parts.append(f"| {d} | {mid} | {neighbour} |")
        if len(flickers) > 50:
            parts.append(f"\n_…and {len(flickers) - 50} more._\n")
    parts.append("")

    return "\n".join(parts)


def main() -> int:
    db_path = os.environ.get("MHDE_DB_PATH", "/home/jpcg/MHDE/data/mhde.duckdb")
    conn = duckdb.connect(db_path)
    try:
        prices = _load_btc_prices(conn)
        if not prices:
            print("No BTCUSDT prices — run backfill_btc_2020.py first.")
            return 1
        results = compute_history(prices)

        # 1. Parquet
        rows = _flatten_to_rows(results)
        df = pd.DataFrame(rows)
        os.makedirs(os.path.dirname(PARQUET_PATH), exist_ok=True)
        df.to_parquet(PARQUET_PATH, index=False)
        print(f"Wrote {PARQUET_PATH}: {len(df)} rows")

        # 2. DB UPSERT
        for r in results:
            persist_today(conn, r)
        cnt = conn.execute("SELECT COUNT(*) FROM crypto_regime_daily").fetchone()[0]
        print(f"crypto_regime_daily rows: {cnt}")

        # 3. Validation MD
        with open(VALIDATION_PATH, "w") as f:
            f.write(_build_validation_md(df))
        print(f"Wrote {VALIDATION_PATH}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Smoke-test the scripts compile (don't run yet)**

Run: `venv/bin/python -c "import ast; ast.parse(open('.claude/local_scripts/backfill_btc_2020.py').read()); ast.parse(open('.claude/local_scripts/backfill_btc_regime_history.py').read()); print('OK')"`
Expected: `OK`.

(The scripts are NOT committed because the path is gitignored; this task
just verifies they exist on disk and parse. They run in Task 22.)

- [ ] **Step 5: No commit for this task**

`.claude/local_scripts/` is gitignored. There is nothing to commit.

---

## Task 18: Dashboard queries — `load_latest_btc_regime` + `load_btc_regime_history`

**Files:**
- Modify: `dashboard/services/queries.py` (append two new functions)
- Create: `tests/dashboard/test_regime_queries.py`

**Why:** Pure read-only functions that decouple the Streamlit rendering
code from the SQL. Cached with `@st.cache_data(ttl=300)` per the
convention used elsewhere in this file. Tests use the in-memory DuckDB
pattern from `tests/dashboard/test_paper_trading_queries.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/dashboard/test_regime_queries.py`:

```python
"""Unit tests for dashboard.services.queries — BTC regime queries."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import duckdb
import pandas as pd
import pytest

from crypto.schema import create_all_tables
from dashboard.services import queries as q


def _conn_with_regime_data(rows: list[dict]) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    now = datetime.now(timezone.utc)
    for r in rows:
        conn.execute(
            "INSERT INTO crypto_regime_daily "
            "(trade_date, regime, confidence, indicators_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [r["trade_date"], r["regime"], r["confidence"],
             json.dumps(r["indicators_json"]), now],
        )
    return conn


def test_load_latest_btc_regime_returns_none_on_empty_table():
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    assert q.load_latest_btc_regime(conn) is None


def test_load_latest_btc_regime_returns_most_recent_row_parsed():
    conn = _conn_with_regime_data([
        {"trade_date": date(2026, 5, 13), "regime": "neutral", "confidence": 0.67,
         "indicators_json": {"close": 79288.3, "rules_evaluated_against": "bull"}},
        {"trade_date": date(2026, 5, 15), "regime": "bull", "confidence": 1.0,
         "indicators_json": {"close": 81048.9, "rules_evaluated_against": "bull"}},
    ])
    result = q.load_latest_btc_regime(conn)
    assert result is not None
    assert result["trade_date"] == date(2026, 5, 15)
    assert result["regime"] == "bull"
    assert result["confidence"] == 1.0
    # indicators_json must be parsed into a dict, not returned as a string.
    assert isinstance(result["indicators"], dict)
    assert result["indicators"]["close"] == 81048.9


def test_load_btc_regime_history_joins_with_prices():
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    now = datetime.now(timezone.utc)
    for i, d in enumerate([date(2026, 5, 13), date(2026, 5, 14), date(2026, 5, 15)]):
        conn.execute(
            "INSERT INTO crypto_prices_daily "
            "(symbol, trade_date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["BTCUSDT", d, 0.0, 0.0, 0.0, 80000.0 + i * 100, 0.0],
        )
        conn.execute(
            "INSERT INTO crypto_regime_daily "
            "(trade_date, regime, confidence, indicators_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [d, "bull", 1.0, "{}", now],
        )
    df = q.load_btc_regime_history(conn, days=90)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["trade_date", "close", "regime", "confidence"]
    assert len(df) == 3
    assert df.iloc[-1]["close"] == 80200.0
    assert (df["regime"] == "bull").all()


def test_load_btc_regime_history_respects_days_limit():
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    start = date(2026, 1, 1)
    for i in range(120):
        d = start + timedelta(days=i)
        conn.execute(
            "INSERT INTO crypto_prices_daily "
            "(symbol, trade_date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["BTCUSDT", d, 0.0, 0.0, 0.0, 50000.0, 0.0],
        )
        conn.execute(
            "INSERT INTO crypto_regime_daily "
            "(trade_date, regime, confidence, indicators_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [d, "bull", 1.0, "{}", now],
        )
    df = q.load_btc_regime_history(conn, days=30)
    assert len(df) == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/dashboard/test_regime_queries.py -v`
Expected: 4 tests FAIL — query functions not defined.

- [ ] **Step 3: Add the query functions**

Read `dashboard/services/queries.py` to confirm:
- whether `@st.cache_data` decorators are present (match the prevailing
  pattern — some pure-helper modules omit them when the conn parameter
  is unhashable);
- the existing module-level imports (so this task does not duplicate them).

Append to `dashboard/services/queries.py`:

```python
def load_latest_btc_regime(conn) -> dict | None:
    """Most recent crypto_regime_daily row; indicators_json parsed.

    Returns None if the table is empty (first-deploy state, before the
    one-off backfill has run).
    """
    import json as _json

    row = conn.execute(
        "SELECT trade_date, regime, confidence, indicators_json, computed_at "
        "FROM crypto_regime_daily "
        "ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "trade_date": row[0],
        "regime": row[1],
        "confidence": row[2],
        "indicators": _json.loads(row[3]),
        "computed_at": row[4],
    }


def load_btc_regime_history(conn, days: int = 90):
    """JOIN crypto_regime_daily ⨝ crypto_prices_daily(BTCUSDT) on
    trade_date; return the last `days` rows ordered ascending.
    Columns: trade_date, close, regime, confidence.
    """
    import pandas as _pd

    df = conn.execute(
        """
        SELECT r.trade_date, p.close, r.regime, r.confidence
        FROM crypto_regime_daily r
        JOIN crypto_prices_daily p
          ON p.trade_date = r.trade_date AND p.symbol = 'BTCUSDT'
        ORDER BY r.trade_date DESC
        LIMIT ?
        """,
        [days],
    ).fetch_df()
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df
```

(If `dashboard/services/queries.py` uses `pd` as a module-level alias, drop
the local import. Match the file's conventions.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/dashboard/test_regime_queries.py -v`
Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/services/queries.py tests/dashboard/test_regime_queries.py
git commit -m "feat(dashboard): add BTC regime query helpers"
```

---

## Task 19: Dashboard component — `regime_banner.py`

**Files:**
- Create: `dashboard/components/regime_banner.py`
- Create: `tests/dashboard/test_regime_banner.py`

**Why:** Render the banner + 90d shaded chart + reference note as three
small public functions. Tests assert that the function does NOT crash on
empty / stale / populated data — Streamlit render assertions are limited
to "the function runs and calls the right helpers".

- [ ] **Step 1: Write the failing tests**

Create `tests/dashboard/test_regime_banner.py`:

```python
"""Unit tests for dashboard.components.regime_banner.

These follow the existing dashboard test pattern: mock the Streamlit
module, call the render function, assert the right sub-calls fired.
Pure rendering correctness (pixel layout) is not unit-tested.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest


@pytest.fixture
def fake_st(monkeypatch):
    """Replace streamlit with a MagicMock so component code under test
    can call st.markdown / st.altair_chart / st.warning without a real
    Streamlit runtime."""
    import dashboard.components.regime_banner as rb
    fake = MagicMock()
    monkeypatch.setattr(rb, "st", fake)
    return fake


def test_render_regime_banner_handles_empty_table(fake_st):
    from dashboard.components import regime_banner as rb

    # Conn whose load_latest_btc_regime() returns None → "not yet computed".
    fake_conn = MagicMock()
    monkeypatched_loader = MagicMock(return_value=None)
    import dashboard.services.queries as q
    original = q.load_latest_btc_regime
    q.load_latest_btc_regime = monkeypatched_loader
    try:
        rb.render_regime_banner(fake_conn)
    finally:
        q.load_latest_btc_regime = original

    # Should at least have rendered something (markdown or info) without exception.
    assert fake_st.markdown.called or fake_st.info.called or fake_st.warning.called


def test_render_regime_banner_renders_bull_regime(fake_st):
    from dashboard.components import regime_banner as rb
    import dashboard.services.queries as q

    payload = {
        "trade_date": date(2026, 5, 15),
        "regime": "bull",
        "confidence": 1.0,
        "indicators": {
            "close": 81048.9, "ma_50": 78550.21, "ma_200": 71204.15,
            "drawdown_from_ath": -0.05,
            "realized_vol_30d": 0.612, "slope_30d": 0.18,
            "rules_fired": {
                "price_above_ma200": True,
                "ma50_above_ma200": True,
                "drawdown_shallow": True,
            },
            "rules_evaluated_against": "bull",
        },
        "computed_at": datetime.now(timezone.utc),
    }
    original = q.load_latest_btc_regime
    q.load_latest_btc_regime = MagicMock(return_value=payload)
    try:
        rb.render_regime_banner(MagicMock())
    finally:
        q.load_latest_btc_regime = original

    assert fake_st.markdown.called
    args_combined = " ".join(
        c.args[0] for c in fake_st.markdown.call_args_list if c.args
    )
    assert "BULL" in args_combined


def test_render_regime_banner_shows_staleness_warning(fake_st):
    from dashboard.components import regime_banner as rb
    import dashboard.services.queries as q

    stale_date = date.today() - timedelta(days=3)
    payload = {
        "trade_date": stale_date, "regime": "neutral", "confidence": 0.67,
        "indicators": {"close": 80000.0, "rules_fired": {}, "rules_evaluated_against": "bull"},
        "computed_at": datetime.now(timezone.utc),
    }
    original = q.load_latest_btc_regime
    q.load_latest_btc_regime = MagicMock(return_value=payload)
    try:
        rb.render_regime_banner(MagicMock())
    finally:
        q.load_latest_btc_regime = original

    assert fake_st.warning.called or any(
        "stale" in (c.args[0] if c.args else "").lower()
        for c in fake_st.markdown.call_args_list
    )


def test_render_regime_chart_skips_when_history_empty(fake_st):
    from dashboard.components import regime_banner as rb
    import dashboard.services.queries as q

    original = q.load_btc_regime_history
    q.load_btc_regime_history = MagicMock(return_value=pd.DataFrame(
        columns=["trade_date", "close", "regime", "confidence"]))
    try:
        rb.render_regime_chart(MagicMock())
    finally:
        q.load_btc_regime_history = original

    # No crash. The component may caption "no history yet" — we just
    # require it did NOT call st.altair_chart on empty data.
    assert not fake_st.altair_chart.called


def test_render_regime_reference_note_renders_markdown(fake_st):
    from dashboard.components import regime_banner as rb
    rb.render_regime_reference_note()
    assert fake_st.markdown.called
    text = " ".join(c.args[0] for c in fake_st.markdown.call_args_list if c.args)
    assert "TBD" in text or "specifics" in text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/dashboard/test_regime_banner.py -v`
Expected: tests FAIL — `dashboard.components.regime_banner` does not exist.

- [ ] **Step 3: Create the component module**

Create `dashboard/components/regime_banner.py`:

```python
"""BTC market regime banner + 90-day shaded chart + reference note.

Pinned to the top of the Crypto Predictions tab. Observability only —
no trading impact. See
docs/superpowers/specs/2026-05-15-btc-regime-classifier-design.md.
"""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from dashboard.services import queries as q

REGIME_COLORS = {
    "bull": "#2e7d32",
    "neutral": "#b8860b",
    "bear": "#c62828",
}

_REFERENCE_NOTE_MD = """
**Under future regime gating (not yet implemented):**

- **Bull:** strategy runs as today
- **Neutral:** position sizing or entry behavior would adjust — specifics TBD
- **Bear:** new entries reduced or halted — specifics TBD

Validation period through 2026-06-15. Gating logic depends on validation
outcomes plus separate research on non-bull strategies.
"""


def render_regime_banner(conn) -> None:
    """Top banner showing today's regime, confidence, rule status, staleness."""
    payload = q.load_latest_btc_regime(conn)
    if payload is None:
        st.markdown(
            "**BTC REGIME: _not yet computed_** — "
            "run `backfill_btc_regime_history.py` to populate."
        )
        return

    regime = payload["regime"]
    color = REGIME_COLORS.get(regime, "#666")
    confidence_pct = int(round(payload["confidence"] * 100))
    indicators = payload["indicators"]
    rules_fired = indicators.get("rules_fired", {}) or {}
    evaluated = indicators.get("rules_evaluated_against", "bull")
    as_of: date = payload["trade_date"]

    st.markdown(
        f"<div style='padding:12px;border-radius:6px;"
        f"background:{color}22;border-left:6px solid {color}'>"
        f"<span style='font-size:1.4em;font-weight:600;color:{color}'>"
        f"BTC REGIME: {regime.upper()}</span>"
        f" &nbsp;·&nbsp; confidence {confidence_pct}%"
        f" &nbsp;·&nbsp; as of {as_of}"
        f" &nbsp;·&nbsp; rules evaluated: {evaluated}"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Per-rule fire/miss row
    rule_chips = []
    for k, v in rules_fired.items():
        mark = "✓" if v else "✗"
        rule_chips.append(f"{mark} {k}")
    if rule_chips:
        st.markdown(" &nbsp; ".join(rule_chips))

    # Staleness warning if last computed > 1 day behind UTC today.
    if as_of < date.today() - timedelta(days=1):
        st.warning(f"⚠ Regime stale — last computed for {as_of}.")


def render_regime_chart(conn, days: int = 90) -> None:
    """90-day BTC close line chart with regime background shading."""
    df = q.load_btc_regime_history(conn, days=days)
    if df is None or len(df) == 0:
        st.caption("No regime history yet — backfill required.")
        return

    try:
        import altair as alt
    except ImportError:
        st.caption("Install altair for charts.")
        return

    # Collapse contiguous regime runs into bands for background shading.
    bands = []
    if len(df) > 0:
        current_regime = df.iloc[0]["regime"]
        band_start = df.iloc[0]["trade_date"]
        for i in range(1, len(df)):
            r = df.iloc[i]["regime"]
            if r != current_regime:
                bands.append({
                    "start": band_start,
                    "end": df.iloc[i - 1]["trade_date"],
                    "regime": current_regime,
                })
                current_regime = r
                band_start = df.iloc[i]["trade_date"]
        bands.append({
            "start": band_start,
            "end": df.iloc[-1]["trade_date"],
            "regime": current_regime,
        })

    import pandas as pd
    bands_df = pd.DataFrame(bands)
    bands_df["start"] = pd.to_datetime(bands_df["start"])
    bands_df["end"] = pd.to_datetime(bands_df["end"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    background = (
        alt.Chart(bands_df)
        .mark_rect(opacity=0.2)
        .encode(
            x="start:T", x2="end:T",
            color=alt.Color("regime:N",
                            scale=alt.Scale(
                                domain=["bull", "neutral", "bear"],
                                range=[REGIME_COLORS["bull"],
                                       REGIME_COLORS["neutral"],
                                       REGIME_COLORS["bear"]],
                            ),
                            legend=alt.Legend(title="Regime")),
        )
    )
    line = (
        alt.Chart(df)
        .mark_line(color="#222")
        .encode(
            x=alt.X("trade_date:T", title="Date"),
            y=alt.Y("close:Q", title="BTC Close"),
            tooltip=["trade_date:T", "close:Q", "regime:N", "confidence:Q"],
        )
    )
    chart = (background + line).properties(
        title=f"BTC — last {days} days, shaded by regime",
        height=240,
    )
    st.altair_chart(chart, use_container_width=True)


def render_regime_reference_note() -> None:
    """Static markdown — describes hypothetical gating; not implemented."""
    st.markdown(_REFERENCE_NOTE_MD)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/dashboard/test_regime_banner.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/components/regime_banner.py tests/dashboard/test_regime_banner.py
git commit -m "feat(dashboard): add BTC regime banner + 90d shaded chart + reference note"
```

---

## Task 20: Wire the regime panel into `tab_crypto`

**Files:**
- Modify: `dashboard/app.py` (insert imports + render calls at the top of the
  `with tab_crypto:` block at line 401)

**Why:** Connects the new component to the dashboard. The panel renders
before the existing crypto-tab content (filters, table). Smoke-tested
via the existing `test_dashboard_queries.py` runner.

- [ ] **Step 1: Add the imports near the existing dashboard imports**

Open `dashboard/app.py`. Find the imports block near the top of the file
(below the `import streamlit as st` line and the other `from dashboard.*`
imports). Append:

```python
from dashboard.components.regime_banner import (
    render_regime_banner,
    render_regime_chart,
    render_regime_reference_note,
)
```

- [ ] **Step 2: Insert render calls at the top of `with tab_crypto:`**

`dashboard/app.py` line 401 currently begins:

```python
with tab_crypto:
    st.title("Crypto Predictions")
```

Change it to:

```python
with tab_crypto:
    render_regime_banner(conn)
    render_regime_chart(conn, days=90)
    render_regime_reference_note()
    st.divider()
    st.title("Crypto Predictions")
```

(If `conn` is not the variable name in scope at this point, use whatever
the existing `tab_crypto` block uses to get the DuckDB connection —
re-read the immediately-following code to confirm.)

- [ ] **Step 3: Run the dashboard-queries smoke test**

Run: `MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python .claude/local_scripts/test_dashboard_queries.py 2>&1 | tail -20`
Expected: no new errors. The existing 10 queries should still pass; the
new regime queries will execute against the real DB. If `crypto_regime_daily`
is still empty at this point, the banner shows "not yet computed" — which
is the designed empty-state behavior.

- [ ] **Step 4: Run the full crypto + dashboard test suites**

Run: `venv/bin/python -m pytest tests/crypto/ tests/dashboard/ -v 2>&1 | tail -30`
Expected: all tests PASS, no new regressions.

- [ ] **Step 5: Commit**

```bash
git add dashboard/app.py
git commit -m "feat(dashboard): render BTC regime banner + chart at top of Crypto tab"
```

---

## Task 21: Documentation — INFRASTRUCTURE.md + OPERATIONS.md

**Files:**
- Modify: `INFRASTRUCTURE.md` (add a row to the user-services table)
- Modify: `OPERATIONS.md` (add a "Enable BTC regime monitor" subsection)

**Why:** New systemd unit must be discoverable from the canonical infra
docs. Deploy steps for the operator must be explicit including the
`mkdir -p data/logs` step the spec called out.

- [ ] **Step 1: Update INFRASTRUCTURE.md**

Locate the user-services / timers table. Find a comparable existing row
(e.g., `mhde-monitor-phase0-calibration.timer`). Below it, add:

```
| `mhde-monitor-btc-regime.timer`         | daily 00:30 UTC | observability-only BTC market regime classifier — writes one row/day into `crypto_regime_daily`; consumed by the dashboard top-of-Crypto-tab panel |
```

(Column order and exact phrasing should match the existing rows. Use
`grep -n "phase0-calibration" INFRASTRUCTURE.md` to find the precise
context.)

- [ ] **Step 2: Update OPERATIONS.md**

Find the section that documents enabling a new monitor service (search
for `systemctl --user enable` or look for the most recently added
"Enable …" subsection). Add a new subsection in the same style:

```markdown
### Enable BTC regime monitor

One-time deploy after merging `feat-btc-regime-classifier`.

\`\`\`bash
mkdir -p /home/jpcg/MHDE/data/logs
cp systemd/mhde-monitor-btc-regime.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mhde-monitor-btc-regime.timer
systemctl --user list-timers --all | grep btc-regime    # verify next-firing time
\`\`\`

Then run the one-off backfill (operator-driven, gitignored scripts):

\`\`\`bash
venv/bin/python .claude/local_scripts/backfill_btc_2020.py
venv/bin/python .claude/local_scripts/backfill_btc_regime_history.py
\`\`\`

The first call extends `crypto_prices_daily` BTCUSDT back to 2020-01-01.
The second labels every historical day, writes
`data/processed/btc_regime_history.parquet` and
`data/processed/btc_regime_validation.md`, and UPSERTs every row into
`crypto_regime_daily`. Both are idempotent.

No log rotation configured — output is ~1KB/day. Revisit if growth
becomes meaningful.
```

(Strip the escape-backslashes; they're only in this plan to keep markdown
fences from terminating prematurely.)

- [ ] **Step 3: Commit**

```bash
git add INFRASTRUCTURE.md OPERATIONS.md
git commit -m "docs(ops): document mhde-monitor-btc-regime service + deploy steps"
```

---

## Task 22: Run the backfill, commit the parquet + validation MD

**Files:**
- Create (via script): `data/processed/btc_regime_history.parquet`
- Create (via script): `data/processed/btc_regime_validation.md`

**Why:** The backfill is operator-driven (it pulls live Binance data and
takes a few minutes). Commit the resulting parquet and validation MD as
durable evidence of the classifier's labels at branch-creation time.

- [ ] **Step 1: Run the BTC backfill to 2020**

Run: `venv/bin/python .claude/local_scripts/backfill_btc_2020.py 2>&1 | tail -20`
Expected: terminal output ending in something like
`BTCUSDT rows in crypto_prices_daily: 2300+`.

If the BinanceClient hits a rate limit, retry — it's idempotent.

- [ ] **Step 2: Run the regime history backfill**

Run: `venv/bin/python .claude/local_scripts/backfill_btc_regime_history.py 2>&1 | tail -20`
Expected:
- `Wrote /home/jpcg/MHDE/data/processed/btc_regime_history.parquet: ~2300 rows`
- `crypto_regime_daily rows: ~2300`
- `Wrote /home/jpcg/MHDE/data/processed/btc_regime_validation.md`

- [ ] **Step 3: Sanity-check the validation report**

Read `data/processed/btc_regime_validation.md` and confirm:
- Day-count table sums to the row count.
- 2021 bull peak section shows a non-trivial bull count.
- 2022 bear bottom section shows a non-trivial bear count.
- 2023 recovery section shows mostly neutral / early bull.
- The current pullback section's regime matches the operator's
  perception of today's market.

If anything looks wildly off, STOP — do not commit. Re-read
`monitoring/btc_regime.py` and the failing scenario in the validation MD,
add a regression test, fix the code, re-run.

- [ ] **Step 4: Force-add the parquet (it may be gitignored)**

The path `data/processed/` is partly gitignored. Force-add the two new
files explicitly:

```bash
git add -f data/processed/btc_regime_history.parquet
git add -f data/processed/btc_regime_validation.md
```

- [ ] **Step 5: Commit**

```bash
git commit -m "data(crypto): commit BTC regime history + validation evidence"
```

---

## Task 23: Final regression sweep + SESSION_LOG.md + push

**Files:**
- Modify: `SESSION_LOG.md` (prepend a new entry at the top of the file —
  most-recent-first ordering per the file's own header)

**Why:** Cross-chat protocol from `CLAUDE.md`: any substantial workstream
must update `SESSION_LOG.md` before the chat ends. Push the branch (no
PR creation, no merge — per the feedback memory
`feedback_branch_handoff_pattern`).

- [ ] **Step 1: Run the full test suite**

Run: `venv/bin/python -m pytest tests/ -v 2>&1 | tail -40`
Expected: all tests PASS. Pay particular attention to:
- `tests/crypto/` (the new btc_regime tests)
- `tests/dashboard/` (the new query + banner tests)
- `tests/integration/` (no regression)

If any test fails, STOP. Diagnose and fix before continuing.

- [ ] **Step 2: Run the dashboard query smoke**

Run: `MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python .claude/local_scripts/test_dashboard_queries.py 2>&1 | tail -10`
Expected: 10/10 queries pass (or whatever the current expected count is).

- [ ] **Step 3: Restart the Streamlit dashboard service**

Per memory `feedback_restart_streamlit_after_dashboard_merge`, any branch
that touches `dashboard/` requires a Streamlit restart on merge. The
operator does the merge; this task just performs the restart so they
can visually validate before approving.

Run: `systemctl --user restart mhde-streamlit.service && systemctl --user status mhde-streamlit.service 2>&1 | head -10`
Expected: service active (running).

Visual check: open the dashboard URL, click the Crypto tab, confirm the
regime banner renders with the post-backfill regime (most likely
"BULL" or "NEUTRAL" — match your intuition about the current market).

- [ ] **Step 4: Append the SESSION_LOG.md entry**

Prepend the following entry to `SESSION_LOG.md` (top of file, after the
header lines):

```markdown
## 2026-05-15 — BTC market regime classifier (observability only)

**Branch:** `feat-btc-regime-classifier` (pushed; **STOPPED for operator
review/merge** — push-only per branch-handoff pattern, operator opens
the PR). Single-repo (MHDE), no engine repo change.

**Trigger.** Step 1 of a multi-strategy regime-switching architecture.
Strategy is currently regime-blind; backtest evidence (ADR-032, KI-148)
shows tails are regime-dependent. Operator-perceived regime is not
captured anywhere in the system. This branch introduces a daily
rules-based classifier and surfaces it on the dashboard for a 30-day
validation window before any gating logic is built.

**Decision.** Rules-based classifier (interpretable, no ML for v1) on
three indicators: 200d MA position, 50d/200d MA cross, drawdown-from-ATH.
Two informational indicators (30d realized vol, 30d log-price slope)
are persisted but not in the rules. Confidence is the rule-count score
0..1; ties default to bull (`rules_evaluated_against` is recorded per
row for transparency). Drawdown operators flipped from the original
task spec (`Bull: dd > -0.15`, `Bear: dd < -0.25`).

**What shipped.**
- `crypto/schema.py` — `SCHEMA_CRYPTO_REGIME_DAILY` (PK trade_date,
  regime, confidence, indicators_json, computed_at). Added to
  `ALL_SCHEMAS`.
- `monitoring/btc_regime.py` (NEW) — `RegimeIndicators`,
  `RegimeResult`, `compute_indicators`, `classify`, `compute_history`,
  `persist_today`, `run_daily`, `main`.
- `main.py` — `@monitor.command("btc-regime")` subcommand.
- `systemd/mhde-monitor-btc-regime.{service,timer}` (NEW) — daily
  00:30 UTC, log to `data/logs/monitor_btc_regime.log`.
- `dashboard/services/queries.py` — `load_latest_btc_regime`,
  `load_btc_regime_history`.
- `dashboard/components/regime_banner.py` (NEW) — banner + 90d shaded
  chart + reference note ("TBD" gating).
- `dashboard/app.py` — renders regime panel at top of Crypto tab.
- `DATABASE_SCHEMA.md`, `INFRASTRUCTURE.md`, `OPERATIONS.md` — updated.
- `data/processed/btc_regime_history.parquet` (force-added) — full
  classified history from 2020-01-01.
- `data/processed/btc_regime_validation.md` (force-added) — day-count,
  transition-matrix, spot-check, flickers.
- `tests/crypto/test_btc_regime.py` (NEW) — ~30 tests covering
  indicators, classifier rules, boundaries, history, transitions,
  persistence.
- `tests/dashboard/test_regime_queries.py` (NEW) — query helpers.
- `tests/dashboard/test_regime_banner.py` (NEW) — component renders.
- Local scripts (gitignored): `backfill_btc_2020.py`,
  `backfill_btc_regime_history.py`.

**Verification.**
- All new tests pass; full `pytest tests/` green (no regressions).
- Dashboard query smoke: 10/10 pass.
- `systemd-analyze verify` on both new units: clean.
- Validation report spot-check confirms 2021 bull / 2022 bear / 2023
  recovery / current period are labelled consistently with the
  operator's perception.

**Pending (operator-driven).**
- Operator reviews `feat-btc-regime-classifier`, opens PR, merges.
- Operator enables systemd timer per OPERATIONS.md.
- 30-day validation window — operator inspects daily, flags
  misclassifications in `data/processed/btc_regime_validation.md`.
- After validation: dispatch follow-up workstream to gate the strategy
  by regime (separate branch).
```

- [ ] **Step 5: Commit and push**

```bash
git add SESSION_LOG.md
git commit -m "docs: session log entry for btc-regime classifier branch"
git push -u origin feat-btc-regime-classifier
```

Expected: `Branch 'feat-btc-regime-classifier' set up to track 'origin/feat-btc-regime-classifier'.`

- [ ] **Step 6: Final report to the operator**

Write a short summary (in chat, not in a file) covering:
- Branch name + commits ahead of master (`git log master..feat-btc-regime-classifier --oneline | wc -l`).
- Today's classified regime (from `data/processed/btc_regime_validation.md`).
- The deploy steps the operator needs to run after merge (point at
  the OPERATIONS.md subsection).
- The 30-day validation window through 2026-06-15.

**Do not open a PR** — operator does that per the branch-handoff pattern.

---

## Self-Review

### Spec coverage check

| Spec section | Implementing task(s) |
|---|---|
| Architecture (file map) | Task 1 (schema), Task 2 (module skeleton), Task 16 (systemd), Task 19 (dashboard component), Task 20 (wire-in) |
| Module API: indicators | Tasks 3, 4, 5, 6 |
| Module API: classify | Tasks 7, 8, 9, 10 |
| Module API: compute_history | Task 11 |
| Module API: persist_today + run_daily + main | Tasks 13, 14 |
| Classification rules (operators flipped) | Task 7 |
| Confidence formula | Tasks 7, 8 |
| Tie-breaker (bull default) | Task 8 |
| `rules_evaluated_against` recorded | Task 7, 13 (in JSON), 19 (in dashboard) |
| Storage: crypto_regime_daily | Task 1 |
| Storage: indicators_json shape | Task 13 |
| Storage: parquet snapshot | Task 17, 22 |
| Storage: validation MD | Task 17, 22 |
| Storage: DATABASE_SCHEMA.md update | Task 1 |
| Live timer: main.py CLI | Task 15 |
| Live timer: systemd units | Task 16 |
| Live timer: 00:30 UTC, graceful staleness | Tasks 14, 16, 19 |
| Live timer: log dir mkdir | Task 21 (OPERATIONS.md) |
| Live timer: INFRASTRUCTURE.md entry | Task 21 |
| Dashboard: top of Crypto tab placement | Task 20 |
| Dashboard: banner + chart + reference note | Task 19 |
| Dashboard: colors | Task 19 (REGIME_COLORS constants) |
| Dashboard: staleness handling | Task 19 |
| Dashboard: empty-table handling | Task 19 |
| Dashboard: new queries | Task 18 |
| Dashboard: altair pattern match | Task 19 (matches dashboard/components/charts.py) |
| Tests: indicator group | Tasks 3, 4, 5, 6 |
| Tests: classifier rule group | Tasks 7, 8 |
| Tests: boundary conditions | Task 9 |
| Tests: history group | Task 11 |
| Tests: transition / no-smoothing | Task 12 |
| Tests: persistence | Task 13 |
| Tests: integration (run_daily end-to-end) | Task 14 |
| Tests: dashboard query layer | Task 18 |
| Tests: dashboard component layer | Task 19 |
| Tests: full-suite regression green | Task 23 |
| Validation period | Task 23 (SESSION_LOG entry calls out the window) |

No spec section is unimplemented.

### Placeholder scan

No "TBD", "TODO", "implement later", or "similar to Task N" in the plan.
Each step contains the actual file path, the actual code, and the actual
command. The only literal "TBD" string is intentional dashboard text
(Task 19, in the `_REFERENCE_NOTE_MD` constant — operator-requested).

### Type consistency

- `RegimeIndicators` fields and signatures match across Task 2 (definition),
  Tasks 3-6 (implementation), Task 7 (consumer), Task 13 (serialization),
  Task 18 (deserialization), Task 19 (rendering).
- `RegimeResult.rules_fired` is `dict[str, bool]` everywhere it's used.
- `RegimeResult.rules_evaluated_against` is `Regime` everywhere — values
  match between writer (`classify`), serializer (`persist_today`), and
  reader (`load_latest_btc_regime`).
- `classify()` signature is `(ind, *, as_of: date) -> RegimeResult`
  consistently in Tasks 7-12 and 14.
- `compute_indicators` signature is `(prices: list[tuple[date, float]]) -> RegimeIndicators`
  consistently in Tasks 3-6, 11, 14.
- Bull rule key set vs bear rule key set: distinct keys for each
  (`price_above_ma200` vs `price_below_ma200`, etc.) — consistent with
  the spec's intent that `rules_fired` describes the regime that was
  evaluated against.
- Test helper `_ind(...)` and `_series(...)` introduced in Tasks 3/7
  reused consistently in later tasks.

No type or naming inconsistencies found.
