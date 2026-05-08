"""Phase 1B replay harness — orchestrates the execution backtest.

See ``crypto/execution/backtest/SPEC.md`` for the full design. The harness
ties together the four already-built collaborators:

  * ``costs.py``      — fees / slippage / funding model
  * ``policies.py``   — five exit policies + ``ExitEvent``
  * ``selection.py``  — Top-N and threshold-based daily selection
  * Phase 1A backfill — produces the OOS predictions in
    ``crypto_ml_predictions WHERE model_id LIKE 'crypto_%_walkfold_%'``

This file is the **module skeleton** for step 1 of the harness build:

  * Schemas and creator for the three new ``crypto_backtest_*`` tables
  * Read-only data loaders for predictions / OHLCV / funding / ATR /
    volume rank
  * State containers for an open position and a whole run
  * A deterministic ``run_id`` generator
  * Run-orchestrator stub (raises NotImplementedError)

Step 2 fills in the trade lifecycle (entry / daily step / exit). Step 3
adds output writing. Step 4 adds tests.

Isolation:
  - Reads only: ``crypto_ml_predictions`` (filtered to walkfold IDs),
    ``crypto_prices_daily``, ``crypto_funding_rates``, ``crypto_ml_features``.
  - Writes only: ``crypto_backtest_runs``, ``crypto_backtest_trades``,
    ``crypto_backtest_summary``.
  - No imports from equity / FX / shared ``ml/``. No mutation of any
    existing crypto table.

Why the harness's TP rate is higher than the label hit rate
-----------------------------------------------------------

The training-time label (e.g. ``label_5d_10pct``) is true iff the
maximum **daily close** in the forward window is ``>= entry_close *
(1 + threshold)``. The harness's take-profit logic, by contrast, fires
on the **intraday high** crossing ``entry_open * (1 + tp_pct)``. Two
sources of divergence:

  1. ``high >= close`` always, so the harness will register more hits
     than the label at the same threshold.
  2. ``tp_pct`` is a policy parameter (``0.05`` by default) and is
     usually **not** the same as the label threshold (``0.10``).
     Lower tp_pct → more harness TP fires.

Both effects push the harness TP rate above the label hit rate. A 69 %
harness TP rate against a 52 % label hit rate (Phase 1B smoke,
Policy A, +5 % TP, 5d horizon) is consistent with these two effects;
it is **not** a bug.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

import duckdb
import pandas as pd

from crypto.execution.backtest.costs import (
    TradeCosts,
    compute_trade_costs,
    get_missing_funding_warnings,
    reset_missing_funding_warnings,
)
from crypto.execution.backtest.policies import (
    ExitEvent,
    ExitPolicy,
    build_policy,
)
from crypto.execution.backtest.selection import (
    select_threshold,
    select_top_n,
)

logger = logging.getLogger("mhde.crypto.backtest.harness")


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────


WALKFOLD_PATTERN = "crypto_%_walkfold_%"

# Funding-rate data coverage floor — predictions before this date have
# no funding rows in the table, which would bias policy ranking toward
# longer-hold policies (they'd appear cheaper than they actually are
# during live trading). See SPEC.md "Date range" subsection.
MIN_FUNDING_DATA_DATE: date = date(2025, 4, 5)

# Hold-window gap thresholds, per SPEC.md "Missing data handling".
# Gap of <= FORWARD_FILL_MAX_DAYS calendar days inside the hold window
# is forward-filled. Gap of >= DATA_GAP_EXIT_DAYS triggers an early
# exit with reason='data_gap'.
FORWARD_FILL_MAX_DAYS = 2
DATA_GAP_EXIT_DAYS = 3


# ──────────────────────────────────────────────────────────────────────
# Schemas — three new tables, all written exclusively by the harness
# ──────────────────────────────────────────────────────────────────────


SCHEMA_CRYPTO_BACKTEST_RUNS = """
CREATE TABLE IF NOT EXISTS crypto_backtest_runs (
    run_id                       VARCHAR PRIMARY KEY,
    run_timestamp                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    horizon                      VARCHAR NOT NULL,
    exit_policy                  VARCHAR NOT NULL,
    selection_rule               VARCHAR NOT NULL,
    parameters                   VARCHAR,
    date_start                   DATE,
    date_end                     DATE,
    n_predictions_seen           INTEGER,
    n_trades                     INTEGER,
    n_skipped_duplicates         INTEGER,
    n_skipped_missing_atr        INTEGER,
    n_data_gap_exits             INTEGER,
    n_forward_fills              INTEGER,
    n_excluded_by_funding_floor  INTEGER,
    n_missing_funding_warnings   INTEGER
);
"""

# Idempotent migrations — applied after CREATE so existing live tables
# (which may have the old skeleton schema) gain the new counters without
# losing data. All ALTER calls are no-ops on freshly-created tables.
_RUNS_TABLE_MIGRATIONS = [
    "ALTER TABLE crypto_backtest_runs "
    "ADD COLUMN IF NOT EXISTS n_predictions_seen INTEGER",
    "ALTER TABLE crypto_backtest_runs "
    "ADD COLUMN IF NOT EXISTS n_skipped_duplicates INTEGER",
    "ALTER TABLE crypto_backtest_runs "
    "ADD COLUMN IF NOT EXISTS n_skipped_missing_atr INTEGER",
    "ALTER TABLE crypto_backtest_runs "
    "ADD COLUMN IF NOT EXISTS n_excluded_by_funding_floor INTEGER",
    "ALTER TABLE crypto_backtest_runs "
    "ADD COLUMN IF NOT EXISTS n_missing_funding_warnings INTEGER",
    # Drop the deprecated counters from the original skeleton schema.
    "ALTER TABLE crypto_backtest_runs DROP COLUMN IF EXISTS n_predictions",
    "ALTER TABLE crypto_backtest_runs DROP COLUMN IF EXISTS n_skipped",
    # Summary table — added in step 4 (metrics.py).
    "ALTER TABLE crypto_backtest_summary "
    "ADD COLUMN IF NOT EXISTS total_slippage_paid_pct DOUBLE",
]

SCHEMA_CRYPTO_BACKTEST_TRADES = """
CREATE TABLE IF NOT EXISTS crypto_backtest_trades (
    run_id               VARCHAR NOT NULL,
    trade_id             VARCHAR NOT NULL,
    coin                 VARCHAR NOT NULL,
    entry_date           DATE NOT NULL,
    entry_price          DOUBLE NOT NULL,
    exit_date            DATE,
    exit_price           DOUBLE,
    exit_reason          VARCHAR,
    holding_days         INTEGER,
    gross_pnl_pct        DOUBLE,
    fee_pct              DOUBLE,
    slippage_pct         DOUBLE,
    funding_pct          DOUBLE,
    net_pnl_pct          DOUBLE,
    probability_at_entry DOUBLE,
    forward_fill_days    INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, trade_id)
);
"""

SCHEMA_CRYPTO_BACKTEST_SUMMARY = """
CREATE TABLE IF NOT EXISTS crypto_backtest_summary (
    run_id                   VARCHAR PRIMARY KEY,
    net_pnl_total_pct        DOUBLE,
    net_pnl_annualized_pct   DOUBLE,
    sharpe_ratio             DOUBLE,
    max_drawdown_pct         DOUBLE,
    hit_rate                 DOUBLE,
    avg_winner_pct           DOUBLE,
    avg_loser_pct            DOUBLE,
    profit_factor            DOUBLE,
    avg_holding_days         DOUBLE,
    pct_exits_tp             DOUBLE,
    pct_exits_sl             DOUBLE,
    pct_exits_trailing       DOUBLE,
    pct_exits_time           DOUBLE,
    pct_exits_data_gap       DOUBLE,
    total_fees_paid_pct      DOUBLE,
    total_funding_paid_pct   DOUBLE,
    total_slippage_paid_pct  DOUBLE
);
"""


def ensure_backtest_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the three backtest tables if they don't exist, then apply
    idempotent migrations to evolve any existing copies to the current
    schema. Safe to call repeatedly."""
    conn.execute(SCHEMA_CRYPTO_BACKTEST_RUNS)
    conn.execute(SCHEMA_CRYPTO_BACKTEST_TRADES)
    conn.execute(SCHEMA_CRYPTO_BACKTEST_SUMMARY)
    for stmt in _RUNS_TABLE_MIGRATIONS:
        conn.execute(stmt)


