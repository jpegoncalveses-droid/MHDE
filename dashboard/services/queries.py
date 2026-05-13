from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd


def _connect() -> duckdb.DuckDBPyConnection:
    db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
    return duckdb.connect(db_path, read_only=True)


def get_distinct_prediction_dates(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    date_col: str,
    limit: int = 30,
) -> list:
    """Return the most-recent N distinct prediction dates from ``table``.

    A ``SELECT DISTINCT col FROM t ORDER BY col DESC LIMIT N`` shape
    triggers a TopN-with-distinct planner regression in DuckDB 1.5.2 that
    silently returns far fewer rows than the table contains. Using
    ``GROUP BY`` instead avoids the fusion. See regression test
    ``tests/dashboard/test_distinct_date_selector_regression.py`` and
    KNOWN_ISSUES.md (KI-119 note).
    """
    sql = (
        f"SELECT {date_col} FROM {table} "
        f"GROUP BY {date_col} "
        f"ORDER BY {date_col} DESC "
        f"LIMIT ?"
    )
    rows = conn.execute(sql, [limit]).fetchall()
    return [r[0] for r in rows]


def get_latest_run_id(conn: duckdb.DuckDBPyConnection) -> str | None:
    rows = conn.execute(
        "SELECT run_id FROM scores ORDER BY created_at DESC LIMIT 1"
    ).fetchall()
    return rows[0][0] if rows else None


def get_overview_stats(conn: duckdb.DuckDBPyConnection) -> dict:
    run_id = get_latest_run_id(conn)
    universe = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE is_active = true"
    ).fetchone()[0]

    candidates_scored = 0
    tier_counts: dict[str, int] = {}
    if run_id:
        candidates_scored = conn.execute(
            "SELECT COUNT(*) FROM scores WHERE run_id = ?", [run_id]
        ).fetchone()[0]
        tiers = conn.execute(
            "SELECT tier, COUNT(*) FROM scores WHERE run_id = ? GROUP BY tier", [run_id]
        ).fetchall()
        tier_counts = dict(tiers)

    source_fails = conn.execute(
        "SELECT COUNT(*) FROM source_runs WHERE status = 'error'"
    ).fetchone()[0]

    alerts_sent = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE status = 'sent'"
    ).fetchone()[0]

    health_warns = conn.execute(
        "SELECT COUNT(*) FROM health_checks WHERE status IN ('warn', 'fail')"
    ).fetchone()[0]

    feature_coverage = conn.execute(
        """
        SELECT
            COUNT(CASE WHEN feature_value IS NOT NULL THEN 1 END) * 100.0 / COUNT(*)
        FROM features
        WHERE run_id = ?
        """,
        [run_id or ""],
    ).fetchone()[0] if run_id else None

    return {
        "run_id": run_id,
        "universe_size": universe,
        "candidates_scored": candidates_scored,
        "tier_a": tier_counts.get("A", 0),
        "tier_b": tier_counts.get("B", 0),
        "tier_c": tier_counts.get("C", 0),
        "rejected": tier_counts.get("Reject", 0),
        "source_failures": source_fails,
        "alerts_sent": alerts_sent,
        "health_warnings": health_warns,
        "feature_coverage_pct": feature_coverage,
    }


def get_candidates(
    conn: duckdb.DuckDBPyConnection,
    run_id: str | None = None,
    tier: str | None = None,
    min_score: float = 0,
    max_score: float = 100,
    search: str | None = None,
) -> list[dict]:
    if not run_id:
        run_id = get_latest_run_id(conn)
    if not run_id:
        return []

    query = """
        SELECT s.ticker, c.company_name, s.tier, s.total_score, s.cheap_score,
               s.quality_score, s.catalyst_score, s.momentum_score, s.sentiment_score,
               s.risk_penalty, s.confidence, s.why_ranked, s.missing_data_json, s.run_id,
               s.as_of_date
        FROM scores s
        LEFT JOIN companies c ON s.ticker = c.ticker
        WHERE s.run_id = ?
          AND s.total_score >= ? AND s.total_score <= ?
    """
    params: list = [run_id, min_score, max_score]

    if tier:
        query += " AND s.tier = ?"
        params.append(tier)
    if search:
        query += " AND (s.ticker ILIKE ? OR c.company_name ILIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY s.total_score DESC"
    rows = conn.execute(query, params).fetchall()
    cols = [
        "ticker", "company_name", "tier", "total_score", "cheap_score",
        "quality_score", "catalyst_score", "momentum_score", "sentiment_score",
        "risk_penalty", "confidence", "why_ranked", "missing_data_json", "run_id", "as_of_date",
    ]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("missing_data_json"):
            try:
                d["missing_data"] = json.loads(d["missing_data_json"])
            except Exception:
                d["missing_data"] = []
        result.append(d)
    return result


