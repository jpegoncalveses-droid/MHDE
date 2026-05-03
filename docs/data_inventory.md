# MHDE Data Inventory

## Database Tables

### schema_version
- **Description**: Tracks applied DB schema migrations by version number.
- **Primary Key**: `version`
- **Source**: storage/migrations.py
- **Freshness**: on schema migration
- **Row Count**: 5
- **Consumers**: storage/migrations.py, storage/db.py

### companies
- **Description**: Master universe of equities with metadata, sector, market cap, and SEC CIK.
- **Primary Key**: `ticker`
- **Source**: ingestion/ingest_prices.py, ingestion/ingest_sec.py
- **Freshness**: daily
- **Row Count**: 510
- **Consumers**: features/feature_builder.py, features/momentum.py, features/quality.py, features/valuation.py, features/catalyst.py, scoring/scorecard.py, health/operational.py, pipelines/daily_radar.py, missed/catalyst_queue.py

### source_runs
- **Description**: Audit log of each data ingestion run: source, status, record counts, errors.
- **Primary Key**: `id`
- **Source**: ingestion/orchestrator.py
- **Freshness**: each pipeline run
- **Row Count**: 161
- **Date Range**: 2026-05-01 08:38:44.748575 → 2026-05-02 11:53:41.844528
- **Consumers**: health/operational.py, ingestion/orchestrator.py

### filings
- **Description**: SEC EDGAR filing index (10-K, 10-Q, 8-K, etc.) per ticker.
- **Primary Key**: `id`
- **Source**: SEC EDGAR XBRL API (ingestion/ingest_sec.py)
- **Freshness**: daily
- **Row Count**: 1252238
- **Date Range**: 1995-02-09 → 2026-05-01
- **Consumers**: features/catalyst.py, features/risk.py, features/filer_utils.py, missed/catalyst_sampler.py, missed/detector.py, missed/investigator.py, review/packet_builder.py, ingestion/ingest_sec.py

### fundamentals_raw
- **Description**: Raw XBRL concept values from SEC filings (revenue, net income, shares, etc.).
- **Primary Key**: `id`
- **Source**: SEC EDGAR XBRL API (ingestion/ingest_sec.py)
- **Freshness**: daily
- **Row Count**: 1058100
- **Date Range**: 1989-12-31 → 2199-12-31
- **Consumers**: features/quality.py, features/valuation.py, health/operational.py

### fundamentals_features
- **Description**: Processed fundamental features per ticker per date (revenue growth, net margin, dilution, etc.).
- **Primary Key**: `id`
- **Source**: features/quality.py, features/valuation.py
- **Freshness**: daily
- **Row Count**: 0
- **Consumers**: scoring/scorecard.py, health/operational.py, features/feature_builder.py

### prices_daily
- **Description**: Daily OHLCV price data per ticker from Stooq/Polygon.
- **Primary Key**: `id`
- **Source**: ingestion/ingest_prices.py (Stooq/Polygon)
- **Freshness**: daily
- **Row Count**: 124978
- **Date Range**: 2025-05-02 → 2026-05-01
- **Consumers**: features/momentum.py, features/valuation.py, features/risk.py, features/feature_builder.py, pipelines/daily_radar.py, backtest/historical_replay.py, backtest/labels.py, missed/detector.py, missed/investigator.py, review/packet_builder.py

### macro_series
- **Description**: Macroeconomic time series (FRED: rates, spreads, VIX, etc.).
- **Primary Key**: `id`
- **Source**: FRED API (ingestion/ingest_prices.py)
- **Freshness**: daily
- **Row Count**: 94
- **Date Range**: 2023-04-01 → 2026-04-30
- **Consumers**: features/momentum.py, health/operational.py

### short_interest
- **Description**: FINRA short interest data per ticker per settlement date.
- **Primary Key**: `id`
- **Source**: FINRA API (ingestion/ingest_finra.py)
- **Freshness**: bi-monthly (FINRA schedule)
- **Row Count**: 0
- **Consumers**: features/momentum.py, health/operational.py

### events
- **Description**: Catalytic events per ticker: earnings, FDA, conferences, insider buys, etc.
- **Primary Key**: `id`
- **Source**: ingestion/ingest_events.py (multiple APIs)
- **Freshness**: daily
- **Row Count**: 63
- **Date Range**: 2026-05-01 → 2026-06-01
- **Consumers**: features/catalyst.py, health/operational.py

