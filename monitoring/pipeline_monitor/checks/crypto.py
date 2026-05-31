"""Outcome-based checks for the crypto prediction → engine-entry pipeline.

Each ``check_*`` reads the database / files directly and returns a
:class:`~monitoring.pipeline_monitor.core.StepResult`. Nothing here looks at
a script's exit code: the 2026-05-11/12 regression (KI-138) had every step
exit 0 while ``predictions_latest.json`` was stale and the engine placed no
positions — only an outcome check catches that.

Conventions (the daily monitor fires ~06:40 UTC, after the 00:30 crypto
predict chain and the 06:15 prediction export and the 06:30 engine entry):

* OHLCV / features / predictions are produced for ``today - 1`` because the
  ingestion fix (ADR-022) only freezes fully-closed UTC days, so
  ``MAX(trade_date)`` in ``crypto_prices_daily`` / ``crypto_ml_features`` and
  ``prediction_date`` in ``crypto_ml_predictions`` are all structurally
  ``today - 1`` (KI-138).
* The prediction export's ``export_date`` IS ``today`` (the trading date the
  engine validates against — INTERFACE.md §3.1) and ``features_as_of_date``
  is ``today - 1``.
* The engine DuckDB is read **read-only** — a deliberate, scoped exception to
  INTERFACE.md's "no cross-system DB access" (ADR-020, same as the
  paper-trading-drift monitor).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

from monitoring.pipeline_monitor.core import Status, StepResult

# ── default paths ─────────────────────────────────────────────────────
DEFAULT_EXPORTS_DIR = Path("data/exports")
DEFAULT_SPEC_PATH = Path("data/exports/active_spec.json")
DEFAULT_ENGINE_DB = "/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb"
ENGINE_DB_ENV = "CRYPTO_ENGINE_DB_PATH"

# ── step display names (imported by the daily runner) ─────────────────
OHLCV_INGESTION = "OHLCV ingestion (crypto_prices_daily)"
DATA_QUALITY_GUARD = "Data-quality guard (no systemic OHLCV corruption)"
FUNDING_OI_INGESTION = "Funding / OI ingestion"
FEATURE_PIPELINE = "Feature pipeline (crypto_ml_features)"
MODEL_PREDICTIONS = "Model predictions (crypto_ml_predictions)"
OUTCOME_TAGGING = "Outcome tagging (actual_hit backfill)"
EXPORT_PREDICTIONS = "Export predictions (predictions_latest.json)"
ENGINE_INGEST = "Engine ingest (entry run today)"
ENGINE_POSITIONS = "Engine entry / positions placed"

#: how many extra days past a prediction's forward window before we expect
#: ``fill_outcomes`` to have tagged it (absorbs the close-availability lag
#: under cap-at-today-1 plus any pipeline retry).
OUTCOME_SETTLE_MARGIN_DAYS = 2

_HORIZON_DAYS_CASE = (
    "CASE p.horizon WHEN '1d' THEN 1 WHEN '3d' THEN 3 WHEN '5d' THEN 5 "
    "WHEN '10d' THEN 10 WHEN '20d' THEN 20 ELSE 9999 END"
)

# states in the engine `positions` table that mean "not an open position"
_CLOSED_POSITION_STATES = ("exit_filled", "failed", "closed", "exited", "cancelled")

# states that mean the entry never actually opened a position today. Distinct
# from _CLOSED_POSITION_STATES on purpose: that set includes ``exit_filled``,
# which DID open (then closed) and must still count as "opened today". A row
# only fails to count as opened if it was rejected/cancelled at the venue
# (``failed``/``cancelled``) or is still mid-flight (``candidate``/``entry_pending``).
_NEVER_OPENED_STATES = ("failed", "cancelled", "candidate", "entry_pending")

# subset of the above where the entry was rejected/cancelled at the venue —
# surfaced inline (with the binance reject code) but kept GREEN: a venue reject
# is not a pipeline failure and these recur ~weekly on testnet (cf KI-128).
_FAILED_ENTRY_STATES = ("failed", "cancelled")


def _utc_date(now: datetime):
    return now.date()


def open_engine_db(path: Optional[str] = None):
    """Open the crypto-trading-engine DuckDB read-only. Raises on failure."""
    import duckdb

    return duckdb.connect(path or os.environ.get(ENGINE_DB_ENV, DEFAULT_ENGINE_DB), read_only=True)


# ──────────────────────────────────────────────────────────────────────
# 1. OHLCV ingestion
# ──────────────────────────────────────────────────────────────────────
def check_ohlcv_ingestion(conn, now: datetime) -> StepResult:
    today = _utc_date(now)
    expected_min = today - timedelta(days=1)  # cap-at-today-1 (ADR-022)
    latest = conn.execute("SELECT MAX(trade_date) FROM crypto_prices_daily").fetchone()[0]
    if latest is None:
        return StepResult(OHLCV_INGESTION, Status.RED, "crypto_prices_daily is empty")
    n = conn.execute(
        "SELECT COUNT(*) FROM crypto_prices_daily WHERE trade_date = ?", [latest]
    ).fetchone()[0]
    if latest >= expected_min:
        return StepResult(OHLCV_INGESTION, Status.GREEN, f"MAX(trade_date)={latest}, {n} symbols")
    return StepResult(
        OHLCV_INGESTION, Status.RED,
        f"MAX(trade_date)={latest} — expected >= {expected_min} (today-1); ingestion did not advance",
    )


# ──────────────────────────────────────────────────────────────────────
# 2. Data-quality guard
# ──────────────────────────────────────────────────────────────────────
def check_data_quality_guard(conn, now: datetime) -> StepResult:
    today = _utc_date(now)
    floor = today - timedelta(days=2)  # target_date is the evaluated trade_date (today-1 under cap)
    systemic = conn.execute(
        "SELECT date FROM crypto_data_quality_reports "
        "WHERE check_name = 'systemic_corruption' AND date >= ? ORDER BY date DESC LIMIT 1",
        [floor],
    ).fetchone()
    if systemic is not None:
        return StepResult(
            DATA_QUALITY_GUARD, Status.RED,
            f"SYSTEMIC OHLCV corruption flagged for {systemic[0]} — the daily crypto pipeline was blocked",
        )
    n_warn = conn.execute(
        "SELECT COUNT(*) FROM crypto_data_quality_reports "
        "WHERE check_name <> 'systemic_corruption' AND date >= ?",
        [floor],
    ).fetchone()[0]
    note = "clean — no systemic corruption flag"
    if n_warn:
        note += f" ({n_warn} per-symbol warning(s) in last 2d, non-blocking)"
    return StepResult(DATA_QUALITY_GUARD, Status.GREEN, note)


# ──────────────────────────────────────────────────────────────────────
# 3. Funding / OI ingestion
# ──────────────────────────────────────────────────────────────────────
def check_funding_oi_ingestion(conn, now: datetime) -> StepResult:
    today = _utc_date(now)
    expected_min = today - timedelta(days=1)
    fr = conn.execute("SELECT MAX(funding_time) FROM crypto_funding_rates").fetchone()[0]
    oi = conn.execute("SELECT MAX(trade_date) FROM crypto_open_interest").fetchone()[0]
    fr_date = fr.date() if isinstance(fr, datetime) else fr
    problems: list[str] = []
    if fr_date is None:
        problems.append("crypto_funding_rates is empty")
    elif fr_date < expected_min:
        problems.append(f"funding stale: MAX(funding_time)={fr} (date < {expected_min})")
    if oi is None:
        problems.append("crypto_open_interest is empty")
    elif oi < expected_min:
        problems.append(f"OI stale: MAX(trade_date)={oi} (< {expected_min})")
    if problems:
        return StepResult(FUNDING_OI_INGESTION, Status.RED, "; ".join(problems))
    return StepResult(FUNDING_OI_INGESTION, Status.GREEN, f"funding @ {fr}, OI @ {oi}")


# ──────────────────────────────────────────────────────────────────────
# 4. Feature pipeline
# ──────────────────────────────────────────────────────────────────────
def check_feature_pipeline(conn, now: datetime) -> StepResult:
    today = _utc_date(now)
    expected = today - timedelta(days=1)  # features-as-of date under cap-at-today-1 (KI-138)
    latest = conn.execute("SELECT MAX(trade_date) FROM crypto_ml_features").fetchone()[0]
    if latest is None:
        return StepResult(FEATURE_PIPELINE, Status.RED, "crypto_ml_features is empty")
    n = conn.execute(
        "SELECT COUNT(*) FROM crypto_ml_features WHERE trade_date = ?", [latest]
    ).fetchone()[0]
    if latest >= expected and n > 0:
        return StepResult(FEATURE_PIPELINE, Status.GREEN, f"{n} symbols @ trade_date={latest}")
    return StepResult(
        FEATURE_PIPELINE, Status.RED,
        f"MAX(trade_date)={latest} ({n} rows) — expected features for {expected} (today-1)",
    )


# ──────────────────────────────────────────────────────────────────────
# 5. Model predictions
# ──────────────────────────────────────────────────────────────────────
def check_model_predictions(conn, now: datetime) -> StepResult:
    today = _utc_date(now)
    expected = today - timedelta(days=1)  # prediction_date = MAX(trade_date) = today-1 (KI-138)
    n_active = conn.execute(
        "SELECT COUNT(*) FROM crypto_ml_model_runs WHERE is_active = TRUE"
    ).fetchone()[0]
    if n_active == 0:
        return StepResult(MODEL_PREDICTIONS, Status.RED, "no active model in crypto_ml_model_runs")
    latest = conn.execute(
        "SELECT MAX(p.prediction_date) FROM crypto_ml_predictions p "
        "JOIN crypto_ml_model_runs m ON p.model_id = m.model_id WHERE m.is_active = TRUE"
    ).fetchone()[0]
    if latest is None:
        return StepResult(MODEL_PREDICTIONS, Status.RED, "no crypto predictions written by an active model")
    n = conn.execute(
        "SELECT COUNT(*) FROM crypto_ml_predictions p "
        "JOIN crypto_ml_model_runs m ON p.model_id = m.model_id "
        "WHERE m.is_active = TRUE AND p.prediction_date = ?",
        [latest],
    ).fetchone()[0]
    if latest >= expected and n > 0:
        return StepResult(MODEL_PREDICTIONS, Status.GREEN, f"{n} predictions @ prediction_date={latest}")
    return StepResult(
        MODEL_PREDICTIONS, Status.RED,
        f"latest active-model prediction_date={latest} ({n} rows) — expected {expected} (today-1)",
    )


# ──────────────────────────────────────────────────────────────────────
# 6. Outcome tagging
# ──────────────────────────────────────────────────────────────────────
def check_outcome_tagging(conn, now: datetime) -> StepResult:
    today = _utc_date(now)
    backlog = conn.execute(
        "SELECT COUNT(*) FROM crypto_ml_predictions p "
        "JOIN crypto_ml_model_runs m ON p.model_id = m.model_id "
        "WHERE m.is_active = TRUE AND p.actual_hit IS NULL "
        f"  AND p.prediction_date + {_HORIZON_DAYS_CASE} + {OUTCOME_SETTLE_MARGIN_DAYS} <= ?",
        [today],
    ).fetchone()[0]
    last_fill = conn.execute(
        "SELECT MAX(outcome_filled_at) FROM crypto_ml_predictions"
    ).fetchone()[0]
    if backlog == 0:
        detail = "all matured predictions tagged"
        if last_fill is not None:
            detail += f" (last fill {last_fill:%Y-%m-%d %H:%M} UTC)"
        return StepResult(OUTCOME_TAGGING, Status.GREEN, detail)
    return StepResult(
        OUTCOME_TAGGING, Status.RED,
        f"{backlog} matured prediction(s) still have actual_hit NULL — fill_outcomes is behind",
    )


# ──────────────────────────────────────────────────────────────────────
# 7. Export predictions
# ──────────────────────────────────────────────────────────────────────
def check_export_predictions(now: datetime, exports_dir: Optional[Path] = None) -> StepResult:
    today = _utc_date(now)
    exports_dir = Path(exports_dir) if exports_dir else DEFAULT_EXPORTS_DIR
    latest = exports_dir / "predictions_latest.json"
    if not latest.exists():
        return StepResult(EXPORT_PREDICTIONS, Status.RED, f"{latest} does not exist")
    try:
        data = json.loads(latest.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return StepResult(EXPORT_PREDICTIONS, Status.RED, f"could not read {latest}: {exc}")

    try:
        target = os.path.basename(os.readlink(latest))
    except OSError:
        target = latest.name

    export_date = data.get("export_date")
    preds = data.get("predictions") or []
    n_preds = data.get("n_predictions")
    if n_preds is None:
        n_preds = len(preds)

    if export_date != today.isoformat():
        return StepResult(
            EXPORT_PREDICTIONS, Status.RED,
            f"{target}: export_date={export_date!r} — expected {today.isoformat()}; "
            "predictions export is stale (engine will reject it)",
        )
    if not n_preds:
        return StepResult(EXPORT_PREDICTIONS, Status.RED, f"{target}: export_date OK but 0 predictions")
    feats = data.get("features_as_of_date")
    return StepResult(
        EXPORT_PREDICTIONS, Status.GREEN,
        f"{target}: export_date={export_date}, features_as_of={feats}, {n_preds} predictions",
    )


# ──────────────────────────────────────────────────────────────────────
# 8. Engine ingest — entry phase ran today
# ──────────────────────────────────────────────────────────────────────
def check_engine_ingest(engine_conn, now: datetime) -> StepResult:
    if engine_conn is None:
        return StepResult(ENGINE_INGEST, Status.RED, "engine DuckDB not reachable")
    today = _utc_date(now)
    midnight = datetime.combine(today, time.min)
    row = engine_conn.execute(
        "SELECT started_at, success, error_message FROM engine_runs "
        "WHERE phase = 'entry' AND started_at >= ? ORDER BY started_at DESC LIMIT 1",
        [midnight],
    ).fetchone()
    if row is None:
        return StepResult(ENGINE_INGEST, Status.RED, f"engine ran no 'entry' phase today ({today})")
    started_at, success, err = row
    if not success:
        return StepResult(
            ENGINE_INGEST, Status.RED,
            f"engine 'entry' run at {started_at:%H:%M} UTC failed: {err or '(no error message)'}",
        )
    return StepResult(
        ENGINE_INGEST, Status.GREEN,
        f"engine 'entry' phase ran today at {started_at:%H:%M} UTC (success)",
    )


# ──────────────────────────────────────────────────────────────────────
# 9. Engine entry / positions placed
# ──────────────────────────────────────────────────────────────────────
def _read_max_concurrent(spec_path: Optional[Path]) -> Optional[int]:
    spec_path = Path(spec_path) if spec_path else DEFAULT_SPEC_PATH
    try:
        spec = json.loads(spec_path.read_text())
        return int(spec["sizing"]["max_concurrent"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _failed_entries_today(engine_conn, today) -> list[tuple[str, Optional[int]]]:
    """Return ``[(symbol, reject_code_or_None), ...]`` for entries that were
    rejected/cancelled at the venue today, newest reject code per position.

    Reads the engine ``events`` table read-only (ADR-020) for the latest
    ``order_cancelled`` payload code. Falls back to symbol-only (code ``None``)
    if the events lookup is not cheaply available (missing table / unparsable
    payload); the join is left-outer so a failed row with no event still lists.
    """
    placeholders = ",".join("?" * len(_FAILED_ENTRY_STATES))
    try:
        rows = engine_conn.execute(
            "SELECT p.symbol, "
            "  CAST(json_extract(e.payload, '$.code') AS BIGINT) AS code "
            "FROM positions p "
            "LEFT JOIN LATERAL ("
            "  SELECT payload FROM events ev "
            "  WHERE ev.position_id = p.id AND ev.event_type = 'order_cancelled' "
            "  ORDER BY ev.timestamp DESC LIMIT 1"
            ") e ON TRUE "
            f"WHERE p.entry_date = ? AND lower(p.current_state) IN ({placeholders}) "
            "ORDER BY p.symbol",
            [today, *_FAILED_ENTRY_STATES],
        ).fetchall()
        return [(sym, int(code) if code is not None else None) for sym, code in rows]
    except Exception:
        # events table absent or payload not extractable — degrade to symbol-only
        rows = engine_conn.execute(
            "SELECT symbol FROM positions "
            f"WHERE entry_date = ? AND lower(current_state) IN ({placeholders}) "
            "ORDER BY symbol",
            [today, *_FAILED_ENTRY_STATES],
        ).fetchall()
        return [(sym, None) for (sym,) in rows]


def _render_failures(failures: list[tuple[str, Optional[int]]]) -> str:
    return ", ".join(
        f"{sym} ({code})" if code is not None else sym for sym, code in failures
    )


def check_engine_positions(engine_conn, now: datetime, spec_path: Optional[Path] = None) -> StepResult:
    if engine_conn is None:
        return StepResult(ENGINE_POSITIONS, Status.RED, "engine DuckDB not reachable")
    today = _utc_date(now)
    opened_placeholders = ",".join("?" * len(_NEVER_OPENED_STATES))
    n_opened = engine_conn.execute(
        f"SELECT COUNT(*) FROM positions "
        f"WHERE entry_date = ? AND lower(current_state) NOT IN ({opened_placeholders})",
        [today, *_NEVER_OPENED_STATES],
    ).fetchone()[0]
    if n_opened > 0:
        failures = _failed_entries_today(engine_conn, today)
        if failures:
            return StepResult(
                ENGINE_POSITIONS, Status.GREEN,
                f"{n_opened} opened, {len(failures)} failed today ({today}): "
                f"{_render_failures(failures)}",
            )
        return StepResult(ENGINE_POSITIONS, Status.GREEN, f"{n_opened} position(s) opened today ({today})")

    placeholders = ",".join("?" * len(_CLOSED_POSITION_STATES))
    open_now = engine_conn.execute(
        f"SELECT COUNT(*) FROM positions WHERE lower(current_state) NOT IN ({placeholders})",
        list(_CLOSED_POSITION_STATES),
    ).fetchone()[0]
    max_concurrent = _read_max_concurrent(spec_path)
    if max_concurrent is not None and open_now >= max_concurrent:
        return StepResult(
            ENGINE_POSITIONS, Status.GREEN,
            f"0 new entries today — book already at max_concurrent ({open_now}/{max_concurrent} open)",
        )
    cap = f"/{max_concurrent}" if max_concurrent is not None else ""
    return StepResult(
        ENGINE_POSITIONS, Status.RED,
        f"0 positions opened today and only {open_now}{cap} open — "
        "check the engine entry log (predictions file rejected? all top-N filtered?)",
    )
