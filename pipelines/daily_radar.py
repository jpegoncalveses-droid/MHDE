from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

import duckdb

logger = logging.getLogger("mhde.pipelines.daily_radar")


@dataclass
class RunSummary:
    run_id: str
    run_date: date
    universe_size: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    sources_skipped: int = 0
    candidates_scored: int = 0
    tier_a: int = 0
    tier_b: int = 0
    tier_c: int = 0
    rejected: int = 0
    alerts_sent: int = 0
    llm_provider: str = "mock"
    report_path: str = ""
    warnings: list[str] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)


def _stage(name: str, summary: RunSummary):
    """Context manager that logs stage start/end with elapsed time."""
    import contextlib
    import time

    @contextlib.contextmanager
    def _ctx():
        t0 = time.monotonic()
        logger.info(">>> Stage: %s", name)
        try:
            yield
        finally:
            elapsed = time.monotonic() - t0
            summary.stage_timings[name] = round(elapsed, 2)
            logger.info("<<< Stage: %s — %.1fs", name, elapsed)

    return _ctx()


def run(cfg: dict, conn: duckdb.DuckDBPyConnection) -> RunSummary:
    run_id = uuid.uuid4().hex[:16]
    summary = RunSummary(run_id=run_id, run_date=date.today())

    logger.info("=== MHDE Daily Radar — run_id=%s ===", run_id)

    # Step 1: Build / refresh universe
    with _stage("universe", summary):
        try:
            from universe.universe_builder import build_universe
            build_universe(conn, cfg)
            rows = conn.execute(
                "SELECT COUNT(*) FROM companies WHERE is_active = true"
            ).fetchone()
            summary.universe_size = rows[0] if rows else 0
            summary.warnings.append(
                "Universe selection is name-filtered only (no market cap ranking)"
            )
        except Exception as exc:
            logger.error("Universe build failed: %s", exc)
            summary.warnings.append(f"Universe build failed: {exc}")

    # ORDER BY universe_tier DESC: see ingestion/orchestrator.py for the full
    # rationale (ADR-031 / KI-143). The list built here is what daily-radar
    # passes as `tickers_override` to ingestion/orchestrator.py:run_all, which
    # short-circuits the orchestrator's own SELECT — so this is the call site
    # that actually reaches production. Both queries must stay byte-identical
    # until KI-144 (shared helper) consolidates them.
    tickers = [
        r[0] for r in conn.execute(
            "SELECT ticker FROM companies WHERE is_active = true "
            "ORDER BY universe_tier DESC, ticker"
        ).fetchall()
    ]
    max_symbols = cfg.get("universe", {}).get("max_symbols")
    if max_symbols and len(tickers) > max_symbols:
        tickers = tickers[:max_symbols]
        logger.info("Dev mode: capped tickers to %d (universe has %d)", max_symbols, summary.universe_size)
    if not tickers:
        logger.warning("Empty universe — radar has no candidates to score")
        summary.warnings.append("Universe is empty — run ingestion first")

    # Step 2: Ingest data
    with _stage("ingestion", summary):
        try:
            from ingestion.orchestrator import run_all
            ingest_result = run_all(conn, cfg, target="all", dry_run=False, run_id=run_id,
                                    tickers_override=tickers if tickers else None)
            summary.sources_succeeded = ingest_result.get("sources_succeeded", 0)
            summary.sources_failed = ingest_result.get("sources_failed", 0)
            summary.sources_skipped = ingest_result.get("sources_skipped", 0)
        except Exception as exc:
            logger.error("Ingestion failed: %s", exc)
            summary.warnings.append(f"Ingestion error: {exc}")

    # Step 3: Build features
    with _stage("features", summary):
        try:
            from features.feature_builder import build_features
            build_features(conn, run_id, tickers, cfg)
        except Exception as exc:
            logger.error("Feature build failed: %s", exc)
            summary.warnings.append(f"Feature build error: {exc}")

    # Step 4: Score and rank
    with _stage("scoring", summary):
        try:
            from scoring.scorecard import compute_scores
            from scoring.ranker import rank_tickers
            compute_scores(conn, run_id, tickers, cfg)
            ranked = rank_tickers(conn, run_id)
            summary.candidates_scored = len(ranked)
            summary.tier_a = sum(1 for r in ranked if r["tier"] == "A")
            summary.tier_b = sum(1 for r in ranked if r["tier"] == "B")
            summary.tier_c = sum(1 for r in ranked if r["tier"] == "C")
            summary.rejected = sum(1 for r in ranked if r["tier"] == "Reject")
        except Exception as exc:
            logger.error("Scoring failed: %s", exc)
            summary.warnings.append(f"Scoring error: {exc}")
            ranked = []

    # Step 5: Generate hypotheses
    hypotheses = []
    with _stage("hypotheses", summary):
        try:
            from hypotheses.generator import generate_hypotheses
            from hypotheses.rejection_logger import log_rejections
            hypotheses = generate_hypotheses(conn, run_id, ranked)
            log_rejections(conn, run_id, ranked)
        except Exception as exc:
            logger.error("Hypothesis generation failed: %s", exc)
            summary.warnings.append(f"Hypothesis error: {exc}")

    # Step 6: LLM briefs
    with _stage("llm", summary):
        try:
            from llm.runner import run_briefs, _get_provider
            from llm.local_provider import MockProvider
            provider = _get_provider(cfg)
            summary.llm_provider = provider.__class__.__name__.lower().replace("provider", "")
            if isinstance(provider, MockProvider):
                summary.warnings.append("LLM running in mock mode (no API key configured)")
            run_briefs(conn, run_id, hypotheses, cfg)
        except Exception as exc:
            logger.error("LLM briefs failed: %s", exc)
            summary.warnings.append(f"LLM error: {exc}")

    # Step 7: Outcome tracking
    with _stage("outcomes", summary):
        try:
            from outcomes.tracker import create_outcome_record
            for row in ranked:
                if row["tier"] != "Reject":
                    ref_price = _get_latest_price(conn, row["ticker"])
                    create_outcome_record(
                        conn, run_id, row["ticker"], date.today(),
                        row["tier"], row["total_score"], ref_price,
                    )
            # Populate forward returns for all mature outcome windows
            from outcomes.tracker import populate_forward_returns
            populate_forward_returns(conn, as_of_date=date.today().isoformat())
        except Exception as exc:
            logger.error("Outcome tracking failed: %s", exc)

    # Step 8: Notify
    with _stage("notifications", summary):
        try:
            from notifications.telegram import TelegramNotifier
            from notifications.email import EmailNotifier
            tg = TelegramNotifier(cfg, conn)
            em = EmailNotifier(cfg, conn)
            a_tier = [h for h in hypotheses if h.get("tier") == "A"]
            for c in a_tier:
                if tg.send_alert(c):
                    summary.alerts_sent += 1
            em.send_digest({
                "run_id": run_id,
                "candidates": hypotheses,
                "sent": summary.alerts_sent,
            })
            if summary.alerts_sent == 0:
                summary.warnings.append("Alerts sent: 0 (Telegram not configured or no A-tier)")
        except Exception as exc:
            logger.error("Notifications failed: %s", exc)

    # Step 9: Generate reports
    with _stage("reports", summary):
        try:
            from reports.markdown_report import write_daily_report
            from reports.json_report import write_json_report
            report_path = write_daily_report(run_id, conn, "outputs", run_summary={
                "universe_size": summary.universe_size,
                "sources_succeeded": summary.sources_succeeded,
                "alerts_sent": summary.alerts_sent,
            })
            write_json_report(run_id, conn, "outputs")
            summary.report_path = report_path
        except Exception as exc:
            logger.error("Report generation failed: %s", exc)
            summary.warnings.append(f"Report error: {exc}")

    # Step 10: Health check
    with _stage("health", summary):
        try:
            from health.checks import run_all_checks
            health_run_id = uuid.uuid4().hex[:16]
            run_all_checks(conn, health_run_id, cfg)
        except Exception as exc:
            logger.error("Health check failed: %s", exc)

    _record_pipeline_run(conn, summary)
    _print_summary(summary)
    return summary