### features
- **Description**: Computed feature scores per ticker per run (grouped by feature_group).
- **Primary Key**: `id`
- **Source**: features/feature_builder.py
- **Freshness**: each pipeline run
- **Row Count**: 65948
- **Consumers**: scoring/scorecard.py, health/operational.py

### scores
- **Description**: Composite scores per ticker per run (cheap, quality, catalyst, momentum, total) with tier assignment.
- **Primary Key**: `id`
- **Source**: scoring/scorecard.py
- **Freshness**: each pipeline run
- **Row Count**: 4260
- **Date Range**: 2026-05-01 → 2026-05-02
- **Consumers**: scoring/ranker.py, review/packet_builder.py, missed/catalyst_sampler.py, missed/catalyst_queue.py, missed/detector.py, missed/investigator.py, health/operational.py, health/data_quality.py, pipelines/weekly_review.py, pipelines/daily_radar.py, learning/calibration.py, main.py (shadow command)

### hypotheses
- **Description**: Investment theses for top-ranked candidates with structured evidence and status tracking.
- **Primary Key**: `hypothesis_id`
- **Source**: pipelines/daily_radar.py
- **Freshness**: each pipeline run
- **Row Count**: 1125
- **Consumers**: review/packet_builder.py, health/operational.py, learning/insights.py

### rejections
- **Description**: Tickers rejected from scoring with reasons and risk flags.
- **Primary Key**: `id`
- **Source**: scoring/tiers.py
- **Freshness**: each pipeline run
- **Row Count**: 3135
- **Consumers**: health/operational.py, pipelines/daily_radar.py

### candidate_outcomes
- **Description**: Forward return tracking for scored candidates (1d, 5d, 20d, 60d, 120d returns and drawdowns).
- **Primary Key**: `candidate_id`
- **Source**: learning/insights.py
- **Freshness**: daily (lookback fill)
- **Row Count**: 1125
- **Date Range**: 2026-05-01 → 2026-05-02
- **Consumers**: learning/insights.py, review/packet_builder.py, health/operational.py

### backtest_runs
- **Description**: Backtest summary results: hit rate, avg return, metrics per run.
- **Primary Key**: `backtest_run_id`
- **Source**: learning/insights.py
- **Freshness**: on demand / pipeline run
- **Row Count**: 1
- **Date Range**: 2026-04-30 → 2026-04-30
- **Consumers**: learning/insights.py, health/operational.py

### model_runs
- **Description**: ML model training run metadata (XGBoost, features, metrics).
- **Primary Key**: `model_run_id`
- **Source**: learning/insights.py
- **Freshness**: on demand
- **Row Count**: 0
- **Consumers**: learning/insights.py, health/operational.py

### llm_runs
- **Description**: LLM inference audit log: provider, model, prompt version, tokens, cost, input/output hashes.
- **Primary Key**: `llm_run_id`
- **Source**: features/catalyst.py, pipelines/daily_radar.py
- **Freshness**: each pipeline run
- **Row Count**: 192
- **Date Range**: 2026-05-01 08:48:50.275795 → 2026-05-02 11:55:03.122384
- **Consumers**: health/operational.py, pipelines/daily_radar.py

### alerts
- **Description**: Outbound alerts sent per ticker/channel with deduplication keys.
- **Primary Key**: `alert_id`
- **Source**: pipelines/daily_radar.py
- **Freshness**: each pipeline run
- **Row Count**: 0
- **Consumers**: health/operational.py, pipelines/daily_radar.py

### pipeline_runs
- **Description**: High-level pipeline execution log: universe size, sources, candidates scored, tier counts, status.
- **Primary Key**: `pipeline_run_id`
- **Source**: pipelines/daily_radar.py
- **Freshness**: each pipeline run
- **Row Count**: 18
- **Date Range**: 2026-05-01 → 2026-05-02
- **Consumers**: health/operational.py, health/data_quality.py, main.py

### review_notes
- **Description**: Human analyst notes attached to tickers or hypotheses.
- **Primary Key**: `note_id`
- **Source**: review/packet_builder.py (user input)
- **Freshness**: on analyst review
- **Row Count**: 0
- **Consumers**: review/packet_builder.py, health/operational.py

### dashboard_actions
- **Description**: Audit log of actions taken via the review dashboard (status changes, annotations).
- **Primary Key**: `action_id`
- **Source**: review/packet_builder.py (dashboard API)
- **Freshness**: on user action
- **Row Count**: 0
- **Consumers**: review/packet_builder.py, health/operational.py