def get_candidate_detail(
    conn: duckdb.DuckDBPyConnection, ticker: str, run_id: str | None = None
) -> dict:
    if not run_id:
        run_id = get_latest_run_id(conn)

    score_row = conn.execute(
        """
        SELECT s.*, c.company_name, c.sector, c.industry, c.cik
        FROM scores s LEFT JOIN companies c ON s.ticker = c.ticker
        WHERE s.ticker = ? AND s.run_id = ?
        """,
        [ticker, run_id],
    ).fetchone()

    hyp = conn.execute(
        """
        SELECT thesis, why_now, cheap_evidence_json, quality_evidence_json,
               catalyst_evidence_json, risks_json, missing_evidence_json, status
        FROM hypotheses WHERE ticker = ? AND run_id = ?
        """,
        [ticker, run_id],
    ).fetchone()

    llm = conn.execute(
        """
        SELECT output_json, provider, model, status, error_message
        FROM llm_runs WHERE ticker = ? AND run_id = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        [ticker, run_id],
    ).fetchone()

    features_rows = conn.execute(
        """
        SELECT feature_group, feature_name, feature_value, feature_score, confidence
        FROM features WHERE ticker = ? AND run_id = ?
        ORDER BY feature_group, feature_name
        """,
        [ticker, run_id],
    ).fetchall()

    prices = conn.execute(
        """
        SELECT trade_date, close, volume FROM prices_daily
        WHERE ticker = ? ORDER BY trade_date DESC LIMIT 90
        """,
        [ticker],
    ).fetchall()

    outcome = conn.execute(
        """
        SELECT forward_return_20d, forward_return_60d, max_drawdown_20d,
               max_runup_20d, review_status, review_notes
        FROM candidate_outcomes WHERE ticker = ? AND run_id = ?
        """,
        [ticker, run_id],
    ).fetchone()

    def parse_json(s):
        if not s:
            return []
        try:
            return json.loads(s)
        except Exception:
            return []

    detail: dict = {}
    if score_row:
        cols = [d[0] for d in conn.description]
        detail.update(dict(zip(cols, score_row)))

    if hyp:
        detail["thesis"] = hyp[0]
        detail["why_now"] = hyp[1]
        detail["cheap_evidence"] = parse_json(hyp[2])
        detail["quality_evidence"] = parse_json(hyp[3])
        detail["catalyst_evidence"] = parse_json(hyp[4])
        detail["risks"] = parse_json(hyp[5])
        detail["missing_evidence"] = parse_json(hyp[6])
        detail["hypothesis_status"] = hyp[7]

    if llm:
        try:
            llm_data = json.loads(llm[0]) if llm[0] else {}
        except Exception:
            llm_data = {}
        detail["llm_thesis"] = llm_data.get("thesis", "")
        detail["llm_confidence"] = llm_data.get("confidence", "")
        detail["llm_action"] = llm_data.get("recommended_action", "")
        detail["llm_provider"] = llm[1]
        detail["llm_model"] = llm[2]
        detail["llm_status"] = llm[3]
        detail["llm_error"] = llm[4]

    detail["features"] = [
        {"group": r[0], "name": r[1], "value": r[2], "score": r[3], "confidence": r[4]}
        for r in features_rows
    ]
    detail["prices"] = [
        {"date": r[0], "close": r[1], "volume": r[2]} for r in reversed(prices)
    ]

    if outcome:
        detail["outcome"] = {
            "forward_return_20d": outcome[0],
            "forward_return_60d": outcome[1],
            "max_drawdown_20d": outcome[2],
            "max_runup_20d": outcome[3],
            "review_status": outcome[4],
            "review_notes": outcome[5],
        }

    return detail


def get_source_health(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT source_name, status, COUNT(*) as runs,
               SUM(records_inserted) as total_inserted,
               MAX(finished_at) as last_run,
               COUNT(CASE WHEN status = 'error' THEN 1 END) as errors
        FROM source_runs
        GROUP BY source_name, status
        ORDER BY source_name, status
        """
    ).fetchall()
    cols = ["source_name", "status", "runs", "total_inserted", "last_run", "errors"]
    return [dict(zip(cols, r)) for r in rows]


