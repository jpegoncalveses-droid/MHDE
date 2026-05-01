from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

logger = logging.getLogger("mhde.health")


def check_universe_size(conn: duckdb.DuckDBPyConnection) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM companies WHERE is_active = true").fetchone()[0]
    if count == 0:
        return {"check_name": "universe_size", "status": "fail", "severity": "high",
                "message": "No companies in universe. Run 'ingest all'."}
    if count < 10:
        return {"check_name": "universe_size", "status": "warn", "severity": "medium",
                "message": f"{count} companies (very small universe — expected 100+)"}
    return {"check_name": "universe_size", "status": "pass", "severity": "low",
            "message": f"{count} companies in universe"}


def check_price_data(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        row = conn.execute("SELECT MAX(trade_date), COUNT(DISTINCT ticker) FROM prices_daily").fetchone()
        latest, tickers = row if row else (None, 0)
        if not latest or tickers == 0:
            return {"check_name": "price_data", "status": "warn", "severity": "medium",
                    "message": "No price data. Run 'ingest all' with POLYGON_API_KEY."}
        lag = (date.today() - latest).days
        if lag > 5:
            return {"check_name": "price_data", "status": "warn", "severity": "medium",
                    "message": f"Price data is {lag}d stale (latest: {latest}, {tickers} tickers)"}
        return {"check_name": "price_data", "status": "pass", "severity": "low",
                "message": f"Price data: {tickers} tickers, latest {latest}"}
    except Exception as exc:
        return {"check_name": "price_data", "status": "skip", "severity": "low",
                "message": f"Could not check: {exc}"}


def check_fundamental_data(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        row = conn.execute("SELECT COUNT(DISTINCT ticker) FROM fundamentals_raw").fetchone()
        count = row[0] if row else 0
        if count == 0:
            return {"check_name": "fundamental_data", "status": "warn", "severity": "medium",
                    "message": "No fundamental data. Run 'ingest all'."}
        return {"check_name": "fundamental_data", "status": "pass", "severity": "low",
                "message": f"Fundamentals for {count} tickers"}
    except Exception as exc:
        return {"check_name": "fundamental_data", "status": "skip", "severity": "low",
                "message": f"Could not check: {exc}"}


def check_feature_coverage(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        total = conn.execute("SELECT COUNT(*) FROM companies WHERE is_active = true").fetchone()[0]
        if total == 0:
            return {"check_name": "feature_coverage", "status": "skip", "severity": "low",
                    "message": "No universe to check"}
        scored = conn.execute("SELECT COUNT(DISTINCT ticker) FROM features").fetchone()[0]
        pct = scored / total * 100 if total > 0 else 0
        if pct < 10:
            return {"check_name": "feature_coverage", "status": "warn", "severity": "medium",
                    "message": f"{scored}/{total} tickers have features ({pct:.0f}%)"}
        return {"check_name": "feature_coverage", "status": "pass", "severity": "low",
                "message": f"{scored}/{total} tickers have features ({pct:.0f}%)"}
    except Exception as exc:
        return {"check_name": "feature_coverage", "status": "skip", "severity": "low",
                "message": f"Could not check: {exc}"}


def check_score_distribution(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        rows = conn.execute(
            "SELECT tier, COUNT(*) FROM scores GROUP BY tier"
        ).fetchall()
        if not rows:
            return {"check_name": "score_distribution", "status": "warn", "severity": "low",
                    "message": "No scores yet. Run 'score'."}
        dist = ", ".join(f"{t}:{c}" for t, c in rows)
        return {"check_name": "score_distribution", "status": "pass", "severity": "low",
                "message": f"Score tiers: {dist}"}
    except Exception as exc:
        return {"check_name": "score_distribution", "status": "skip", "severity": "low",
                "message": f"Could not check: {exc}"}