def _record_pipeline_run(conn: duckdb.DuckDBPyConnection, s: RunSummary) -> None:
    try:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                pipeline_run_id, run_id, run_date, pipeline_type,
                universe_size, sources_succeeded, sources_failed, sources_skipped,
                candidates_scored, tier_a, tier_b, tier_c, rejected,
                alerts_sent, llm_provider, report_path,
                warnings_json, status, finished_at
            ) VALUES (?, ?, ?, 'daily_radar', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?)
            """,
            [
                uuid.uuid4().hex[:16], s.run_id, s.run_date,
                s.universe_size, s.sources_succeeded, s.sources_failed, s.sources_skipped,
                s.candidates_scored, s.tier_a, s.tier_b, s.tier_c, s.rejected,
                s.alerts_sent, s.llm_provider, str(s.report_path) if s.report_path else None,
                json.dumps(s.warnings), datetime.utcnow(),
            ],
        )
    except Exception as exc:
        logger.warning("Could not record pipeline run: %s", exc)


def _get_latest_price(conn: duckdb.DuckDBPyConnection, ticker: str) -> float | None:
    rows = conn.execute(
        "SELECT close FROM prices_daily WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
        [ticker],
    ).fetchall()
    return rows[0][0] if rows else None


def _print_summary(s: RunSummary) -> None:
    skipped_label = f"{s.sources_skipped} skipped — no credentials" if s.sources_skipped else ""
    print(f"\nMHDE daily radar complete")
    print(f"Run ID:              {s.run_id}")
    print(f"Universe size:       {s.universe_size}")
    print(
        f"Sources succeeded:   {s.sources_succeeded} / "
        f"{s.sources_succeeded + s.sources_failed + s.sources_skipped}"
        + (f" ({skipped_label})" if skipped_label else "")
    )
    print(f"Candidates scored:   {s.candidates_scored}")
    print(f"A-tier candidates:   {s.tier_a}")
    print(f"B-tier candidates:   {s.tier_b}")
    print(f"C-tier candidates:   {s.tier_c}")
    print(f"Rejected:            {s.rejected}")
    print(f"Alerts sent:         {s.alerts_sent}")
    if s.report_path:
        print(f"Report:              {s.report_path}")

    # Stage timings
    if s.stage_timings:
        total_s = sum(s.stage_timings.values())
        print(f"\nStage timings (total {total_s:.1f}s):")
        for stage, t in s.stage_timings.items():
            bar = "█" * max(1, int(t / total_s * 20)) if total_s > 0 else ""
            print(f"  {stage:<18} {t:6.1f}s  {bar}")

    if s.warnings:
        print("\nWarnings:")
        for w in s.warnings:
            print(f"  - {w}")
