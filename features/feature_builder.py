from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime

import duckdb

from features.valuation import compute_valuation
from features.quality import compute_quality
from features.catalyst import compute_catalyst
from features.momentum import compute_momentum
from features.sentiment import compute_sentiment
from features.macro import compute_macro
from features.risk import compute_risk

logger = logging.getLogger("mhde.features")


def _upsert_feature(conn: duckdb.DuckDBPyConnection, run_id: str, ticker: str | None, as_of: date, f: dict) -> None:
    metadata = f.get("metadata")
    metadata_json = json.dumps(metadata) if metadata else None
    try:
        conn.execute(
            """
            INSERT INTO features
                (id, run_id, ticker, as_of_date, feature_group, feature_name,
                 feature_value, feature_score, source, confidence, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, ticker, feature_group, feature_name) DO UPDATE SET
                feature_value = excluded.feature_value,
                feature_score = excluded.feature_score,
                confidence = excluded.confidence,
                metadata_json = excluded.metadata_json
            """,
            [
                uuid.uuid4().hex[:16], run_id,
                ticker if ticker is not None else f.get("ticker"),
                as_of,
                f["feature_group"], f["feature_name"],
                f.get("feature_value"), f.get("feature_score"),
                f.get("source"), f.get("confidence", "medium"),
                metadata_json, datetime.utcnow(),
            ],
        )
    except Exception as exc:
        logger.debug("Feature upsert failed %s/%s: %s", ticker, f.get("feature_name"), exc)


def build_features(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    tickers: list[str],
    cfg: dict,
) -> dict:
    as_of = date.today()
    total = len(tickers)
    covered = 0

    # Macro features (ticker-independent)
    macro_features = compute_macro(conn, run_id, as_of)
    for f in macro_features:
        _upsert_feature(conn, run_id, None, as_of, f)

    for ticker in tickers:
        per_ticker_features: list[dict] = []

        val = compute_valuation(conn, run_id, ticker, as_of)
        qual = compute_quality(conn, run_id, ticker, as_of)
        cat = compute_catalyst(conn, run_id, ticker, as_of)
        mom = compute_momentum(conn, run_id, ticker, as_of)
        sent = compute_sentiment(conn, run_id, ticker, as_of)
        per_ticker_features.extend(val + qual + cat + mom + sent)

        risk = compute_risk(conn, run_id, ticker, as_of, per_ticker_features)
        per_ticker_features.extend(risk)

        scored_count = sum(1 for f in per_ticker_features if f.get("feature_score") is not None)
        if scored_count > 0:
            covered += 1

        for f in per_ticker_features:
            _upsert_feature(conn, run_id, ticker, as_of, f)

    coverage_pct = covered / total * 100 if total > 0 else 0
    logger.info(
        "Features built: %d/%d tickers with data (%.0f%% coverage)",
        covered, total, coverage_pct,
    )
    if coverage_pct < 50:
        logger.warning(
            "WARNING: Only %.0f%% of tickers have feature data. "
            "Run 'ingest all' to populate more data.",
            coverage_pct,
        )

    return {"tickers": total, "covered": covered, "coverage_pct": coverage_pct}
