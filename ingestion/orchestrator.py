from __future__ import annotations

import logging
import uuid

import duckdb

from ingestion.ingest_sec import SECIngestor
from ingestion.ingest_prices import PricesIngestor
from ingestion.ingest_stooq import StooqPricesIngestor
from ingestion.ingest_yahoo_historical import YahooHistoricalIngestor
from ingestion.ingest_fred import FREDIngestor
from ingestion.ingest_finra import FINRAIngestor
from ingestion.ingest_cftc import CFTCIngestor
from ingestion.ingest_events import EventsIngestor
from ingestion.ingest_fda import FDAIngestor
from ingestion.ingest_stocktwits import StocktwitsIngestor
from ingestion.ingest_gdelt import GDELTIngestor
from universe.universe_builder import build_universe

logger = logging.getLogger("mhde.ingestion.orchestrator")

_ALL_INGESTORS = [
    SECIngestor,
    PricesIngestor,       # Polygon — runs first, fills what it can
    StooqPricesIngestor,      # Stooq — fills gaps for tickers Polygon missed
    YahooHistoricalIngestor,  # Yahoo — bootstraps/increments price history for momentum
    FREDIngestor,
    FINRAIngestor,
    CFTCIngestor,
    EventsIngestor,
    FDAIngestor,
    StocktwitsIngestor,
    GDELTIngestor,
]

_RUN_STATUSES = {"active", "experimental"}


def run_all(
    conn: duckdb.DuckDBPyConnection,
    cfg: dict,
    target: str = "all",
    dry_run: bool = False,
    run_id: str | None = None,
    tickers_override: list[str] | None = None,
) -> dict:
    """Run all active ingestors. Returns summary of results."""
    sources_cfg = cfg.get("sources", {}).get("sources", {})
    if not run_id:
        run_id = uuid.uuid4().hex[:16]

    # Build/refresh universe first
    logger.info("Building universe (run_id=%s)...", run_id)
    universe_count = build_universe(conn, cfg)
    logger.info("Universe: %d companies", universe_count)

    if cfg.get("ingestion", {}).get("skip_all_ingestion", False):
        logger.info("Skipping all ingestion (skip_all_ingestion=True)")
        return {
            "run_id": run_id,
            "universe_size": universe_count,
            "sources_succeeded": 0,
            "sources_failed": 0,
            "sources_skipped": len(_ALL_INGESTORS),
            "skipped_reason": "skip_all_ingestion",
            "results": {},
        }

    if tickers_override is not None:
        tickers = tickers_override
        logger.info("Tickers capped to %d (override)", len(tickers))
    else:
        rows = conn.execute(
            "SELECT ticker FROM companies WHERE is_active = true ORDER BY universe_tier, ticker"
        ).fetchall()
        tickers = [r[0] for r in rows]
        max_symbols = cfg.get("universe", {}).get("max_symbols")
        if max_symbols and len(tickers) > max_symbols:
            tickers = tickers[:max_symbols]
            logger.info("Tickers capped to %d by max_symbols config", max_symbols)

    if dry_run:
        logger.info("[DRY RUN] Would ingest %d tickers from %d sources",
                    len(tickers), len(_ALL_INGESTORS))
        for cls in _ALL_INGESTORS:
            source_cfg = sources_cfg.get(cls.source_name, {})
            status = source_cfg.get("status", cls.source_status)
            print(f"  {cls.source_name:<20} [{status}]")
        return {"run_id": run_id, "dry_run": True}

    results = {}
    succeeded = failed_sources = skipped = 0

    for IngestorClass in _ALL_INGESTORS:
        source_cfg = sources_cfg.get(IngestorClass.source_name, {})
        status = source_cfg.get("status", IngestorClass.source_status)

        if target != "all" and IngestorClass.source_name != target:
            continue

        if status not in _RUN_STATUSES:
            logger.info("Skipping %s [%s]", IngestorClass.source_name, status)
            skipped += 1
            continue

        if status == "experimental":
            logger.warning("[EXPERIMENTAL] Running %s — outputs may be unreliable",
                           IngestorClass.source_name)

        ingestor = IngestorClass(cfg)
        try:
            result = ingestor.ingest(conn, run_id, tickers)
            results[IngestorClass.source_name] = result
            if result.get("status") in ("ok", "experimental", "skip"):
                succeeded += 1
            else:
                failed_sources += 1
        except Exception as exc:
            logger.error("Ingestor %s crashed: %s", IngestorClass.source_name, exc)
            results[IngestorClass.source_name] = {"status": "error", "error": str(exc)}
            failed_sources += 1

    logger.info(
        "Ingestion complete: %d succeeded, %d failed, %d skipped (run_id=%s)",
        succeeded, failed_sources, skipped, run_id,
    )
    return {
        "run_id": run_id,
        "universe_size": universe_count,
        "sources_succeeded": succeeded,
        "sources_failed": failed_sources,
        "sources_skipped": skipped,
        "results": results,
    }