# ──────────────────────────────────────────────────────────────────────
# Read-only data loaders
# ──────────────────────────────────────────────────────────────────────


def load_oos_predictions(
    conn: duckdb.DuckDBPyConnection,
    horizon: str,
    *,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
    apply_funding_floor: bool = True,
) -> pd.DataFrame:
    """Read walk-forward OOS predictions for ``horizon``.

    Filters strictly to backfill model_ids (``model_id LIKE
    'crypto_%_walkfold_%'``) so the live active model rows are never
    consumed by the harness. Also applies the
    :data:`MIN_FUNDING_DATA_DATE` floor by default — predictions whose
    ``prediction_date < 2025-04-05`` are excluded because the
    ``crypto_funding_rates`` table does not cover those dates and
    including them would bias the cost model. Set
    ``apply_funding_floor=False`` to bypass (testing only).

    Returned columns: ``coin, date, horizon, model_id, probability,
    actual_max_return, actual_max_drawdown, actual_hit, outcome_filled_at``.
    """
    sql = f"""
        SELECT symbol AS coin,
               prediction_date AS date,
               horizon,
               model_id,
               predicted_probability AS probability,
               actual_max_return,
               actual_max_drawdown,
               actual_hit,
               outcome_filled_at
        FROM crypto_ml_predictions
        WHERE model_id LIKE '{WALKFOLD_PATTERN}'
          AND horizon = ?
    """
    params: list[Any] = [horizon]
    if apply_funding_floor:
        sql += " AND prediction_date >= ?"
        params.append(MIN_FUNDING_DATA_DATE)
    if date_start is not None:
        sql += " AND prediction_date >= ?"
        params.append(date_start)
    if date_end is not None:
        sql += " AND prediction_date <= ?"
        params.append(date_end)
    sql += " ORDER BY prediction_date, symbol"
    return conn.execute(sql, params).fetchdf()


