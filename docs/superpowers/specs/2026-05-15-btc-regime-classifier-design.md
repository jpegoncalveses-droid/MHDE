# BTC Market Regime Classifier — Design

**Date:** 2026-05-15
**Branch:** `feat-btc-regime-classifier`
**Status:** Approved by operator (2026-05-15), ready for TDD implementation.
**Scope:** Observability only — no trading impact. Step 1 of a multi-strategy
regime-switching architecture; gating logic and non-bull strategies are
separate follow-up workstreams.

## Why

The crypto strategy currently runs without awareness of broad market regime.
Backtest evidence (DECISIONS.md ADR-032, KI-148) shows the strategy's tails
are regime-dependent: a non-bull market resembling 2022-style drawdowns is
where the kill-switch would trip, but we don't currently *measure* what
regime we're in. Operators carry an intuition ("we're still in a bull
market"); the system carries none.

This branch introduces a daily-computed BTC regime label (bull / neutral /
bear) with full audit trail (which indicators fired, confidence score),
persisted to DuckDB and surfaced on the dashboard. No effect on trading
or predictions. Operators validate the classifier's calls against their
own perception for 30+ days before any gating logic is built on top.

## Scope (in / out)

**In scope (this branch):**

- Pure regime classifier module (rules-based, interpretable, no ML).
- One-off historical backfill of BTCUSDT (2020-01-01 → today) and regime
  labels across the full history, plus a committed validation report.
- Daily live computation via systemd timer at 00:30 UTC.
- Dashboard surfacing (banner + 90d shaded chart) at the top of the
  Crypto tab.
- Test coverage of every indicator, every classifier branch, every
  boundary condition, and the no-smoothing transition behavior.

**Out of scope (separate workstreams):**

- Trading logic changes — purely additive data, no consumer in the
  predict / paper-trading paths.
- Strategies for non-bull regimes (different research initiative).
- ML-based regime detection (v1 stays rules-based for interpretability;
  v2 may revisit once we have labelled validation data).
- Telegram alerts on regime transitions (dashboard-only for v1; revisit
  after the 30-day validation period).
- Dashboard auto-refresh (regime is daily, manual refresh is fine).

## Decisions made during brainstorming

| Decision | Choice | Rationale |
|---|---|---|
| Historical data range | Backfill BTCUSDT 2020-01-01 → today via existing `BinanceClient` | Validation against "known regimes" (2021 bull, 2022 bear, 2023 recovery) requires the data; current `crypto_prices_daily` only has BTC from 2024-04-05. |
| Drawdown threshold operators | Flipped from the original task spec | Spec read `Bull: drawdown < -15%` which meant "more than 15% below ATH" — inverted from intent. Correct: `Bull: drawdown > -15%` (within 15% of ATH), `Bear: drawdown < -25%` (more than 25% below ATH). |
| Dashboard location | Top of Crypto tab | Highest visibility during the 30-day validation window. |
| Confidence/strength score | Rule-count 0..1 with per-rule fire/miss reporting | Interpretable, easy to test, deterministic. |
| Trend strength indicator | Rolling 30d log-price slope (annualized) | Simpler than ADX, easier to test, reported as indicator only (does not enter the regime decision). |
| Transition alerts | None in v1 | Spec says "observability only"; dashboard-only avoids noise during validation. |
| Tie-breaker for neutral confidence | Default to bull's rules when bull-met == bear-met | Deterministic; consistent `rules_fired` shape for the dashboard; rare edge case. Explicitly recorded per row as `rules_evaluated_against`. |

## Architecture

```
ingestion/binance_client.py              (existing; supports long-range backfill already)

monitoring/btc_regime.py                 NEW — pure classifier + persistence
crypto/schema.py                         (extended: + SCHEMA_CRYPTO_REGIME_DAILY)
main.py                                  (extended: + `monitor btc-regime` subcommand)

systemd/mhde-monitor-btc-regime.service  NEW
systemd/mhde-monitor-btc-regime.timer    NEW — OnCalendar=*-*-* 00:30:00

dashboard/services/queries.py            (extended: + load_latest_btc_regime,
                                                       load_btc_regime_history)
dashboard/components/regime_banner.py    NEW — banner + 90d shaded chart + reference note
dashboard/app.py                         (modified: render at top of tab_crypto, ~line 401)

tests/crypto/test_btc_regime.py          NEW — classifier + indicator + persistence tests
tests/dashboard/test_regime_queries.py   NEW — query function tests
tests/dashboard/test_regime_banner.py    NEW — component render tests

.claude/local_scripts/backfill_btc_2020.py            NEW (gitignored path)
.claude/local_scripts/backfill_btc_regime_history.py  NEW (gitignored path)

data/processed/btc_regime_history.parquet    NEW (committed, ~40KB)
data/processed/btc_regime_validation.md      NEW (committed, validation evidence)

docs/superpowers/specs/2026-05-15-btc-regime-classifier-design.md   this file
```