### candidate_reviews
- **Description**: Structured analyst reviews of scored candidates: usefulness, thesis quality, evidence quality, false positive reasons.
- **Primary Key**: `review_id`
- **Source**: review/packet_builder.py (user input)
- **Freshness**: on analyst review
- **Row Count**: 7
- **Consumers**: review/packet_builder.py, learning/insights.py, health/operational.py

### scorecard_experiments
- **Description**: Proposed and tested scoring changes: hypothesis, expected effect, backtest results, approval status.
- **Primary Key**: `experiment_id`
- **Source**: learning/insights.py, scoring/scorecard.py
- **Freshness**: on experiment proposal/review
- **Row Count**: 10
- **Consumers**: learning/insights.py, health/operational.py

### missed_opportunity_events
- **Description**: Detected missed opportunity events: large moves in tickers we did or didn't catch.
- **Primary Key**: `event_id`
- **Source**: missed/catalyst_queue.py
- **Freshness**: daily
- **Row Count**: 1238
- **Date Range**: 2026-02-02 → 2026-05-01
- **Consumers**: missed/catalyst_queue.py, health/operational.py

### missed_opportunity_investigations
- **Description**: Root cause investigations for missed opportunity events, with LLM enrichment status.
- **Primary Key**: `investigation_id`
- **Source**: missed/catalyst_queue.py
- **Freshness**: on investigation
- **Row Count**: 1238
- **Date Range**: 2026-02-02 → 2026-05-01
- **Consumers**: missed/catalyst_queue.py, health/operational.py

### missed_opportunity_root_causes
- **Description**: Individual root cause records per missed opportunity investigation.
- **Primary Key**: `rc_id`
- **Source**: missed/catalyst_queue.py
- **Freshness**: on investigation
- **Row Count**: 3728
- **Date Range**: 2026-02-02 → 2026-05-01
- **Consumers**: missed/catalyst_queue.py, health/operational.py

### promotion_gate_results
- **Description**: Go/no-go gate results for promoting model or scoring experiments to production.
- **Primary Key**: `gate_result_id`
- **Source**: learning/insights.py
- **Freshness**: on promotion evaluation
- **Row Count**: 0
- **Consumers**: learning/insights.py, health/operational.py

### health_checks
- **Description**: Operational health check results: check name, status, severity, message.
- **Primary Key**: `id`
- **Source**: health/data_quality.py, health/operational.py
- **Freshness**: each pipeline run
- **Row Count**: 1446
- **Date Range**: 2026-05-01 08:48:50.435822 → 2026-05-02 12:24:41.451898
- **Consumers**: main.py, health/operational.py, health/data_quality.py

## Flat Files

### daily_catalyst_queue.html
- **Format**: HTML
- **Description**: Current daily catalyst queue rendered as HTML for browser review.
- **Size**: 6781 bytes
- **Row Count**: 78
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_llm_pilot_enriched.jsonl
- **Format**: JSONL
- **Description**: LLM pilot study enriched candidates for catalyst scoring evaluation.
- **Size**: 66302 bytes
- **Row Count**: 100
- **Consumers**: features/catalyst.py, .claude/local_scripts/regen_daily_queue.py

### catalyst_shadow_score_report.md
- **Format**: MD
- **Description**: Summary report of catalyst shadow scoring results.
- **Size**: 3779 bytes
- **Row Count**: 59
- **Consumers**: scoring/scorecard.py

### catalyst_llm_pilot_sample.jsonl
- **Format**: JSONL
- **Description**: Sample of candidates used in the catalyst LLM pilot study.
- **Size**: 610274 bytes
- **Row Count**: 100
- **Consumers**: features/catalyst.py

### daily_catalyst_queue.md
- **Format**: MD
- **Description**: Current daily catalyst queue rendered as Markdown for human review.
- **Size**: 3877 bytes
- **Row Count**: 67
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_llm_pilot_review.csv
- **Format**: CSV
- **Description**: Human review results for the catalyst LLM pilot study.
- **Size**: 18406 bytes
- **Row Count**: 50
- **Consumers**: features/catalyst.py, learning/insights.py

### catalyst_shadow_score_rows.csv
- **Format**: CSV
- **Description**: Shadow scoring rows for catalyst model evaluation.
- **Size**: 4200 bytes
- **Row Count**: 17
- **Consumers**: scoring/scorecard.py, features/catalyst.py