def count_predictions_below_funding_floor(
    conn: duckdb.DuckDBPyConnection,
    horizon: str,
    *,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
) -> int:
    """Count walkfold predictions in the user-supplied date window that
    would be excluded by :data:`MIN_FUNDING_DATA_DATE`. Used by
    :func:`run_backtest` for visibility logging."""
    sql = f"""
        SELECT COUNT(*) FROM crypto_ml_predictions
        WHERE model_id LIKE '{WALKFOLD_PATTERN}'
          AND horizon = ?
          AND prediction_date < ?
    """
    params: list[Any] = [horizon, MIN_FUNDING_DATA_DATE]
    if date_start is not None:
        sql += " AND prediction_date >= ?"
        params.append(date_start)
    if date_end is not None:
        sql += " AND prediction_date <= ?"
        params.append(date_end)
    return int(conn.execute(sql, params).fetchone()[0])


def load_ohlcv(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    *,
    date_start: date,
    date_end: date,
) -> pd.DataFrame:
    """Read daily OHLCV for ``symbols`` over ``[date_start, date_end]``.

    Used by the harness's daily-step loop. For symbols absent on a given
    day the row is simply missing (caller applies forward-fill or
    data_gap exit per SPEC.md missing-data rules).
    """
    if not symbols:
        return pd.DataFrame(
            columns=["symbol", "trade_date", "open", "high", "low", "close",
                     "volume"]
        )
    placeholders = ",".join(["?"] * len(symbols))
    sql = f"""
        SELECT symbol, trade_date, open, high, low, close, volume
        FROM crypto_prices_daily
        WHERE symbol IN ({placeholders})
          AND trade_date >= ?
          AND trade_date <= ?
        ORDER BY symbol, trade_date
    """
    return conn.execute(sql, list(symbols) + [date_start, date_end]).fetchdf()


def load_funding(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    *,
    dt_start: datetime,
    dt_end: datetime,
) -> pd.DataFrame:
    """Read funding rates for ``symbols`` over ``[dt_start, dt_end]``.

    Returns a long frame with columns ``symbol, funding_time, funding_rate``
    matching the shape ``costs.funding_payments_during_hold`` consumes.
    The harness pre-filters per-coin before calling that function.
    """
    if not symbols:
        return pd.DataFrame(columns=["symbol", "funding_time", "funding_rate"])
    placeholders = ",".join(["?"] * len(symbols))
    sql = f"""
        SELECT symbol, funding_time, funding_rate
        FROM crypto_funding_rates
        WHERE symbol IN ({placeholders})
          AND funding_time >= ?
          AND funding_time <= ?
        ORDER BY symbol, funding_time
    """
    return conn.execute(sql, list(symbols) + [dt_start, dt_end]).fetchdf()


