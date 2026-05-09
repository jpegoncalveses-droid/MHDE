"""Monitor: the dashboard's prediction tabs serve complete, consistent data.

Two layers of check:

1. **Outcomes parity** (legacy). The `get_outcomes` helper count must
   match a direct `SELECT COUNT(*) FROM candidate_outcomes`. Any
   divergence indicates the dashboard is filtering / joining wrong.

2. **Per-engine × per-horizon column completeness** (added 2026-05-09
   after the equity maturity-date bug). For each prediction tab —
   equity, crypto, FX — the dashboard's query helper is invoked,
   and we assert that the columns the user actually reads are
   populated where they should be:
     - `price_at_prediction` populated for every row.
     - `maturity_date` (or `maturity_datetime` for FX) populated for
       every row. This is the column the May 9 bug made all-NULL on
       pending equity rows.
     - `current_price` populated for every row.
     - `price_at_maturity` populated for filled rows AND NULL for
       pending rows. (Both directions are checked: a pending row with
       a price_at_maturity is just as wrong as a filled row without
       one.)
     - For filled rows: `actual_max_return` (equity/crypto) or
       `actual_max_pips` (FX) populated.
     - `pct_move_str` non-empty/parseable: the format helper, when
       called with the row's data, returns a non-empty string. This
       is checked for every row (pending or filled). "+0.00%" counts
       as parseable; only an empty result counts as a failure.

   Per-horizon granularity: an all-NULL column for the 5d horizon
   while the 20d horizon is fine still trips the alert. Helps
   distinguish "engine fully broken" from "one horizon's data path
   has rotted".

Schedule: every 6 hours.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.dashboard_consistency")


def _check_pct_move_string(row: pd.Series, engine: str) -> bool:
    """Return True if `format_pct_move(...)` would render a non-empty
    string for this row. "+0.00%" counts as non-empty (correct for a
    same-day prediction). Only a truly empty result is a failure."""
    from dashboard.services.maturity import (
        format_pct_move,
        pct_move_equity_or_crypto,
        pct_move_fx,
    )

    outcome_filled = pd.notna(row.get("outcome_filled_at"))
    if engine == "fx":
        value = pct_move_fx(
            row.get("direction"),
            row.get("actual_max_pips"),
            row.get("price_at_prediction"),
            row.get("current_price"),
            outcome_filled,
        )
    else:
        value = pct_move_equity_or_crypto(
            row.get("actual_max_return"),
            row.get("price_at_prediction"),
            row.get("current_price"),
            outcome_filled,
        )
    return format_pct_move(value) != ""


def _check_engine_horizon(
    df: pd.DataFrame, engine: str, horizon: str
) -> list[str]:
    """Return a list of issue strings for one (engine, horizon) slice."""
    issues: list[str] = []
    n = len(df)
    if n == 0:
        return issues

    maturity_col = "maturity_datetime" if engine == "fx" else "maturity_date"
    realized_col = "actual_max_pips" if engine == "fx" else "actual_max_return"

    pending_mask = df["outcome_filled_at"].isna()
    filled_mask = ~pending_mask
    n_pending = int(pending_mask.sum())
    n_filled = int(filled_mask.sum())

    # 1. Always-required columns
    for col in ("price_at_prediction", "current_price", maturity_col):
        if col not in df.columns:
            issues.append(f"{engine}/{horizon}: column '{col}' is missing from query result")
            continue
        n_null = int(df[col].isna().sum())
        if n_null == n:
            issues.append(
                f"{engine}/{horizon}: column '{col}' is all-NULL across {n} rows"
            )
        elif n_null > 0:
            issues.append(
                f"{engine}/{horizon}: column '{col}' has {n_null}/{n} NULL "
                "(expected populated for every row)"
            )

    # 2. price_at_maturity directional check
    if "price_at_maturity" in df.columns:
        wrong_pending = int(df.loc[pending_mask, "price_at_maturity"].notna().sum())
        wrong_filled = int(df.loc[filled_mask, "price_at_maturity"].isna().sum())
        if wrong_pending > 0:
            issues.append(
                f"{engine}/{horizon}: {wrong_pending}/{n_pending} pending rows "
                "have non-NULL price_at_maturity (should be NULL — future row "
                "doesn't exist yet)"
            )
        if wrong_filled > 0:
            issues.append(
                f"{engine}/{horizon}: {wrong_filled}/{n_filled} filled rows "
                "have NULL price_at_maturity (should be populated — JOIN should "
                "have resolved the future row)"
            )

    # 3. Filled rows must carry the realized return / pips column
    if n_filled > 0 and realized_col in df.columns:
        wrong_realized = int(df.loc[filled_mask, realized_col].isna().sum())
        if wrong_realized > 0:
            issues.append(
                f"{engine}/{horizon}: {wrong_realized}/{n_filled} filled rows "
                f"have NULL {realized_col} (fill_outcomes did not write it)"
            )

    # 4. pct_move_str parseability — call the format helper, ensure
    # it would render a non-empty string. "+0.00%" is valid output.
    rendered_empty = sum(
        0 if _check_pct_move_string(row, engine) else 1
        for _, row in df.iterrows()
    )
    if rendered_empty > 0:
        issues.append(
            f"{engine}/{horizon}: pct_move_str renders empty for "
            f"{rendered_empty}/{n} rows (format helper got insufficient inputs)"
        )

    return issues


def _check_engine(conn, engine: str) -> tuple[list[str], dict[str, Any]]:
    """Run column-completeness checks for one engine across all horizons.

    Returns (issues, metrics). metrics carries the per-horizon row
    counts so the alert body can include the breakdown.
    """
    from dashboard.services.queries import (
        get_crypto_predictions,
        get_equity_predictions,
        get_fx_recent_predictions,
    )

    issues: list[str] = []
    metrics: dict[str, Any] = {}

    # An engine with no rows is a separate concern (pipeline_execution
    # monitor's job). dashboard_consistency only judges whether the
    # rows that DO exist render correctly; "no rows" means "no rows to
    # check", not "broken dashboard".
    if engine == "equity":
        latest = conn.execute(
            "SELECT MAX(prediction_date) FROM ml_predictions"
        ).fetchone()[0]
        if latest is None:
            metrics["latest"] = None
            metrics["rows"] = 0
            return [], metrics
        df = get_equity_predictions(conn, latest)
        metrics["latest"] = str(latest)
    elif engine == "crypto":
        latest = conn.execute(
            "SELECT MAX(prediction_date) FROM crypto_ml_predictions"
        ).fetchone()[0]
        if latest is None:
            metrics["latest"] = None
            metrics["rows"] = 0
            return [], metrics
        df = get_crypto_predictions(conn, latest)
        metrics["latest"] = str(latest)
    elif engine == "fx":
        df = get_fx_recent_predictions(conn, limit=30)
        if df.empty:
            metrics["latest"] = None
            metrics["rows"] = 0
            return [], metrics
        metrics["latest"] = str(df["datetime_utc"].max())
    else:
        return [f"unknown engine: {engine}"], metrics

    metrics["rows"] = len(df)
    metrics["horizons"] = sorted(df["horizon"].unique().tolist())

    for horizon in metrics["horizons"]:
        sub = df[df["horizon"] == horizon]
        issues.extend(_check_engine_horizon(sub, engine, horizon))

    return issues, metrics


def run(conn=None) -> MonitorResult:
    """Run the dashboard-consistency check.

    `conn` is an open DuckDB read-only connection; if None, opens one
    from the configured engine path.
    """
    started = datetime.now(timezone.utc)

    close_conn = False
    if conn is None:
        from storage.config import load_engine_config
        import duckdb
        cfg = load_engine_config()
        conn = duckdb.connect(cfg["db_path"], read_only=True)
        close_conn = True

    try:
        problems: list[str] = []
        metrics: dict[str, Any] = {}

        # Layer 1: outcomes count parity (legacy).
        try:
            from dashboard.services.queries import get_outcomes
            dashboard_rows = get_outcomes(conn, limit=200)
            dashboard_count = len(dashboard_rows)
            direct = conn.execute("SELECT COUNT(*) FROM candidate_outcomes").fetchone()
            direct_count = min(direct[0], 200) if direct else 0
            if dashboard_count != direct_count:
                problems.append(
                    f"outcomes: dashboard={dashboard_count}, db={direct_count}"
                )
            metrics["outcomes_dashboard"] = dashboard_count
            metrics["outcomes_direct"] = direct_count
        except Exception as exc:
            problems.append(f"outcomes parity check raised: {exc}")

        # Layer 2: per-engine × per-horizon column completeness.
        for engine in ("equity", "crypto", "fx"):
            try:
                issues, m = _check_engine(conn, engine)
            except Exception as exc:
                problems.append(f"{engine}: column-check raised: {exc}")
                continue
            problems.extend(issues)
            metrics[engine] = m

        finished = datetime.now(timezone.utc)
        if problems:
            return MonitorResult(
                monitor="dashboard_consistency",
                status="fail",
                severity="warn",
                title="Dashboard ↔ DB mismatch detected",
                body="\n".join(f"- {m}" for m in problems),
                metrics=metrics,
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="dashboard_consistency",
            status="ok",
            severity="info",
            title="dashboard ↔ DB consistent (outcomes + 3-engine column completeness)",
            metrics=metrics,
            started_at=started, finished_at=finished,
        )
    finally:
        if close_conn:
            conn.close()


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
