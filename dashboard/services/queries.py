from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd


def _connect() -> duckdb.DuckDBPyConnection:
    import os
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