def load_atr_at_entry(
    conn: duckdb.DuckDBPyConnection,
    keys: list[tuple[str, date]],
) -> dict[tuple[str, date], float]:
    """Read ``atr_pct_14d`` from ``crypto_ml_features`` for the given
    ``(symbol, trade_date)`` keys. Returns a dict for O(1) lookup keyed
    by the same tuples; missing entries are absent from the dict
    (caller decides how to handle — Policy C will skip the trade if
    ATR is unavailable).
    """
    if not keys:
        return {}
    # Build a temp view of (symbol, trade_date) so we can do one JOIN.
    view = pd.DataFrame(keys, columns=["symbol", "trade_date"])
    view_name = f"_atr_keys_{id(view):x}"
    conn.register(view_name, view)
    try:
        rows = conn.execute(
            f"""
            SELECT v.symbol, v.trade_date, f.atr_pct_14d
            FROM {view_name} v
            LEFT JOIN crypto_ml_features f
              ON f.symbol = v.symbol AND f.trade_date = v.trade_date
            WHERE f.atr_pct_14d IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.unregister(view_name)
    return {(s, d): float(a) for s, d, a in rows}


def compute_volume_ranks(
    conn: duckdb.DuckDBPyConnection,
    *,
    as_of: date,
    lookback_days: int = 30,
) -> dict[str, int]:
    """Compute coin volume ranks (1 = most-traded) from average daily
    USD volume (``close * volume``) over the ``lookback_days`` calendar
    days ending at ``as_of``. Used by ``costs.classify_slippage_tier``.

    The ranks are computed once per run (not per trade) — slippage tier
    is a coarse classification and doesn't need to track the rolling
    rank per trade entry.
    """
    rows = conn.execute(
        f"""
        WITH recent AS (
            SELECT symbol, AVG(close * volume) AS avg_dollar_vol
            FROM crypto_prices_daily
            WHERE trade_date >  ?::DATE - INTERVAL '{lookback_days} days'
              AND trade_date <= ?
            GROUP BY symbol
        )
        SELECT symbol,
               RANK() OVER (ORDER BY avg_dollar_vol DESC) AS rk
        FROM recent
        WHERE avg_dollar_vol IS NOT NULL
        ORDER BY rk
        """,
        [as_of, as_of],
    ).fetchall()
    return {symbol: int(rk) for symbol, rk in rows}


# ──────────────────────────────────────────────────────────────────────
# State containers
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Position:
    """One open simulated trade tracked by :func:`run_backtest`.

    Step 2 of the harness build will populate the lifecycle fields
    (``days_held``, ``forward_fill_days``, ``exits``, ``costs``).
    """

    trade_id: str
    coin: str
    entry_date: date
    entry_price: float
    horizon: str
    policy: ExitPolicy
    probability_at_entry: float
    last_known_close: float = 0.0
    last_known_high: float = 0.0
    last_known_low: float = 0.0
    days_held: int = 0
    forward_fill_days: int = 0
    exits: list[ExitEvent] = field(default_factory=list)
    closed: bool = False
    exit_date: Optional[date] = None
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    costs: Optional[TradeCosts] = None

    def __post_init__(self) -> None:
        # Initialise OHLC carry-forward values so step 2's forward-fill
        # logic always has something to read.
        self.last_known_close = float(self.entry_price)
        self.last_known_high = float(self.entry_price)
        self.last_known_low = float(self.entry_price)


@dataclass
class SkippedPrediction:
    """Records why the harness refused to open a trade for a prediction."""

    coin: str
    date: date
    reason: str   # 'missing_entry_price' | 'duplicate_open_position' | 'missing_atr' | ...


@dataclass
class RunState:
    """Aggregate state for one ``run_backtest`` invocation. The
    orchestrator owns this; nothing outside the harness should mutate it."""

    run_id: str
    horizon: str
    exit_policy_id: str
    selection_rule: str
    parameters: dict[str, Any]
    date_start: Optional[date] = None
    date_end: Optional[date] = None

    open_positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[Position] = field(default_factory=list)
    skipped: list[SkippedPrediction] = field(default_factory=list)
    n_predictions_seen: int = 0
    n_forward_fills: int = 0
    n_data_gap_exits: int = 0
    n_skipped_duplicates: int = 0
    n_excluded_by_funding_floor: int = 0
    n_missing_funding_warnings: int = 0
    # Actual prediction-date range observed in the data after the funding
    # floor + user date bounds. Populated by _run_lifecycle. Persisted into
    # crypto_backtest_runs.{date_start,date_end} so metrics.py can compute
    # a meaningful annualized return.
    effective_date_start: Optional[date] = None
    effective_date_end: Optional[date] = None


# ──────────────────────────────────────────────────────────────────────
# Run-id generation — deterministic from configuration
# ──────────────────────────────────────────────────────────────────────


def make_run_id(
    *,
    horizon: str,
    exit_policy_id: str,
    selection_rule: str,
    selection_params: Optional[dict[str, Any]] = None,
    policy_params: Optional[dict[str, Any]] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
) -> str:
    """Deterministic run_id from the run's configuration. Two invocations
    with identical configuration produce the same run_id; this enables
    idempotent re-runs and PK-based deduplication.

    Format: ``backtest_{horizon}_{policy}_{selection}_{8-char hash}``.

    >>> make_run_id(horizon="5d", exit_policy_id="A", selection_rule="top_n",
    ...              selection_params={"n": 6})
    'backtest_5d_A_top_n_...'  # last segment is sha1[:8] of the canonical key
    """
    key = json.dumps(
        {
            "horizon": horizon,
            "exit_policy_id": exit_policy_id.upper(),
            "selection_rule": selection_rule,
            "selection_params": selection_params or {},
            "policy_params": policy_params or {},
            "date_start": str(date_start) if date_start else None,
            "date_end": str(date_end) if date_end else None,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return (
        f"backtest_{horizon}_{exit_policy_id.upper()}_"
        f"{selection_rule}_{digest}"
    )


# ──────────────────────────────────────────────────────────────────────
# Run orchestrator — skeleton (lifecycle filled in step 2)
# ──────────────────────────────────────────────────────────────────────


def _coerce_date(value: Any) -> date:
    """Coerce DuckDB's datetime64-backed DATE values into Python ``date``
    so dict lookups keyed by Python ``date`` work consistently."""
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        return value.to_pydatetime().date()
    return value


def _apply_selection(
    candidates: pd.DataFrame, selection_rule: str,
    selection_params: dict[str, Any],
) -> pd.DataFrame:
    """Dispatch on ``selection_rule`` to either Top-N or threshold."""
    if selection_rule == "top_n":
        n = int(selection_params.get("n", 6))
        return select_top_n(candidates, n=n)
    if selection_rule == "threshold":
        threshold = float(selection_params.get("threshold", 0.55))
        return select_threshold(candidates, threshold=threshold)
    raise ValueError(
        f"unknown selection_rule {selection_rule!r}; "
        f"expected 'top_n' or 'threshold'"
    )


def _build_position(
    *,
    coin: str, pred_date: date, entry_date: date, entry_price: float,
    horizon: str, horizon_days: int, exit_policy_id: str,
    policy_params: dict[str, Any], probability: float,
    atr_lookup: dict[tuple[str, date], float],
    trade_id: str,
) -> tuple[Optional[Position], Optional[str]]:
    """Build an :class:`ExitPolicy` and wrap in a :class:`Position`.

    Returns (Position, None) on success, or (None, reason) if the trade
    cannot be opened (e.g. Policy C with missing ATR).
    """
    params = dict(policy_params)
    if exit_policy_id.upper() == "C":
        atr_pct = atr_lookup.get((coin, pred_date))
        if atr_pct is None or atr_pct <= 0:
            return None, "missing_atr"
        params["atr_pct"] = float(atr_pct)
    try:
        policy = build_policy(
            exit_policy_id, entry_price=entry_price,
            horizon_days=horizon_days, params=params,
        )
    except Exception as exc:
        return None, f"policy_build_failed:{type(exc).__name__}:{exc}"

    return Position(
        trade_id=trade_id, coin=coin, entry_date=entry_date,
        entry_price=entry_price, horizon=horizon, policy=policy,
        probability_at_entry=float(probability),
    ), None


def _walk_position_forward(
    position: Position,
    *,
    horizon_days: int,
    ohlcv_by_key: dict[tuple[str, date], tuple[float, float, float, float]],
) -> tuple[bool, int]:
    """Drive one position day-by-day until exit. Mutates ``position``.

    Returns (data_gap_exited: bool, n_forward_fills: int) so the caller
    can roll counters into RunState.
    """
    forward_fill_streak = 0
    n_forward_fills = 0
    data_gap_exited = False

    for day_idx in range(1, horizon_days + 1):
        bar_date = position.entry_date + timedelta(days=day_idx)
        bar = ohlcv_by_key.get((position.coin, bar_date))
        if bar is None:
            forward_fill_streak += 1
            if forward_fill_streak >= DATA_GAP_EXIT_DAYS:
                # Exit at last known close before the gap started.
                exit_price = position.last_known_close
                position.exits.append(ExitEvent(
                    exit_price=exit_price,
                    fraction=position.policy.remaining_fraction,
                    reason="data_gap",
                ))
                # Last real bar was the day before the gap began,
                # i.e. (forward_fill_streak) days before today.
                position.exit_date = bar_date - timedelta(days=forward_fill_streak)
                position.exit_price = exit_price
                position.exit_reason = "data_gap"
                data_gap_exited = True
                break
            # Forward-fill (1-2 missing day case).
            n_forward_fills += 1
            position.forward_fill_days += 1
            high = low = close = position.last_known_close
        else:
            forward_fill_streak = 0
            _, hi, lo, cl = bar
            high, low, close = float(hi), float(lo), float(cl)
            position.last_known_high = high
            position.last_known_low = low
            position.last_known_close = close

        position.days_held = day_idx
        events = position.policy.step(day_idx, high, low, close)
        if events:
            position.exits.extend(events)
            position.exit_date = bar_date
            # Record the most-recent exit reason / price as the trade's
            # representative values (Policy E may have two events; the
            # last one is the closing tranche).
            position.exit_price = float(events[-1].exit_price)
            position.exit_reason = events[-1].reason
        if position.policy.is_complete:
            break

    # Defensive: if no event fired and policy isn't complete (shouldn't
    # happen with the existing policies, but explicit fallback), close at
    # last known close as a time stop.
    if not position.policy.is_complete and not data_gap_exited:
        position.exits.append(ExitEvent(
            exit_price=position.last_known_close,
            fraction=position.policy.remaining_fraction,
            reason="time",
        ))
        position.exit_date = position.entry_date + timedelta(days=horizon_days)
        position.exit_price = position.last_known_close
        position.exit_reason = "time"

    return data_gap_exited, n_forward_fills


def _gross_pnl_pct(position: Position) -> float:
    """Weighted gross P&L across all ExitEvents (handles Policy E partial
    fills). Returns a fraction; multiply by 100 for percentage display."""
    if position.entry_price <= 0:
        return 0.0
    return float(sum(
        ((evt.exit_price / position.entry_price) - 1.0) * evt.fraction
        for evt in position.exits
    ))


def _funding_window_for_position(
    position: Position, funding_by_coin: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Slice the per-coin funding frame to ``[entry, exit]`` for cost calc."""
    coin_frame = funding_by_coin.get(position.coin)
    if coin_frame is None or coin_frame.empty:
        return pd.DataFrame(columns=["funding_time", "funding_rate"])
    return coin_frame


def _run_lifecycle(
    conn: duckdb.DuckDBPyConnection,
    state: RunState,
    *,
    selection_params: dict[str, Any],
    policy_params: dict[str, Any],
) -> RunState:
    """End-to-end lifecycle for one run, populating ``state``. Pure
    in-memory work — no DB writes (those land in step 3)."""
    horizon = state.horizon
    horizon_days = int(horizon.rstrip("d"))

    # Predictions, with the funding-data floor applied.
    preds = load_oos_predictions(
        conn, horizon,
        date_start=state.date_start, date_end=state.date_end,
    )
    state.n_predictions_seen = len(preds)
    if preds.empty:
        return state

    # Snapshot the effective prediction window (post-funding-floor +
    # post-user-bounds) so the runs row has populated date_start/date_end
    # for metrics.py to compute annualized return.
    state.effective_date_start = _coerce_date(preds["date"].min())
    state.effective_date_end = _coerce_date(preds["date"].max())

    # Selection.
    candidates = _apply_selection(preds, state.selection_rule, selection_params)
    if candidates.empty:
        return state

    # Pre-load all per-coin reference data needed for the daily step.
    candidates = candidates.copy()
    candidates["date"] = candidates["date"].apply(_coerce_date)
    coins = sorted(candidates["coin"].unique().tolist())
    pred_min = candidates["date"].min()
    pred_max = candidates["date"].max()
    # Entry is at T+1 open; exit at latest at T+1+horizon_days. Pull a
    # cushion so forward-fill lookups don't miss boundary rows.
    price_start = pred_min + timedelta(days=1)
    price_end = pred_max + timedelta(days=horizon_days + 1)

    ohlcv = load_ohlcv(conn, coins, date_start=price_start, date_end=price_end)
    funding = load_funding(
        conn, coins,
        dt_start=datetime.combine(price_start, time.min),
        dt_end=datetime.combine(price_end, time.max),
    )
    atr_keys = list({(c, d) for c, d in zip(candidates["coin"], candidates["date"])})
    atrs = load_atr_at_entry(conn, atr_keys)
    # Per-run volume ranks — coarse classification per spec.
    volume_ranks = compute_volume_ranks(conn, as_of=pred_max, lookback_days=30)

    # Index OHLCV by (coin, date) for O(1) lookup. Coerce DATE to Python date.
    ohlcv_by_key: dict[tuple[str, date], tuple[float, float, float, float]] = {}
    for r in ohlcv.itertuples(index=False):
        d = _coerce_date(r.trade_date)
        ohlcv_by_key[(r.symbol, d)] = (
            float(r.open), float(r.high), float(r.low), float(r.close),
        )

    funding_by_coin: dict[str, pd.DataFrame] = {
        c: g.reset_index(drop=True) for c, g in funding.groupby("symbol")
    }

    # Reset the missing-funding counter so we attribute warnings to this run.
    reset_missing_funding_warnings()

    # Per-coin most-recent exit_date — used to skip a prediction whose
    # entry date would land inside an already-open position on the same
    # coin. The harness processes candidates in (date, prob desc, coin)
    # order, so this is sufficient: any future candidate whose entry
    # falls on/before last_exit_by_coin[coin] overlaps the prior trade.
    last_exit_by_coin: dict[str, date] = {}

    trade_seq = 0
    for _, candidate in candidates.iterrows():
        coin = str(candidate["coin"])
        pred_date = _coerce_date(candidate["date"])
        prob = float(candidate["probability"])
        entry_date = pred_date + timedelta(days=1)

        # Duplicate-position guard — at most one open position per coin.
        # If the previous trade on this coin is still open at entry_date,
        # skip today's signal.
        prev_exit = last_exit_by_coin.get(coin)
        if prev_exit is not None and prev_exit >= entry_date:
            state.skipped.append(SkippedPrediction(
                coin=coin, date=pred_date,
                reason="duplicate_open_position",
            ))
            state.n_skipped_duplicates += 1
            continue

        # Entry price — T+1 open. Skip if missing.
        entry_bar = ohlcv_by_key.get((coin, entry_date))
        if entry_bar is None:
            state.skipped.append(SkippedPrediction(
                coin=coin, date=pred_date, reason="missing_entry_price",
            ))
            continue
        entry_open, _, _, _ = entry_bar
        if entry_open <= 0:
            state.skipped.append(SkippedPrediction(
                coin=coin, date=pred_date, reason="non_positive_entry_price",
            ))
            continue

        trade_seq += 1
        trade_id = f"{state.run_id}_{trade_seq:06d}"
        position, build_error = _build_position(
            coin=coin, pred_date=pred_date, entry_date=entry_date,
            entry_price=float(entry_open), horizon=horizon,
            horizon_days=horizon_days, exit_policy_id=state.exit_policy_id,
            policy_params=policy_params, probability=prob,
            atr_lookup=atrs, trade_id=trade_id,
        )
        if position is None:
            state.skipped.append(SkippedPrediction(
                coin=coin, date=pred_date, reason=build_error or "build_failed",
            ))
            trade_seq -= 1   # don't burn a sequence number on a skip
            continue
        state.open_positions[trade_id] = position

        # Walk forward day-by-day with forward-fill / data-gap handling.
        data_gap_exited, n_ff = _walk_position_forward(
            position, horizon_days=horizon_days, ohlcv_by_key=ohlcv_by_key,
        )
        if data_gap_exited:
            state.n_data_gap_exits += 1
        state.n_forward_fills += n_ff

        # Cost computation via costs.py.
        coin_funding = _funding_window_for_position(position, funding_by_coin)
        entry_dt = datetime.combine(position.entry_date, time.min)
        # exit_date may have been pushed earlier by data_gap; use end-of-day
        # of whichever date the position closed.
        close_date = position.exit_date or (
            position.entry_date + timedelta(days=horizon_days)
        )
        exit_dt = datetime.combine(close_date, time.max)
        position.costs = compute_trade_costs(
            volume_rank=volume_ranks.get(coin),
            entry_dt=entry_dt, exit_dt=exit_dt,
            funding_rates=coin_funding,
        )
        position.closed = True

        # Move to closed bucket.
        state.closed_trades.append(position)
        state.open_positions.pop(trade_id, None)

        # Track this exit for the next iteration's duplicate-position guard.
        if position.exit_date is not None:
            prev = last_exit_by_coin.get(coin)
            if prev is None or position.exit_date > prev:
                last_exit_by_coin[coin] = position.exit_date

    return state


def _n_skipped_for_reason(state: RunState, reason: str) -> int:
    return sum(1 for s in state.skipped if s.reason == reason)


def _persist_run(
    conn: duckdb.DuckDBPyConnection,
    state: RunState,
    *,
    force: bool,
) -> None:
    """Transactionally write ``state`` to ``crypto_backtest_runs`` and
    ``crypto_backtest_trades``.

    Idempotency: if ``state.run_id`` already exists, raise unless
    ``force=True`` is passed — in which case prior rows for the same
    ``run_id`` are deleted from both tables before writing.

    Both inserts run inside a single ``BEGIN/COMMIT`` (or ``ROLLBACK``
    on any failure). Caller must not have an outer transaction open.
    """
    existing = conn.execute(
        "SELECT 1 FROM crypto_backtest_runs WHERE run_id = ?",
        [state.run_id],
    ).fetchone()
    if existing is not None and not force:
        raise RuntimeError(
            f"run_id {state.run_id!r} already exists in "
            f"crypto_backtest_runs. Re-run with --force to overwrite, "
            f"or delete the prior rows from crypto_backtest_runs and "
            f"crypto_backtest_trades first."
        )

    params_json = json.dumps(state.parameters, default=str, sort_keys=True)
    n_skipped_missing_atr = _n_skipped_for_reason(state, "missing_atr")

    # Prefer the data-driven effective range over the user-provided
    # bounds — it's what metrics.py needs for annualization, and it's a
    # more faithful description of what actually ran.
    persist_date_start = state.effective_date_start or state.date_start
    persist_date_end = state.effective_date_end or state.date_end

    run_row = [
        state.run_id, state.horizon, state.exit_policy_id,
        state.selection_rule, params_json,
        persist_date_start, persist_date_end,
        state.n_predictions_seen, len(state.closed_trades),
        state.n_skipped_duplicates, n_skipped_missing_atr,
        state.n_data_gap_exits, state.n_forward_fills,
        state.n_excluded_by_funding_floor,
        state.n_missing_funding_warnings,
    ]

    trade_rows: list[list[Any]] = []
    for t in state.closed_trades:
        gross = _gross_pnl_pct(t)
        costs_obj = t.costs
        fee = float(costs_obj.fee_total) if costs_obj is not None else 0.0
        slip = float(costs_obj.slippage_total) if costs_obj is not None else 0.0
        funding = float(costs_obj.funding) if costs_obj is not None else 0.0
        net = gross - (float(costs_obj.total) if costs_obj is not None else 0.0)
        holding = (
            (t.exit_date - t.entry_date).days
            if t.exit_date is not None else None
        )
        trade_rows.append([
            state.run_id, t.trade_id, t.coin,
            t.entry_date, float(t.entry_price),
            t.exit_date,
            float(t.exit_price) if t.exit_price is not None else None,
            t.exit_reason, holding,
            gross, fee, slip, funding, net,
            float(t.probability_at_entry), int(t.forward_fill_days),
        ])

    conn.execute("BEGIN TRANSACTION")
    try:
        if existing is not None and force:
            n_old = conn.execute(
                "SELECT COUNT(*) FROM crypto_backtest_trades WHERE run_id = ?",
                [state.run_id],
            ).fetchone()[0]
            conn.execute(
                "DELETE FROM crypto_backtest_trades WHERE run_id = ?",
                [state.run_id],
            )
            conn.execute(
                "DELETE FROM crypto_backtest_runs WHERE run_id = ?",
                [state.run_id],
            )
            logger.warning(
                "Force-deleted prior run %s (1 run row + %d trade rows)",
                state.run_id, n_old,
            )

        conn.execute(
            """
            INSERT INTO crypto_backtest_runs (
                run_id, horizon, exit_policy, selection_rule, parameters,
                date_start, date_end,
                n_predictions_seen, n_trades,
                n_skipped_duplicates, n_skipped_missing_atr,
                n_data_gap_exits, n_forward_fills,
                n_excluded_by_funding_floor, n_missing_funding_warnings
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            run_row,
        )

        if trade_rows:
            conn.executemany(
                """
                INSERT INTO crypto_backtest_trades (
                    run_id, trade_id, coin, entry_date, entry_price,
                    exit_date, exit_price, exit_reason, holding_days,
                    gross_pnl_pct, fee_pct, slippage_pct, funding_pct,
                    net_pnl_pct, probability_at_entry, forward_fill_days
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                trade_rows,
            )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def run_backtest(
    conn: duckdb.DuckDBPyConnection,
    *,
    horizon: str,
    exit_policy_id: str,
    selection_rule: str,
    selection_params: Optional[dict[str, Any]] = None,
    policy_params: Optional[dict[str, Any]] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
    dry_run: bool = False,
    force: bool = False,
) -> RunState:
    """Run one execution-backtest configuration end-to-end.

    Returns the populated :class:`RunState` (regardless of ``dry_run``).
    Persists one row to ``crypto_backtest_runs`` and N rows to
    ``crypto_backtest_trades`` inside a single transaction unless
    ``dry_run=True``.

    Args:
        conn: DuckDB connection (must be writable for the schema-create
            step; lifecycle itself only reads existing tables).
        horizon: ``'5d'`` or ``'10d'`` — must match Phase 1A backfill.
        exit_policy_id: ``'A'`` … ``'E'`` (see ``policies.py``).
        selection_rule: ``'top_n'`` or ``'threshold'`` (see ``selection.py``).
        selection_params / policy_params: forwarded to the respective module.
        date_start / date_end: inclusive prediction-date bounds (the
            funding-data floor is applied on top of these).
        dry_run: if True, skip persistence — useful for ad-hoc inspection.
        force: if True, overwrite an existing ``run_id`` (deletes prior
            rows from both tables first). Default raises on collision.
    """
    ensure_backtest_tables(conn)

    selection_params = dict(selection_params or {})
    policy_params = dict(policy_params or {})

    run_id = make_run_id(
        horizon=horizon,
        exit_policy_id=exit_policy_id,
        selection_rule=selection_rule,
        selection_params=selection_params,
        policy_params=policy_params,
        date_start=date_start,
        date_end=date_end,
    )
    state = RunState(
        run_id=run_id,
        horizon=horizon,
        exit_policy_id=exit_policy_id.upper(),
        selection_rule=selection_rule,
        parameters={
            "selection_params": selection_params,
            "policy_params": policy_params,
        },
        date_start=date_start,
        date_end=date_end,
    )

    n_below_floor = count_predictions_below_funding_floor(
        conn, horizon, date_start=date_start, date_end=date_end,
    )
    if n_below_floor > 0:
        logger.info(
            "Date floor: %d predictions excluded (entry_date < %s) "
            "for visibility — funding-rate coverage starts on the floor date.",
            n_below_floor, MIN_FUNDING_DATA_DATE.isoformat(),
        )
    state.n_excluded_by_funding_floor = n_below_floor

    logger.info(
        "run_backtest start: run_id=%s horizon=%s policy=%s selection=%s "
        "selection_params=%s policy_params=%s",
        run_id, horizon, exit_policy_id, selection_rule,
        selection_params, policy_params,
    )

    state = _run_lifecycle(
        conn, state,
        selection_params=selection_params,
        policy_params=policy_params,
    )
    state.n_missing_funding_warnings = get_missing_funding_warnings()

    if not dry_run:
        _persist_run(conn, state, force=force)

    logger.info(
        "run_backtest done : run_id=%s n_predictions_seen=%d n_trades=%d "
        "n_skipped=%d (duplicates=%d, missing_atr=%d) "
        "n_data_gap_exits=%d n_forward_fills=%d "
        "n_excluded_by_funding_floor=%d n_missing_funding_warnings=%d "
        "persisted=%s",
        run_id, state.n_predictions_seen, len(state.closed_trades),
        len(state.skipped), state.n_skipped_duplicates,
        _n_skipped_for_reason(state, "missing_atr"),
        state.n_data_gap_exits, state.n_forward_fills,
        state.n_excluded_by_funding_floor, state.n_missing_funding_warnings,
        not dry_run,
    )
    return state