### catalyst_near_threshold_enriched.jsonl
- **Format**: JSONL
- **Description**: Near-threshold candidates enriched by LLM for calibration analysis.
- **Size**: 36496 bytes
- **Row Count**: 50
- **Consumers**: features/catalyst.py, scoring/scorecard.py

### daily_catalyst_queue.csv
- **Format**: CSV
- **Description**: Current daily catalyst queue as a flat CSV for spreadsheet review.
- **Size**: 29048 bytes
- **Row Count**: 43
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### daily_catalyst_queue_enriched.jsonl
- **Format**: JSONL
- **Description**: Current daily catalyst queue with LLM-enriched summaries per candidate.
- **Size**: 31464 bytes
- **Row Count**: 43
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py, .claude/local_scripts/regen_daily_queue.py

### daily_catalyst_queue_cache.jsonl
- **Format**: JSONL
- **Description**: LLM response cache for the daily catalyst queue to avoid redundant API calls.
- **Size**: 33758 bytes
- **Row Count**: 50
- **Consumers**: missed/catalyst_queue.py, .claude/local_scripts/regen_daily_queue.py

### catalyst_queue_history/2026-05-03/daily_catalyst_queue.html
- **Format**: HTML
- **Description**: Current daily catalyst queue rendered as HTML for browser review.
- **Size**: 6781 bytes
- **Row Count**: 78
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_queue_history/2026-05-03/daily_catalyst_queue.md
- **Format**: MD
- **Description**: Current daily catalyst queue rendered as Markdown for human review.
- **Size**: 3877 bytes
- **Row Count**: 67
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_queue_history/2026-05-03/daily_catalyst_queue.csv
- **Format**: CSV
- **Description**: Current daily catalyst queue as a flat CSV for spreadsheet review.
- **Size**: 29048 bytes
- **Row Count**: 43
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_queue_history/2026-05-03/run_metadata.json
- **Format**: JSON
- **Description**: Run metadata JSON for historical catalyst queue runs.
- **Size**: 229 bytes
- **Row Count**: 11
- **Consumers**: missed/catalyst_queue.py

### catalyst_queue_history/2026-05-03/manual_review.csv
- **Format**: CSV
- **Description**: Historical manual review CSV files archived by date.
- **Size**: 60 bytes
- **Row Count**: 0
- **Consumers**: missed/catalyst_queue.py

### catalyst_queue_history/2026-05-03/daily_catalyst_queue_enriched.jsonl
- **Format**: JSONL
- **Description**: Current daily catalyst queue with LLM-enriched summaries per candidate.
- **Size**: 31464 bytes
- **Row Count**: 43
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py, .claude/local_scripts/regen_daily_queue.py

### catalyst_queue_history/2026-05-02/daily_catalyst_queue.html
- **Format**: HTML
- **Description**: Current daily catalyst queue rendered as HTML for browser review.
- **Size**: 5313 bytes
- **Row Count**: 75
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_queue_history/2026-05-02/daily_catalyst_queue.md
- **Format**: MD
- **Description**: Current daily catalyst queue rendered as Markdown for human review.
- **Size**: 3424 bytes
- **Row Count**: 63
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_queue_history/2026-05-02/daily_catalyst_queue.csv
- **Format**: CSV
- **Description**: Current daily catalyst queue as a flat CSV for spreadsheet review.
- **Size**: 12663 bytes
- **Row Count**: 43
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py

### catalyst_queue_history/2026-05-02/run_metadata.json
- **Format**: JSON
- **Description**: Run metadata JSON for historical catalyst queue runs.
- **Size**: 229 bytes
- **Row Count**: 11
- **Consumers**: missed/catalyst_queue.py

### catalyst_queue_history/2026-05-02/manual_review.csv
- **Format**: CSV
- **Description**: Historical manual review CSV files archived by date.
- **Size**: 169 bytes
- **Row Count**: 2
- **Consumers**: missed/catalyst_queue.py

### catalyst_queue_history/2026-05-02/daily_catalyst_queue_enriched.jsonl
- **Format**: JSONL
- **Description**: Current daily catalyst queue with LLM-enriched summaries per candidate.
- **Size**: 31467 bytes
- **Row Count**: 43
- **Consumers**: missed/catalyst_queue.py, pipelines/daily_radar.py, .claude/local_scripts/regen_daily_queue.py