Rationale for `monitoring/btc_regime.py` (not `crypto/regime.py`): matches
the existing observability pattern (`monitoring/paper_trading_drift.py`,
`monitoring/phase0_calibration.py`). When gating logic is added later, it
lives in `crypto/` and *reads* from `crypto_regime_daily` — clean producer
/ consumer split.

## Module API: `monitoring/btc_regime.py`

```python
from dataclasses import dataclass
from datetime import date
from typing import Literal

Regime = Literal["bull", "neutral", "bear"]

@dataclass(frozen=True)
class RegimeIndicators:
    """Raw indicator values for one day, for transparency in dashboard and DB."""
    close: float
    ma_50: float | None              # None if <50 days of history
    ma_200: float | None             # None if <200 days of history
    drawdown_from_ath: float         # signed, in [-1.0, 0.0]
    realized_vol_30d: float | None   # annualized stdev of daily log returns; None if <30d
    slope_30d: float | None          # rolling 30d log-price slope, annualized; None if <30d

@dataclass(frozen=True)
class RegimeResult:
    """One classification with full audit trail."""
    as_of: date
    regime: Regime
    confidence: float                  # 0.0, 0.33, 0.67, or 1.0
    rules_fired: dict[str, bool]       # rule -> fired? against rules_evaluated_against
    rules_evaluated_against: Regime    # 'bull' or 'bear' — which rule set the dict reports
    indicators: RegimeIndicators

def compute_indicators(prices: list[tuple[date, float]]) -> RegimeIndicators:
    """Pure function. Input: ascending (date, close). Output: indicators for the last day."""

def classify(indicators: RegimeIndicators) -> RegimeResult:
    """Pure function. Apply the rule set; return regime + confidence + rule status."""

def compute_history(prices: list[tuple[date, float]]) -> list[RegimeResult]:
    """Run classify() over every day. Days with <200 lookback get ma_200=None
       and regime='neutral', confidence=0.0."""

def persist_today(conn, result: RegimeResult) -> None:
    """UPSERT into crypto_regime_daily."""

def run_daily(db_path: str) -> RegimeResult:
    """Entry point for main.py monitor btc-regime. Loads BTCUSDT from
       crypto_prices_daily, computes regime for max(trade_date), UPSERTs."""
```

### Classification rules

```
Bull:    price > ma_200  AND  ma_50 > ma_200  AND  drawdown_from_ath > -0.15
Bear:    price < ma_200  AND  ma_50 < ma_200  AND  drawdown_from_ath < -0.25
Neutral: anything else
```

All operators are strict (`>` and `<`); exact-threshold values do NOT fire
the rule (tested explicitly).

### Confidence

- Count rules fired in the matched regime's rule set; divide by 3.
- Possible values: 0.0, 0.33, 0.67, 1.0.
- For `regime='neutral'`, confidence is computed as
  `max(bull_rules_met, bear_rules_met) / 3`, and `rules_fired` reports
  the rules of whichever side scored higher (the "closer" regime).
- Tie-breaker when bull_met == bear_met: defaults to bull. Recorded
  per row in `rules_evaluated_against` for transparency.

### Indicator-only fields

`realized_vol_30d` and `slope_30d` are computed and persisted in
`indicators_json` but do NOT enter the v1 decision. They surface on the
dashboard for operator inspection and may inform v2.

## Storage

### New DuckDB table — `crypto_regime_daily`

Added to `crypto/schema.py:SCHEMAS`; auto-provisioned by
`ensure_crypto_schema(conn)`.

```sql
CREATE TABLE IF NOT EXISTS crypto_regime_daily (
    trade_date      DATE PRIMARY KEY,
    regime          VARCHAR NOT NULL,             -- 'bull' | 'neutral' | 'bear'
    confidence      DOUBLE  NOT NULL,             -- 0.0, 0.33, 0.67, 1.0
    indicators_json VARCHAR NOT NULL,             -- JSON: RegimeIndicators + rules_fired
                                                  --       + rules_evaluated_against
    computed_at     TIMESTAMP NOT NULL
);
```