def get_llm_runs(conn: duckdb.DuckDBPyConnection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT llm_run_id, ticker, provider, model, job_type, prompt_version,
               estimated_tokens, estimated_cost, status, error_message, created_at
        FROM llm_runs ORDER BY created_at DESC LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "llm_run_id", "ticker", "provider", "model", "job_type", "prompt_version",
        "estimated_tokens", "estimated_cost", "status", "error_message", "created_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_outcomes(conn: duckdb.DuckDBPyConnection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT candidate_id, ticker, as_of_date, tier, total_score, reference_price,
               forward_return_20d, forward_return_60d, max_drawdown_20d, max_runup_20d,
               hit_10pct_before_down_10pct, review_status, review_notes
        FROM candidate_outcomes
        ORDER BY as_of_date DESC, total_score DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "candidate_id", "ticker", "as_of_date", "tier", "total_score", "reference_price",
        "forward_return_20d", "forward_return_60d", "max_drawdown_20d", "max_runup_20d",
        "hit_10pct_before_down_10pct", "review_status", "review_notes",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_health_checks(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT check_name, status, severity, message, created_at
        FROM health_checks ORDER BY created_at DESC LIMIT 50
        """
    ).fetchall()
    cols = ["check_name", "status", "severity", "message", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def get_backtest_runs(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT backtest_run_id, as_of_date, tickers_tested, hit_rate, avg_return,
               warning, status, created_at
        FROM backtest_runs ORDER BY created_at DESC LIMIT 20
        """
    ).fetchall()
    cols = [
        "backtest_run_id", "as_of_date", "tickers_tested", "hit_rate",
        "avg_return", "warning", "status", "created_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_alerts(conn: duckdb.DuckDBPyConnection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT alert_id, ticker, channel, alert_type, status, message, sent_at, error_message
        FROM alerts ORDER BY created_at DESC LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = ["alert_id", "ticker", "channel", "alert_type", "status", "message", "sent_at", "error_message"]
    return [dict(zip(cols, r)) for r in rows]


def get_hypotheses(conn: duckdb.DuckDBPyConnection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT hypothesis_id, ticker, company_name, tier, total_score, thesis,
               why_now, NULL::DOUBLE AS confidence, status, review_status, created_at
        FROM hypotheses
        ORDER BY created_at DESC, total_score DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "hypothesis_id", "ticker", "company_name", "tier", "total_score",
        "thesis", "why_now", "confidence", "status", "review_status", "created_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_candidate_reviews(conn: duckdb.DuckDBPyConnection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT review_id, candidate_id, run_id, ticker, review_status,
               usefulness_score, thesis_quality_score, evidence_quality_score,
               false_positive_reason, missed_risk, missing_evidence,
               review_notes, reviewed_by, created_at, updated_at
        FROM candidate_reviews
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "review_id", "candidate_id", "run_id", "ticker", "review_status",
        "usefulness_score", "thesis_quality_score", "evidence_quality_score",
        "false_positive_reason", "missed_risk", "missing_evidence",
        "review_notes", "reviewed_by", "created_at", "updated_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_scorecard_experiments(conn: duckdb.DuckDBPyConnection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT experiment_id, based_on_run_ids, hypothesis, proposed_change_json,
               affected_components_json, expected_effect, status,
               review_notes, approved_by, applied_at, created_at
        FROM scorecard_experiments
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "experiment_id", "based_on_run_ids", "hypothesis", "proposed_change_json",
        "affected_components_json", "expected_effect", "status",
        "review_notes", "approved_by", "applied_at", "created_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# Prediction tables joined with prices for the price/maturity/move columns
# rendered on the equity / crypto / FX prediction tabs.
#
# Maturity calculation MUST mirror the corresponding `fill_outcomes` logic:
#   - Equity: trading rows forward (ROW_NUMBER on prices_daily). See
#     ml/predict.py::fill_outcomes — the N-th row after entry.
#   - Crypto: calendar days forward (prediction_date + INTERVAL N days).
#     See crypto/ml/predict.py::fill_outcomes.
#   - FX:     calendar hours forward (datetime_utc + INTERVAL N hours).
#     See fx/ml/predict.py::fill_outcomes.
# ─────────────────────────────────────────────────────────────────────────

_EQUITY_HORIZON_DAYS = "WHEN '5d' THEN 5 WHEN '10d' THEN 10 WHEN '20d' THEN 20 ELSE 20"
_CRYPTO_HORIZON_INTERVAL = (
    "WHEN '1d' THEN INTERVAL '1 day' "
    "WHEN '3d' THEN INTERVAL '3 days' "
    "WHEN '5d' THEN INTERVAL '5 days' "
    "WHEN '10d' THEN INTERVAL '10 days' "
    "ELSE INTERVAL '20 days'"
)
_FX_HORIZON_INTERVAL = (
    "WHEN '24h' THEN INTERVAL '24 hours' "
    "WHEN '48h' THEN INTERVAL '48 hours'"
)


def _fill_estimated_equity_maturity(
    df: pd.DataFrame, prediction_date_value=None
) -> pd.DataFrame:
    """Fill missing ``maturity_date`` values with the busday-offset
    estimate for pending equity predictions.

    The trading-rows-forward JOIN populates ``maturity_date`` for
    matured predictions only — pending rows have no future row in
    ``prices_daily`` yet, so the JOIN yields NULL for both the
    maturity date and the price. We leave ``price_at_maturity`` NULL
    for pending (the price doesn't exist yet) but fill the date with
    a calendar estimate so the dashboard's ``time_remaining_str``
    column renders.

    ``prediction_date_value`` is used for ``get_equity_predictions``
    where every row shares the same prediction_date (it isn't returned
    in the SELECT). For ``get_equity_recent_outcomes`` we fall back to
    the per-row ``prediction_date`` column.
    """
    from dashboard.services.maturity import estimate_equity_maturity_date

    if df.empty or "maturity_date" not in df.columns or "horizon" not in df.columns:
        return df

    needs_estimate = df["maturity_date"].isna()
    if not needs_estimate.any():
        return df

    def _row_estimate(row):
        pd_val = (
            prediction_date_value
            if prediction_date_value is not None
            else row.get("prediction_date")
        )
        return estimate_equity_maturity_date(pd_val, row.get("horizon"))

    df.loc[needs_estimate, "maturity_date"] = df.loc[needs_estimate].apply(
        _row_estimate, axis=1
    )
    return df


def get_equity_predictions(
    conn: duckdb.DuckDBPyConnection, prediction_date
) -> pd.DataFrame:
    sql = f"""
        WITH ranked AS (
            SELECT ticker, trade_date, close,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
            FROM prices_daily
            WHERE close IS NOT NULL
        ),
        latest AS (
            SELECT ticker, MAX(trade_date) AS latest_date
            FROM prices_daily WHERE close IS NOT NULL GROUP BY ticker
        )
        SELECT p.ticker, p.horizon, p.predicted_probability, p.prediction_threshold,
               p.sector, p.market_cap_bucket,
               entry.close AS price_at_prediction,
               mat.trade_date AS maturity_date,
               mat.close AS price_at_maturity,
               cur.close AS current_price,
               p.actual_max_return, p.actual_max_drawdown, p.actual_hit,
               p.outcome_filled_at
        FROM ml_predictions p
        LEFT JOIN ranked entry
          ON entry.ticker = p.ticker AND entry.trade_date = p.prediction_date
        LEFT JOIN ranked mat
          ON mat.ticker = p.ticker
         AND mat.rn = entry.rn + CASE p.horizon {_EQUITY_HORIZON_DAYS} END
        LEFT JOIN latest lp ON lp.ticker = p.ticker
        LEFT JOIN prices_daily cur
          ON cur.ticker = lp.ticker AND cur.trade_date = lp.latest_date
        WHERE p.prediction_date = ?
        ORDER BY p.horizon, p.predicted_probability DESC
    """
    df = conn.execute(sql, [prediction_date]).fetchdf()
    return _fill_estimated_equity_maturity(df, prediction_date_value=prediction_date)


def get_equity_recent_outcomes(
    conn: duckdb.DuckDBPyConnection, limit: int = 50
) -> pd.DataFrame:
    sql = f"""
        WITH ranked AS (
            SELECT ticker, trade_date, close,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
            FROM prices_daily
            WHERE close IS NOT NULL
        )
        SELECT p.ticker, p.prediction_date, p.horizon, p.predicted_probability,
               entry.close AS price_at_prediction,
               mat.trade_date AS maturity_date,
               mat.close AS price_at_maturity,
               p.actual_max_return, p.actual_max_drawdown, p.actual_hit,
               p.outcome_filled_at
        FROM ml_predictions p
        LEFT JOIN ranked entry
          ON entry.ticker = p.ticker AND entry.trade_date = p.prediction_date
        LEFT JOIN ranked mat
          ON mat.ticker = p.ticker
         AND mat.rn = entry.rn + CASE p.horizon {_EQUITY_HORIZON_DAYS} END
        WHERE p.outcome_filled_at IS NOT NULL
        ORDER BY p.prediction_date DESC, p.predicted_probability DESC
        LIMIT ?
    """
    df = conn.execute(sql, [limit]).fetchdf()
    return _fill_estimated_equity_maturity(df)


def get_crypto_predictions(
    conn: duckdb.DuckDBPyConnection, prediction_date
) -> pd.DataFrame:
    sql = f"""
        WITH latest AS (
            SELECT symbol, MAX(trade_date) AS latest_date
            FROM crypto_prices_daily WHERE close IS NOT NULL GROUP BY symbol
        )
        SELECT p.symbol, p.horizon, p.predicted_probability, p.prediction_threshold,
               p.market_cap_bucket,
               entry.close AS price_at_prediction,
               (p.prediction_date + CASE p.horizon {_CRYPTO_HORIZON_INTERVAL} END)::DATE
                 AS maturity_date,
               mat.close AS price_at_maturity,
               cur.close AS current_price,
               p.actual_max_return, p.actual_max_drawdown, p.actual_hit,
               p.outcome_filled_at
        FROM crypto_ml_predictions p
        LEFT JOIN crypto_prices_daily entry
          ON entry.symbol = p.symbol AND entry.trade_date = p.prediction_date
        LEFT JOIN crypto_prices_daily mat
          ON mat.symbol = p.symbol
         AND mat.trade_date = (p.prediction_date
                                + CASE p.horizon {_CRYPTO_HORIZON_INTERVAL} END)::DATE
        LEFT JOIN latest lp ON lp.symbol = p.symbol
        LEFT JOIN crypto_prices_daily cur
          ON cur.symbol = lp.symbol AND cur.trade_date = lp.latest_date
        WHERE p.prediction_date = ?
        ORDER BY p.horizon, p.predicted_probability DESC
    """
    return conn.execute(sql, [prediction_date]).fetchdf()


def get_crypto_recent_outcomes(
    conn: duckdb.DuckDBPyConnection, limit: int = 50
) -> pd.DataFrame:
    sql = f"""
        SELECT p.symbol, p.prediction_date, p.horizon, p.predicted_probability,
               entry.close AS price_at_prediction,
               (p.prediction_date + CASE p.horizon {_CRYPTO_HORIZON_INTERVAL} END)::DATE
                 AS maturity_date,
               mat.close AS price_at_maturity,
               p.actual_max_return, p.actual_max_drawdown, p.actual_hit,
               p.outcome_filled_at
        FROM crypto_ml_predictions p
        LEFT JOIN crypto_prices_daily entry
          ON entry.symbol = p.symbol AND entry.trade_date = p.prediction_date
        LEFT JOIN crypto_prices_daily mat
          ON mat.symbol = p.symbol
         AND mat.trade_date = (p.prediction_date
                                + CASE p.horizon {_CRYPTO_HORIZON_INTERVAL} END)::DATE
        WHERE p.outcome_filled_at IS NOT NULL
        ORDER BY p.prediction_date DESC, p.predicted_probability DESC
        LIMIT ?
    """
    return conn.execute(sql, [limit]).fetchdf()


def get_fx_recent_predictions(
    conn: duckdb.DuckDBPyConnection, limit: int = 30
) -> pd.DataFrame:
    sql = f"""
        WITH latest AS (
            SELECT MAX(datetime_utc) AS latest_dt FROM fx_prices_hourly
        )
        SELECT p.datetime_utc, p.direction, p.horizon, p.predicted_probability,
               entry.gbpeur_close AS price_at_prediction,
               (p.datetime_utc + CASE p.horizon {_FX_HORIZON_INTERVAL} END)
                 AS maturity_datetime,
               mat.gbpeur_close AS price_at_maturity,
               cur.gbpeur_close AS current_price,
               p.actual_max_pips, p.actual_hit, p.outcome_filled_at
        FROM fx_ml_predictions p
        LEFT JOIN fx_prices_hourly entry
          ON entry.datetime_utc = p.datetime_utc
        LEFT JOIN fx_prices_hourly mat
          ON mat.datetime_utc = p.datetime_utc
                                + CASE p.horizon {_FX_HORIZON_INTERVAL} END
        CROSS JOIN latest lp
        LEFT JOIN fx_prices_hourly cur ON cur.datetime_utc = lp.latest_dt
        ORDER BY p.datetime_utc DESC, p.direction, p.horizon
        LIMIT ?
    """
    return conn.execute(sql, [limit]).fetchdf()


# ──────────────────────────────────────────────────────────────────────
# Paper-trading tab (Gap 3) — reads the crypto-trading-engine DuckDB
# read-only via CRYPTO_ENGINE_DB_PATH. See DECISIONS.md ADR-020.
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_ENGINE_DB = "/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb"
ENGINE_DB_ENV = "CRYPTO_ENGINE_DB_PATH"

# Position states that mean "still in the market" (not exit_filled / failed / candidate).
_PAPER_LIVE_STATES = ("entry_pending", "entry_filled", "trailing_active", "exit_pending")

_PAPER_STATE_PRETTY = {
    "entry_pending": "Entry pending",
    "entry_filled": "Entry filled",
    "trailing_active": "Trailing active",
    "exit_pending": "Exit pending",
    "exit_filled": "Exit filled",
    "failed": "Failed",
    "candidate": "Candidate",
}

# Engine event types that are mechanical noise rather than a human-readable reason.
_PAPER_MECHANICAL_EVENTS = {"state_change", "order_placed", "order_filled", "leverage_set"}

# Shown for closed positions whose exit_price / realized_pnl_usd columns are
# genuinely NULL: pre-EXIT-PRICE-001 closes and reconcile auto-closes of
# engine_only_position rows (no real SELL fill). Once EXIT-PRICE-001 / the
# reconcile backfill populates those columns the real values are shown. See KI-136.
_UNCOMPUTABLE = "uncomputable (KI-136)"
_DASH = "—"


def engine_db_path() -> str:
    """Path to the crypto-trading-engine DuckDB (env-overridable)."""
    return os.environ.get(ENGINE_DB_ENV, _DEFAULT_ENGINE_DB)


def _connect_engine() -> duckdb.DuckDBPyConnection:
    """Fresh read-only connection to the engine DuckDB.

    Raises whatever ``duckdb.connect`` raises if the file is missing / locked
    / corrupt — the caller (the Streamlit tab) is responsible for catching
    that and degrading gracefully without breaking the rest of the page.
    """
    return duckdb.connect(engine_db_path(), read_only=True)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _calc_stop(entry_price, peak_price, trail_pct: float, activation_pct: float):
    """Policy-D trailing stop: peak − trail_pct·(peak − entry), once activated.

    Returns a float when the trailing stop is active, else an explanatory
    string ("—" if prices are missing, "— (not activated)" if the peak hasn't
    cleared the activation threshold yet).
    """
    if entry_price is None or peak_price is None:
        return _DASH
    if peak_price < entry_price * (1.0 + activation_pct):
        return f"{_DASH} (not activated)"
    return peak_price - trail_pct * (peak_price - entry_price)


def _latest_reason(events: list[tuple[str, str]]) -> str:
    """Best-effort human-readable reason from a position's events.

    ``events`` is a time-ordered list of ``(event_type, payload_json)``. Scans
    newest-first for the first payload key that reads like a reason
    (operator_reason, action, exit_reason≠"none", kind, reason, note, error);
    falls back to the most recent non-mechanical event type; else "".
    """
    for event_type, payload in reversed(events):
        try:
            data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        for key in ("operator_reason", "action", "kind", "reason", "note", "error"):
            val = data.get(key)
            if val:
                return str(val)
        exit_reason = data.get("exit_reason")
        if exit_reason and str(exit_reason).lower() != "none":
            return f"exit: {exit_reason}"
        if event_type not in _PAPER_MECHANICAL_EVENTS:
            return event_type
    return ""


def _reasons_by_position(
    engine_conn: duckdb.DuckDBPyConnection, position_ids: list
) -> dict:
    if not position_ids:
        return {}
    placeholders = ",".join("?" for _ in position_ids)
    rows = engine_conn.execute(
        f"SELECT position_id, event_type, payload, timestamp FROM events "
        f"WHERE position_id IN ({placeholders}) ORDER BY timestamp",
        position_ids,
    ).fetchall()
    grouped: dict = defaultdict(list)
    for pid, event_type, payload, _ts in rows:
        grouped[pid].append((event_type, payload))
    return {pid: _latest_reason(evs) for pid, evs in grouped.items()}


def get_paper_open_positions(
    engine_conn: duckdb.DuckDBPyConnection, *, trail_pct: float, activation_pct: float
) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in _PAPER_LIVE_STATES)
    rows = engine_conn.execute(
        f"SELECT symbol, current_state, entry_date, entry_price, qty, peak_price "
        f"FROM positions WHERE current_state IN ({placeholders}) "
        f"ORDER BY entry_date DESC, symbol",
        list(_PAPER_LIVE_STATES),
    ).fetchall()
    out = []
    for symbol, state, entry_date, entry_price, qty, peak_price in rows:
        out.append({
            "symbol": symbol,
            "state": _PAPER_STATE_PRETTY.get(state, state),
            "entry_date": entry_date,
            "entry_price": entry_price if entry_price is not None else _DASH,
            "qty": qty if qty is not None else _DASH,
            "peak_price": peak_price if peak_price is not None else _DASH,
            "calc_stop": _calc_stop(entry_price, peak_price, trail_pct, activation_pct),
        })
    return pd.DataFrame(out, columns=[
        "symbol", "state", "entry_date", "entry_price", "qty", "peak_price", "calc_stop",
    ])


def get_paper_closed_trades(
    engine_conn: duckdb.DuckDBPyConnection, *, limit: int = 30
) -> pd.DataFrame:
    """Recent ``exit_filled`` positions, newest-first.

    ``exit_price`` / ``realized_pnl`` come straight from the engine's
    ``positions.exit_price`` / ``positions.realized_pnl_usd`` columns
    (EXIT-PRICE-001): the recorded SELL-fill weighted-average price and the
    gross ``(exit_price - entry_price) * qty`` P&L, the latter rounded to
    cents. Each is shown as ``"uncomputable (KI-136)"`` only when its column
    is genuinely NULL — pre-EXIT-PRICE-001 closes and reconcile auto-closes of
    ``engine_only_position`` rows (no real SELL fill, hence no recoverable price).
    """
    rows = engine_conn.execute(
        "SELECT id, symbol, entry_date, entry_price, qty, peak_price, updated_at, "
        "exit_price, realized_pnl_usd "
        "FROM positions WHERE current_state = 'exit_filled' "
        "ORDER BY updated_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    reasons = _reasons_by_position(engine_conn, [r[0] for r in rows])
    out = []
    for (pid, symbol, entry_date, entry_price, qty, peak_price, updated_at,
         exit_price, realized_pnl_usd) in rows:
        out.append({
            "symbol": symbol,
            "entry_date": entry_date,
            "entry_price": entry_price if entry_price is not None else _DASH,
            "qty": qty if qty is not None else _DASH,
            "peak_price": peak_price if peak_price is not None else _DASH,
            "closed_at": updated_at,
            "close_reason": reasons.get(pid, ""),
            "exit_price": exit_price if exit_price is not None else _UNCOMPUTABLE,
            "realized_pnl": (
                round(realized_pnl_usd, 2) if realized_pnl_usd is not None else _UNCOMPUTABLE
            ),
        })
    return pd.DataFrame(out, columns=[
        "symbol", "entry_date", "entry_price", "qty", "peak_price", "closed_at",
        "close_reason", "exit_price", "realized_pnl",
    ])


def get_paper_failed_entries(
    engine_conn: duckdb.DuckDBPyConnection, *, limit: int = 20
) -> pd.DataFrame:
    rows = engine_conn.execute(
        "SELECT id, symbol, entry_date FROM positions WHERE current_state = 'failed' "
        "ORDER BY updated_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    reasons = _reasons_by_position(engine_conn, [r[0] for r in rows])
    out = [{
        "symbol": symbol,
        "entry_date": entry_date,
        "reason": reasons.get(pid, ""),
    } for pid, symbol, entry_date in rows]
    return pd.DataFrame(out, columns=["symbol", "entry_date", "reason"])


def get_paper_engine_runs_summary(
    engine_conn: duckdb.DuckDBPyConnection, *, now: datetime | None = None
) -> dict:
    now = now or _utcnow_naive()
    last_monitor = engine_conn.execute(
        "SELECT max(started_at) FROM engine_runs WHERE phase = 'monitor' AND success = true"
    ).fetchone()[0]
    last_entry = engine_conn.execute(
        "SELECT max(started_at) FROM engine_runs WHERE phase = 'entry' AND success = true"
    ).fetchone()[0]
    placeholders = ",".join("?" for _ in _PAPER_LIVE_STATES)
    n_open = engine_conn.execute(
        f"SELECT count(*) FROM positions WHERE current_state IN ({placeholders})",
        list(_PAPER_LIVE_STATES),
    ).fetchone()[0]
    n_closed_14d = engine_conn.execute(
        "SELECT count(*) FROM positions WHERE current_state = 'exit_filled' AND updated_at >= ?",
        [now - timedelta(days=14)],
    ).fetchone()[0]
    return {
        "last_monitor_at": last_monitor,
        "last_entry_at": last_entry,
        "n_open": int(n_open),
        "n_closed_14d": int(n_closed_14d),
    }


# ──────────────────────────────────────────────────────────────────────
# Daily balance (top-of-tab strip on Paper Trading)
#
# Source: crypto-trading-engine's ``daily_pnl`` table (read-only — ADR-020).
# The strategy was effectively re-anchored on 2026-05-12 when the KI-138
# OHLCV repair landed; pre-baseline equity readings are mixed with corrupted
# prices, so the table never shows them. The baseline date is read from
# ``config/monitoring.yaml`` (latest ``strategy_baselines[*].date``) with a
# hardcoded ``2026-05-12`` fallback so the dashboard never breaks if the
# config file is absent.
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_PAPER_BASELINE = date(2026, 5, 12)
_MONITORING_YAML = Path(__file__).resolve().parent.parent.parent / "config" / "monitoring.yaml"


def _load_monitoring_config() -> dict:
    """Read ``config/monitoring.yaml``; returns ``{}`` if absent or unreadable.

    Module-level so tests can monkey-patch a fixture dict in without touching
    the live config file.
    """
    try:
        import yaml
        if not _MONITORING_YAML.exists():
            return {}
        return yaml.safe_load(_MONITORING_YAML.read_text()) or {}
    except Exception:
        return {}


def paper_baseline_date() -> date:
    """Most-recent ``strategy_baselines[*].date`` from config, or the
    hardcoded ``2026-05-12`` anchor as a fallback.

    Shared with the drift monitor so the dashboard's daily-balance table
    and the drift monitor's rolling window agree on what counts as "since
    the reset".
    """
    cfg = _load_monitoring_config()
    items = (cfg.get("paper_trading_drift") or {}).get("strategy_baselines") or []
    parsed: list[date] = []
    for item in items:
        raw = item.get("date") if isinstance(item, dict) else None
        if isinstance(raw, date) and not isinstance(raw, datetime):
            parsed.append(raw)
        elif isinstance(raw, str):
            try:
                parsed.append(datetime.strptime(raw, "%Y-%m-%d").date())
            except ValueError:
                continue
    return max(parsed) if parsed else _DEFAULT_PAPER_BASELINE


def get_daily_balance_since_baseline(
    engine_conn: duckdb.DuckDBPyConnection, *, since: date
) -> pd.DataFrame:
    """Daily account-equity strip for the Paper Trading tab.

    Columns (always present, even when empty):
        date              — settlement date of the daily_pnl row
        equity            — account_equity_usd at end-of-day
        daily_delta       — equity − equity_of_prior_present_row;
                            ``None`` for the first in-window row
        cumulative_delta  — equity − equity_of_first_in_window_row;
                            ``0.0`` for the first row

    Pre-baseline rows are excluded. Date gaps (if reconcile skipped a day)
    are preserved as-is — ``daily_delta`` on the row after a gap is the
    raw difference against the previous *present* row, which is the
    honest representation of the equity path the operator actually
    observed.

    Empty input (the pre-RECONCILE-001 reality on the live engine DB)
    yields an empty DataFrame with the correct schema so the caller can
    render an "no data" branch without conditional column handling.
    """
    columns = ["date", "equity", "daily_delta", "cumulative_delta"]
    rows = engine_conn.execute(
        "SELECT date, account_equity_usd FROM daily_pnl "
        "WHERE date >= ? ORDER BY date ASC",
        [since],
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=columns)

    anchor = float(rows[0][1])
    out = []
    prev: float | None = None
    for d, equity in rows:
        equity = float(equity)
        daily_delta = None if prev is None else equity - prev
        out.append({
            "date": d,
            "equity": equity,
            "daily_delta": daily_delta,
            "cumulative_delta": equity - anchor,
        })
        prev = equity
    return pd.DataFrame(out, columns=columns)
