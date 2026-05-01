from __future__ import annotations

import importlib.util
import os

import duckdb


def check_llm_provider(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute(
        "SELECT provider FROM llm_runs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    provider = row[0] if row else None
    if provider is None:
        return {"check_name": "llm_provider", "status": "warn", "severity": "medium",
                "message": "No LLM runs recorded yet. LLM layer has not been exercised."}
    if provider in ("mock", "mockprovider"):
        return {"check_name": "llm_provider", "status": "warn", "severity": "medium",
                "message": "LLM running in mock mode. Set OPENAI_API_KEY or NVIDIA_API_KEY for real analysis."}
    return {"check_name": "llm_provider", "status": "pass", "severity": "low",
            "message": f"LLM provider: {provider}"}


def check_telegram_configured() -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return {"check_name": "telegram_configured", "status": "warn", "severity": "low",
                "message": "Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable alerts."}
    return {"check_name": "telegram_configured", "status": "pass", "severity": "low",
            "message": "Telegram credentials present"}


def check_email_configured() -> dict:
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    if not host or not user:
        return {"check_name": "email_configured", "status": "warn", "severity": "low",
                "message": "Email not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD for email digests."}
    return {"check_name": "email_configured", "status": "pass", "severity": "low",
            "message": "Email (SMTP) credentials present"}


def check_stub_sources(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    stub_sources = {
        "fda": "FDA ingestion is a stub. No drug approval events are being tracked.",
        "stocktwits": "StockTwits ingestion is a stub. No social sentiment data is being tracked.",
        "gdelt": "GDELT ingestion is a stub. No news sentiment data is being tracked.",
    }
    results = []
    known = {r[0] for r in conn.execute("SELECT DISTINCT source_name FROM source_runs").fetchall()}
    for source, msg in stub_sources.items():
        if source not in known:
            results.append({
                "check_name": f"stub_{source}",
                "status": "warn",
                "severity": "low",
                "message": msg,
            })
    return results


def check_candidate_reviews(conn: duckdb.DuckDBPyConnection) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM candidate_reviews").fetchone()[0]
    if count == 0:
        return {"check_name": "candidate_reviews", "status": "warn", "severity": "low",
                "message": "No candidate reviews submitted. Human review data is required for learning loop calibration."}
    return {"check_name": "candidate_reviews", "status": "pass", "severity": "low",
            "message": f"{count} candidate review(s) submitted"}


def check_backtest_coverage(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute(
        "SELECT tickers_tested, warning FROM backtest_runs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"check_name": "backtest_coverage", "status": "warn", "severity": "low",
                "message": "No backtest runs recorded yet."}
    tickers_tested, warning = row
    if tickers_tested == 0 or (warning and "insufficient" in warning.lower()):
        return {"check_name": "backtest_coverage", "status": "warn", "severity": "low",
                "message": "Backtest has insufficient historical coverage. Accumulate multiple weeks of runs."}
    return {"check_name": "backtest_coverage", "status": "pass", "severity": "low",
            "message": f"Backtest ran on {tickers_tested} tickers"}


def check_xgboost_installed() -> dict:
    if importlib.util.find_spec("xgboost") is None:
        return {"check_name": "xgboost_installed", "status": "warn", "severity": "low",
                "message": "xgboost not installed. Experimental ranking model unavailable. pip install xgboost to enable."}
    return {"check_name": "xgboost_installed", "status": "pass", "severity": "low",
            "message": "xgboost installed"}


def check_finra_data(conn: duckdb.DuckDBPyConnection) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM short_interest").fetchone()[0]
    if count == 0:
        return {"check_name": "finra_data", "status": "warn", "severity": "low",
                "message": "FINRA short interest: 0 records. CDN may be returning empty for current tickers."}
    return {"check_name": "finra_data", "status": "pass", "severity": "low",
            "message": f"FINRA short interest: {count} records"}


def check_universe_vs_config(conn: duckdb.DuckDBPyConnection, cfg: dict) -> dict:
    max_symbols = cfg.get("universe", {}).get("max_symbols", 500)
    actual = conn.execute("SELECT COUNT(*) FROM companies WHERE is_active = true").fetchone()[0]
    if actual < max_symbols:
        return {"check_name": "universe_vs_config", "status": "warn", "severity": "low",
                "message": f"Universe has {actual} companies; configured max_symbols={max_symbols}. "
                           "Run daily-radar to ingest the full universe."}
    return {"check_name": "universe_vs_config", "status": "pass", "severity": "low",
            "message": f"Universe size ({actual}) meets configured max_symbols ({max_symbols})"}


def check_a_tier_candidates(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE tier = 'A' AND run_id = "
        "(SELECT run_id FROM scores ORDER BY created_at DESC LIMIT 1)"
    ).fetchone()
    count = row[0] if row else 0
    if count == 0:
        return {"check_name": "a_tier_candidates", "status": "warn", "severity": "low",
                "message": "No A-tier candidates in latest run. Score weights may need calibration, "
                           "or data is immature (expected on first runs)."}
    return {"check_name": "a_tier_candidates", "status": "pass", "severity": "low",
            "message": f"{count} A-tier candidate(s) in latest run"}


def check_score_distribution_quality(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Warn when the score distribution shows signs of fake precision (clustering, low coverage)."""
    results = []

    run_id_row = conn.execute(
        "SELECT run_id FROM scores GROUP BY run_id ORDER BY MAX(created_at) DESC LIMIT 1"
    ).fetchone()
    if not run_id_row:
        return []
    run_id = run_id_row[0]

    total_row = conn.execute("SELECT COUNT(*) FROM scores WHERE run_id=?", [run_id]).fetchone()
    total = total_row[0] if total_row else 0
    if total == 0:
        return []

    # Low confidence rate
    low_conf_row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND confidence IN ('low', 'none')", [run_id]
    ).fetchone()
    low_conf = low_conf_row[0] if low_conf_row else 0
    if low_conf / total > 0.50:
        results.append({
            "check_name": "score_low_confidence_rate",
            "status": "warn",
            "severity": "medium",
            "message": (
                f"{low_conf}/{total} ({low_conf/total:.0%}) candidates have low/no confidence. "
                "Missing price, momentum, or sentiment data. Scores are partial estimates."
            ),
        })

    # Missing valuation
    null_cheap_row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND cheap_score IS NULL", [run_id]
    ).fetchone()
    null_cheap = null_cheap_row[0] if null_cheap_row else 0
    if null_cheap / total > 0.50:
        results.append({
            "check_name": "score_missing_valuation",
            "status": "warn",
            "severity": "medium",
            "message": (
                f"{null_cheap}/{total} ({null_cheap/total:.0%}) candidates have no valuation score. "
                "Polygon price data may be missing."
            ),
        })

    # Missing momentum
    null_mom_row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND momentum_score IS NULL", [run_id]
    ).fetchone()
    null_mom = null_mom_row[0] if null_mom_row else 0
    if null_mom / total > 0.50:
        results.append({
            "check_name": "score_missing_momentum",
            "status": "warn",
            "severity": "low",
            "message": (
                f"{null_mom}/{total} ({null_mom/total:.0%}) candidates have no momentum score. "
                "Insufficient price history (need 20+ days)."
            ),
        })

    # Incomplete tier prevalence
    incomplete_row = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE run_id=? AND tier='Incomplete'", [run_id]
    ).fetchone()
    n_incomplete = incomplete_row[0] if incomplete_row else 0
    if n_incomplete / total > 0.30:
        results.append({
            "check_name": "score_incomplete_rate",
            "status": "warn",
            "severity": "medium",
            "message": (
                f"{n_incomplete}/{total} ({n_incomplete/total:.0%}) candidates are Incomplete "
                "(insufficient data to rank). Ingest more data sources for reliable scoring."
            ),
        })

    return results