PK on `trade_date`; UPSERT on conflict (idempotent re-runs). No FK to
`crypto_prices_daily` (DuckDB doesn't enforce FKs; join at read time).

`indicators_json` payload example:

```json
{
  "close": 81048.9,
  "ma_50": 78550.21,
  "ma_200": 71204.15,
  "drawdown_from_ath": -0.247,
  "realized_vol_30d": 0.612,
  "slope_30d": 0.18,
  "rules_fired": {
    "price_above_ma200": true,
    "ma50_above_ma200": true,
    "drawdown_shallow": false
  },
  "rules_evaluated_against": "bull"
}
```

### Doc updates

- `DATABASE_SCHEMA.md` — new `### crypto_regime_daily` subsection in the
  Crypto ML group. Writer: `monitoring/btc_regime.py`. Reader:
  `dashboard/services/queries.py`.

### Parquet snapshot — `data/processed/btc_regime_history.parquet`

Generated once by `.claude/local_scripts/backfill_btc_regime_history.py`,
NOT regenerated by the daily timer. Flattened schema (rule columns as
booleans, easier for pandas/notebook analysis):

```
trade_date          date
close               double
ma_50               double (nullable)
ma_200              double (nullable)
drawdown_from_ath   double
realized_vol_30d    double (nullable)
slope_30d           double (nullable)
regime              string
confidence          double
rule_price_above_ma200   bool
rule_ma50_above_ma200    bool
rule_drawdown_shallow    bool
rules_evaluated_against  string
```

~2,300 rows × 13 cols ≈ 40KB. Committed for auditability (`git add -f` if
`.gitignore` blocks it).

### Validation report — `data/processed/btc_regime_validation.md`

Generated by the same backfill script. Committed. Sections:

1. **Day counts per regime across full history** — table with regime,
   days, % of period.
2. **Transition matrix** — counts for every regime → regime transition.
3. **Spot-check sections** — 2021 bull peak, 2022 bear bottom, 2023
   recovery start, 2024 chop, 2025 bull, current pullback. For each: a
   short markdown excerpt showing the classifier's call vs the known
   period label.
4. **Flagged boundary cases** — any day where the classifier flipped on
   a single day adjacent to two days of the opposite regime (a flicker),
   or any region where the operator's intuition disagrees with the
   classifier. Empty section if none; placeholder otherwise.

### Backfill flow (one-off, local scripts)

```
1. .claude/local_scripts/backfill_btc_2020.py
   → BinanceClient.fetch_klines('BTCUSDT', start='2020-01-01', end=today)
   → INSERT ... ON CONFLICT DO NOTHING into crypto_prices_daily
   → expected ~2,300 new rows
   → idempotent: re-running is a no-op

2. .claude/local_scripts/backfill_btc_regime_history.py
   → reads BTCUSDT from crypto_prices_daily (back to 2020-01-01)
   → calls compute_history() → list[RegimeResult]
   → writes data/processed/btc_regime_history.parquet (overwrite)
   → UPSERT every row into crypto_regime_daily
   → writes data/processed/btc_regime_validation.md (overwrite)
```

Operator runs them in order, once, after the branch lands. The systemd
timer takes over from there.

## Live timer + main.py wiring

### `main.py` subcommand

Pattern from `monitor phase0-calibration` (around `main.py:2612`):

```python
@monitor_app.command("btc-regime")
def monitor_btc_regime():
    """BTC market regime classifier — observability only, no trading impact."""
    from monitoring.btc_regime import run_daily
    db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
    result = run_daily(db_path)
    logger.info(f"BTC regime {result.as_of}: {result.regime} "
                f"(confidence {result.confidence:.2f}, "
                f"rules {result.rules_fired})")
```

### systemd units

Pattern lifted from `mhde-monitor-phase0-calibration.{service,timer}`.

**`systemd/mhde-monitor-btc-regime.service`**:

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

**`systemd/mhde-monitor-btc-regime.timer`**:

```ini
[Unit]
Description=MHDE monitor: BTC regime — daily 00:30 UTC

[Timer]
OnCalendar=*-*-* 00:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

### Cron / freshness behavior

00:30 UTC is after the crypto ingestion chain populates `crypto_prices_daily`
for the prior UTC day. `run_daily()` reads the most recent BTCUSDT close
and persists with `trade_date = max(crypto_prices_daily.trade_date WHERE symbol='BTCUSDT')`.

If Binance ingest failed for the day, the regime row is written for the
most recent close that *does* exist — never a fabricated row for a date
with no close. The dashboard surfaces staleness directly (banner shows
`as of YYYY-MM-DD`, with a warning row when `trade_date < today - 1`).

### Log file path & rotation

- Deploy step explicitly runs `mkdir -p /home/jpcg/MHDE/data/logs` before
  enabling the timer (added to `OPERATIONS.md`).
- No log rotation in v1. Daily output is ~1KB; ~365KB/year. Accepted as
  bounded. Revisit if log volume grows meaningfully.

### INFRASTRUCTURE.md

New entry in the user-services table for `mhde-monitor-btc-regime.timer`
and its service pair. Mirrors how `mhde-monitor-paper-trading-drift.timer`
is documented.

### Deploy steps (added to OPERATIONS.md)

```
mkdir -p /home/jpcg/MHDE/data/logs
cp systemd/mhde-monitor-btc-regime.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mhde-monitor-btc-regime.timer
systemctl --user list-timers --all | grep btc-regime  # verify next-firing time
```

## Dashboard surfacing

### Layout — top of Crypto tab (`dashboard/app.py:401`)

```
┌─ Crypto tab ────────────────────────────────────────────┐
│  ┌─ regime banner ────────────────────────────────┐     │  ← NEW
│  │  BTC REGIME: BULL  (confidence 67%)            │     │
│  │  as of 2026-05-15  ·  rules evaluated: bull    │     │
│  │  ✓ price > MA200    ✓ MA50 > MA200    ✗ dd     │     │
│  └────────────────────────────────────────────────┘     │
│  ┌─ 90d price + regime shading ──────────────────┐      │
│  │  [line chart, x=trade_date, y=close]          │      │
│  │  bg shaded green/yellow/red by daily regime   │      │
│  └────────────────────────────────────────────────┘     │
│  ─ reference note ────────────────────────────────       │
│  Under future regime gating (not yet implemented):       │
│   - Bull: strategy runs as today                          │
│   - Neutral: position sizing or entry behavior would      │
│     adjust — specifics TBD                                │
│   - Bear: new entries reduced or halted — specifics TBD   │
│  Validation period through 2026-06-15. Gating logic       │
│  depends on validation outcomes plus separate research    │
│  on non-bull strategies.                                  │
│  ─────────────────────────────────────────────────       │
│                                                          │
│  st.title("Crypto Predictions")           ← existing    │
│  [existing filters / table]               ← existing    │
└──────────────────────────────────────────────────────────┘
```

### New component — `dashboard/components/regime_banner.py`

```python
def render_regime_banner(conn) -> None:
    """Banner with current regime label, confidence, rule status, staleness."""

def render_regime_chart(conn, days: int = 90) -> None:
    """90-day BTC close line chart with regime shading. Matches existing
       chart library convention from dashboard/components/charts.py."""

def render_regime_reference_note() -> None:
    """Static markdown block — 'not implemented, observability only'."""
```

### Colors

- Bull → `#2e7d32` (green)
- Neutral → `#b8860b` (amber)
- Bear → `#c62828` (red)

Constants in `regime_banner.py`. Distinct from Streamlit's standard
warning yellow.

### Staleness handling

- Banner adds a warning row `⚠ regime stale: last computed for YYYY-MM-DD`
  when `max(crypto_regime_daily.trade_date) < today_utc - 1`.
- Empty-table case (pre-backfill): banner shows
  `BTC REGIME: not yet computed — run backfill_btc_regime_history.py`,
  no chart, no crash.

### New queries — `dashboard/services/queries.py`

```python
def load_latest_btc_regime(conn) -> dict | None:
    """Most recent crypto_regime_daily row as a dict (indicators_json parsed),
       or None if empty."""

def load_btc_regime_history(conn, days: int = 90) -> pd.DataFrame:
    """JOIN crypto_regime_daily ⨝ crypto_prices_daily(BTCUSDT) on trade_date,
       last `days`. Columns: trade_date, close, regime, confidence."""
```

Both `@st.cache_data(ttl=300)` per the convention used elsewhere in
`queries.py`.

### Chart implementation

Altair `layer` (or plotly, matching whichever `dashboard/components/charts.py`
already uses):
- Background `mark_rect` per regime span (contiguous runs collapsed in
  Python before passing to the chart library).
- Foreground `mark_line` of `(trade_date, close)`.

No new chart library introduced.

## Testing

### Layered test files

- `tests/crypto/test_btc_regime.py` — classifier module
  (`monitoring/btc_regime.py`): indicators, classifier rules, boundaries,
  history, transitions, persistence, integration.
- `tests/dashboard/test_regime_queries.py` — query functions
  (`load_latest_btc_regime`, `load_btc_regime_history`): empty table,
  populated table, JSON round-trip, staleness shape.
- `tests/dashboard/test_regime_banner.py` — component render functions:
  layout asserts on what Streamlit elements get called (using the existing
  Streamlit mocking pattern in `tests/dashboard/`).

### Test groups (`tests/crypto/test_btc_regime.py`)

```
INDICATORS
├── test_ma50_on_known_series              constant 100 → ma_50 == 100
├── test_ma50_returns_none_under_50d       49 prices → ma_50 is None
├── test_ma200_on_known_series             linear ramp → ma_200 == mean of last 200
├── test_ma200_returns_none_under_200d     199 prices → ma_200 is None
├── test_drawdown_at_ath_is_zero
├── test_drawdown_below_ath_is_negative    peak then 20% fall → dd ≈ -0.20
├── test_realized_vol_zero_on_constant
├── test_realized_vol_positive_on_noise
├── test_realized_vol_none_under_30d
├── test_slope_zero_on_flat
├── test_slope_positive_on_uptrend         exp(k·t) → slope ≈ k·365
├── test_slope_negative_on_downtrend
└── test_slope_none_under_30d

CLASSIFIER RULES
├── test_full_bull_all_rules_fire           → regime=bull, conf=1.0
├── test_full_bear_all_rules_fire           → regime=bear, conf=1.0
├── test_two_of_three_bull_is_neutral       → conf=0.67, evaluated='bull'
├── test_two_of_three_bear_is_neutral       → conf=0.67, evaluated='bear'
├── test_tie_one_bull_one_bear_defaults_bull → conf=0.33, evaluated='bull'
├── test_zero_rules_either_way              → regime=neutral, conf=0.0
└── test_insufficient_history_returns_neutral_with_nulls

BOUNDARY CONDITIONS (strict > and <)
├── test_price_equal_to_ma200_is_neutral
├── test_ma50_equal_to_ma200_is_neutral
├── test_drawdown_exactly_minus_15pct_is_not_shallow
└── test_drawdown_exactly_minus_25pct_is_not_deep

HISTORY COMPUTATION
├── test_compute_history_skips_pre_200d     first 199 → ma_200=None, conf=0.0
├── test_compute_history_idempotent
└── test_compute_history_one_result_per_day

TRANSITION BEHAVIOR
├── test_consecutive_bear_days_all_marked   5 bear-shaped days → 5 'bear' results
├── test_single_day_flip_is_preserved       bull-bull-bear-bull-bull → 'bear' on day 3
│                                            (documents: v1 does NOT smooth)
└── test_transition_from_bull_to_bear_via_neutral

PERSISTENCE / INTEGRATION
├── test_persist_today_inserts_new_row
├── test_persist_today_upserts_existing_row
├── test_indicators_json_roundtrip
└── test_run_daily_end_to_end
```

### Optional real-data smoke (skip-gated)

```python
@pytest.mark.skipif(
    not Path('data/mhde.duckdb').exists(),
    reason='real DB not present (CI / fresh clone)'
)
def test_real_data_2021_bull_peak(): ...
```

Soft tests. Failure means investigate, not block.

### TDD sequence

1. Write `RegimeIndicators` + `RegimeResult` dataclasses → imports compile.
2. Write INDICATOR tests → red. Implement `compute_indicators()` → green.
3. Write CLASSIFIER + BOUNDARY tests → red. Implement `classify()` → green.
4. Write HISTORY tests → red. Implement `compute_history()` → green.
5. Write PERSISTENCE tests → red. Implement `persist_today()` + `run_daily()` → green.
6. Write TRANSITION tests last → mostly green-on-arrival, document the
   no-smoothing decision.

### Regression expectation

All current tests in `tests/crypto/`, `tests/dashboard/`,
`tests/integration/`, and the wider test suite remain green after this
branch lands. Standard expectation; explicit here so it's part of the
acceptance bar.

### Coverage

No percentage target. The test groups exercise every branch of `classify()`
and every public function. Gaps surface in the implementation-plan
checklist before merge, not via a coverage metric.

## Validation period

- Daily timer runs from branch-merge day through 2026-06-15 (30+ days).
- Operator inspects the dashboard panel daily; compares classifier's calls
  against personal perception of market state.
- Any misclassification or counter-intuitive call gets logged to a
  follow-up section in `data/processed/btc_regime_validation.md` (or a
  new entry under KNOWN_ISSUES.md).
- Only after the validation window passes is the next workstream
  dispatched: gate the existing strategy by regime.

## Open questions / non-decisions

None at this time. Decisions explicitly deferred to a follow-up:

- What action does the strategy take in neutral / bear? (Separate research.)
- Are there better indicators for crypto specifically (e.g., on-chain,
  funding-rate regime)? (Possible v2.)
- Should regime transitions trigger Telegram? (Revisit post-validation.)
- ML-based regime detection (kept off the table for v1 by design).
