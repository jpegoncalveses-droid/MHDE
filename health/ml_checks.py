from __future__ import annotations

import glob
import os
from datetime import date, timedelta

import duckdb


MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", "saved")


def check_trained_models() -> dict:
    models = glob.glob(os.path.join(MODELS_DIR, "*.joblib"))
    if not models:
        return {"check_name": "ml_trained_models", "status": "fail", "severity": "critical",
                "message": "No trained models found in models/saved/"}
    horizons = {os.path.basename(m).split("_")[0] for m in models}
    return {"check_name": "ml_trained_models", "status": "pass", "severity": "low",
            "message": f"{len(models)} model(s) for horizons: {', '.join(sorted(horizons))}"}


def check_last_prediction(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute("""
        SELECT MAX(prediction_date) FROM ml_predictions
    """).fetchone()
    if row is None or row[0] is None:
        return {"check_name": "ml_last_prediction", "status": "fail", "severity": "critical",
                "message": "No predictions found in ml_predictions"}
    last = row[0]
    if isinstance(last, str):
        last = date.fromisoformat(last)
    elif hasattr(last, "date"):
        last = last.date()
    age_days = (date.today() - last).days
    if age_days > 3:
        return {"check_name": "ml_last_prediction", "status": "warn", "severity": "medium",
                "message": f"Last prediction is {age_days} days old ({last})"}
    return {"check_name": "ml_last_prediction", "status": "pass", "severity": "low",
            "message": f"Last prediction: {last} ({age_days}d ago)"}


def check_rolling_precision(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS hits
        FROM ml_predictions
        WHERE outcome_filled_at IS NOT NULL
    """).fetchone()
    n, hits = row[0], row[1]
    if n == 0:
        return {"check_name": "ml_rolling_precision", "status": "skip", "severity": "low",
                "message": "No outcomes filled yet"}
    precision = hits / n
    status = "pass" if precision >= 0.50 else "warn"
    return {"check_name": "ml_rolling_precision", "status": status, "severity": "medium",
            "message": f"Precision: {precision:.0%} ({hits}/{n} hits)"}


_REFERENCE_TICKERS = (
    "SPY", "VIX",
    "XLK", "XLF", "XLV", "XLE", "XLY",
    "XLI", "XLP", "XLB", "XLU", "XLRE", "XLC",
)
_CROSS_ASSET_MAX_AGE_DAYS = 3


def check_cross_asset_freshness(conn: duckdb.DuckDBPyConnection) -> dict:
    """Assert SPY/VIX/sector-ETF prices in prices_daily are within T-3.

    These feed ml/features.py (return_vs_spy_*, return_vs_sector_*, beta_60d,
    vix_level, vix_change_5d). If any are missing or stale, those features
    silently go NULL on every prediction. See KI for ReferenceTickersIngestor.
    """
    today = date.today()
    threshold = today - timedelta(days=_CROSS_ASSET_MAX_AGE_DAYS)
    missing: list[str] = []
    stale: list[tuple[str, date, int]] = []

    for ticker in _REFERENCE_TICKERS:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM prices_daily WHERE ticker = ?",
            [ticker],
        ).fetchone()
        latest = row[0] if row else None
        if latest is None:
            missing.append(ticker)
            continue
        if isinstance(latest, str):
            latest = date.fromisoformat(latest)
        elif hasattr(latest, "date") and not isinstance(latest, date):
            latest = latest.date()
        if latest < threshold:
            stale.append((ticker, latest, (today - latest).days))

    if missing:
        return {
            "check_name": "cross_asset_freshness", "status": "fail", "severity": "critical",
            "message": f"Missing reference tickers in prices_daily: {', '.join(missing)}",
        }
    if stale:
        names = ", ".join(f"{t} ({age}d)" for t, _, age in stale)
        return {
            "check_name": "cross_asset_freshness", "status": "fail", "severity": "high",
            "message": f"Stale reference tickers (>{_CROSS_ASSET_MAX_AGE_DAYS}d): {names}",
        }
    return {
        "check_name": "cross_asset_freshness", "status": "pass", "severity": "low",
        "message": f"All {len(_REFERENCE_TICKERS)} reference tickers fresh (≤{_CROSS_ASSET_MAX_AGE_DAYS}d)",
    }


_MACRO_SERIES = ("DGS10", "DGS2", "VIXCLS")
_MACRO_SERIES_MAX_AGE_DAYS = 5


def check_macro_series_freshness(conn: duckdb.DuckDBPyConnection) -> dict:
    """Assert FRED macro series consumed by ml/features.py are present + fresh.

    DGS10 + DGS2 feed _load_yield_curve (yield_curve_10y_2y). VIXCLS is the
    macro_series-side VIX backup (prices_daily.VIX is the primary path).

    Threshold is wider than check_cross_asset_freshness (T-5 vs T-3) because
    FRED publishes T-1 on a business-day cadence and weekends can push the
    last value back by 2-3 days legitimately.
    """
    today = date.today()
    threshold = today - timedelta(days=_MACRO_SERIES_MAX_AGE_DAYS)
    missing: list[str] = []
    stale: list[tuple[str, date, int]] = []

    for series_id in _MACRO_SERIES:
        row = conn.execute(
            "SELECT MAX(as_of_date) FROM macro_series WHERE series_id = ?",
            [series_id],
        ).fetchone()
        latest = row[0] if row else None
        if latest is None:
            missing.append(series_id)
            continue
        if isinstance(latest, str):
            latest = date.fromisoformat(latest)
        elif hasattr(latest, "date") and not isinstance(latest, date):
            latest = latest.date()
        if latest < threshold:
            stale.append((series_id, latest, (today - latest).days))

    if missing:
        return {
            "check_name": "macro_series_freshness", "status": "fail", "severity": "critical",
            "message": f"Missing macro series in macro_series: {', '.join(missing)}",
        }
    if stale:
        names = ", ".join(f"{s} ({age}d)" for s, _, age in stale)
        return {
            "check_name": "macro_series_freshness", "status": "fail", "severity": "high",
            "message": f"Stale macro series (>{_MACRO_SERIES_MAX_AGE_DAYS}d): {names}",
        }
    return {
        "check_name": "macro_series_freshness", "status": "pass", "severity": "low",
        "message": f"All {len(_MACRO_SERIES)} macro series fresh (≤{_MACRO_SERIES_MAX_AGE_DAYS}d)",
    }


def check_ml_tables_freshness(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    results = []
    for table in ("ml_features", "ml_labels"):
        row = conn.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()
        if row is None or row[0] is None:
            results.append({"check_name": f"{table}_freshness", "status": "fail",
                            "severity": "critical", "message": f"{table} is empty"})
            continue
        last = row[0]
        if isinstance(last, str):
            last = date.fromisoformat(last)
        elif hasattr(last, "date"):
            last = last.date()
        age_days = (date.today() - last).days
        if age_days > 7:
            status, sev = "warn", "medium"
        else:
            status, sev = "pass", "low"
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        results.append({"check_name": f"{table}_freshness", "status": status, "severity": sev,
                        "message": f"Latest: {last} ({age_days}d ago), {count:,} rows"})
    return results
