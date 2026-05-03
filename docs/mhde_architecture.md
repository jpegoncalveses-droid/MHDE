# MHDE Architecture

## System Overview

MHDE (Market Hypothesis Discovery Engine) is a daily pipeline that ingests US equity data from multiple sources, builds scoring features, ranks candidates into tiers, detects stocks that moved significantly without being flagged (missed spikes), evaluates catalyst events via LLM, tracks prediction accuracy over time, and exposes the results through two user interfaces: a Flask review server for daily operator use and a Streamlit dashboard for deep analysis. All persistent state lives in a single DuckDB file. Experiments are shadow-only by default and cannot affect production scores without an explicit feature flag enabled in configuration.

---

## Data Flow

```
Universe definition
  sp500_tickers.yaml + config/universe.yaml (modes: core / extended / research)
          |
          v
Ingestion (weekday 23:15 UTC, or on-demand)
  Polygon.io    → prices_daily, companies (market_cap, exchange, SIC)
  Alpha Vantage → fundamentals_features, fundamentals_raw, earnings_estimates
  SEC EDGAR     → filings (Form 4, 8-K), fundamentals_raw (XBRL)
  GDELT 2.0     → news articles for catalyst classification
  FRED          → macro_series (rates, spreads, VIX)
  FINRA         → short_interest
  CFTC          → CoT positioning
  Sector ETFs   → prices_daily (XLK/XLF/XLE/XLV/XLI/XLP/XLU/XLB/XLRE/XLC/XLY)
          |
          v
Feature building
  valuation  → cheap_score    (PE proxy, PS proxy, book value)
  quality    → quality_score  (revenue growth, margins, dilution)
  catalyst   → catalyst_score (insider filings, earnings surprises, news events)
  momentum   → momentum_score (price momentum, sector ETF relative strength)
  sentiment  → sentiment_score(news volume, short interest, GDELT tone)
  risk       → risk_penalty   (macro exposure, vol, CoT positioning)
  macro      → macro context  (rates, spreads, sector drift)
          |
          v
Scoring
  scorecard.py  → weighted component scores → total_score
  tiers.py      → tier assignment (A / B / C / rejected)
  ranker.py     → ranked candidate list
  explanations.py → why_ranked / why_rejected text
          |
          v
Hypotheses + Candidate Outcomes
  hypotheses table: thesis, evidence JSON, status
  candidate_outcomes table: reference prices, forward returns, hit flags
          |
          v
Missed Spike Pipeline
  detector.py           → find large moves not in the candidate set
  investigator.py       → root cause classification (14 deterministic categories)
  root_cause_enrichment.py → enrich with sector, macro, earnings context
  prediction_report.py  → prediction-vs-actual CSV and markdown report
  sector_attribution.py → sympathy move vs. idiosyncratic
  episode_tracker.py    → move episode lifecycle (open → closed → validated)
          |
          v
Catalyst Queue (daily LLM evaluation)
  catalyst_queue.py     → select candidates by score band
  catalyst_prompt.py    → build prompts
  LLM provider          → evaluate catalyst strength (OpenAI gpt-4o-mini or mock)
  catalyst_digest.py    → generate HTML + plaintext email digest
  catalyst_history/     → per-day artifacts in data/processed/catalyst_queue_history/
          |
          v
Learning Layer
  prediction_vs_actual_rows.csv     → raw prediction outcomes
  prediction_vs_actual_enriched_rows.csv → enriched with root cause labels
  prediction_vs_actual_report.md    → summary statistics (precision, recall, avg return)
  signal_governance_audit.jsonl     → append-only audit log of all proposals
          |
          v
User Interfaces
  Flask review server (port 8765) → daily operator control center
  Streamlit dashboard             → deep analysis and historical exploration
```

---

## User Interfaces

### Flask Review Server (`review/server.py`)

- **Purpose:** Daily control center for the operator. The primary touchpoint each trading day.
- **Access:** `https://mhde.duckdns.org` (HTTP Basic Auth, always-on system service)
- **Port:** 8765 (exposed via bridge relay + nginx reverse proxy)
- **Authentication:** HTTP Basic Auth (`REVIEW_UI_USERNAME` / `REVIEW_UI_PASSWORD` from `.env`)

Routes:

| Route | Purpose |
|---|---|
| `/` | Landing page with navigation |
| `/runs` | Historical run index |
| `/runs/<date>` | Full pipeline run report for a specific date |
| `/today` | Today's run summary (candidates scored, tier breakdown, warnings) |
| `/candidates` | Catalyst queue — LLM-evaluated events with review controls |
| `/moves` | Stocks with significant price moves since last run |
| `/ops` | System health: API key presence, source run outcomes, timestamps |
| `/learning` | Prediction-vs-actual accuracy metrics by tier and time window |
| `/learning/<atype>` | Artifact download for learning CSVs and reports |

### Streamlit Dashboard (`dashboard/app.py`)

- **Purpose:** Deep analysis, historical data exploration, and model diagnostics. Read-only.
- **Pages:** 17 pages including overview, scoring breakdown, missed spikes, sector attribution, learning predictions, and raw data tables.
- **Not auth-gated** in development. In production, run behind the same reverse proxy or restrict to localhost.

---

## Shadow-Only Safety Model

This is the central safety invariant of MHDE:

> **Production scoring never changes unless a feature flag is explicitly set to `true` in `config/settings.yaml`.**

How it works:

1. All experimental adjustments are implemented as optional code paths gated by `FeatureFlagRegistry.is_enabled(flag)` in `governance/feature_flags.py`.
2. All flags default to `false` in `config/settings.yaml`. The YAML comment warns that enabling a flag is a deliberate act requiring a governance proposal.
3. When a flag is disabled (the default), `apply_shadow_adjustments()` in `governance/feature_flags.py` returns `production_score == shadow_score` — no divergence.
4. When a flag is enabled, the shadow score is adjusted but the production score field is never overwritten.
5. All proposal, approval, and rollback events are written to `data/processed/signal_governance_audit.jsonl` (append-only).

Current feature flags (all disabled by default):

| Flag | Effect when enabled |
|---|---|
| `scaled_catalyst_adjustment` | Adds a scaled catalyst adjustment to the shadow score |
| `sector_momentum_boost` | Adds a sector momentum boost component |
| `earnings_surprise_boost` | Adds a boost for earnings surprises above consensus |
| `news_contract_boost` | Adds a boost for high-confidence news catalysts |
| `risk_haircut` | Subtracts a risk haircut from the shadow score |

---

## Key Modules

| Module | Responsibility | Key exports |
|---|---|---|
| `main.py` | CLI entry point — all operator commands | `main()` |
| `review/server.py` | Flask review server, all HTTP routes | Flask `app` |
| `dashboard/app.py` | Streamlit multi-page dashboard | Streamlit pages |
| `scoring/scorecard.py` | Compute weighted component scores → total_score | `score_ticker()` |
| `scoring/tiers.py` | Assign tier (A/B/C/rejected) from total_score | `assign_tier()` |
| `scoring/ranker.py` | Rank scored candidates | `rank_candidates()` |
| `scoring/explanations.py` | Generate why_ranked / why_rejected text | `build_explanation()` |
| `features/feature_builder.py` | Orchestrate all feature group builders | `build_features()` |
| `features/valuation.py` | Cheap score: PE, PS, book value proxies | `ValuationFeatures` |
| `features/quality.py` | Quality score: growth, margins, dilution | `QualityFeatures` |
| `features/catalyst.py` | Catalyst score: insider, earnings, news | `CatalystFeatures` |
| `features/momentum.py` | Momentum score: price, sector relative strength | `MomentumFeatures` |
| `features/sentiment.py` | Sentiment score: news volume, short interest | `SentimentFeatures` |
| `features/risk.py` | Risk penalty: macro exposure, volatility | `RiskFeatures` |
| `features/macro.py` | Macro context: rates, spreads, sector drift | `MacroFeatures` |
| `missed/detector.py` | Detect large price moves not flagged as candidates | `detect_missed_spikes()` |
| `missed/investigator.py` | Classify root causes of missed spikes | `investigate()` |
| `missed/root_cause_enrichment.py` | Enrich investigations with sector/macro/earnings context | `enrich_root_causes()` |
| `missed/prediction_report.py` | Generate prediction-vs-actual CSV and report | `build_report()` |
| `missed/catalyst_queue.py` | Select and LLM-evaluate daily catalyst events | `run_catalyst_queue()` |
| `missed/catalyst_digest.py` | Generate HTML + plaintext email digest | `build_digest()` |
| `missed/sector_attribution.py` | Classify moves as sympathy vs. idiosyncratic | `attribute_move()` |
| `missed/episode_tracker.py` | Track move episode lifecycle | `EpisodeTracker` |
| `missed/deterministic_catalyst_rules.py` | Rule-based catalyst classification (14 categories) | `classify_catalyst()` |
| `universe/universe_builder.py` | Build the ticker universe for each run | `build_universe()` |
| `universe/sp500_loader.py` | Load S&P 500 ticker list | `load_sp500()` |
| `universe/cik_validator.py` | Map ticker → CIK for SEC lookups | `validate_cik()` |
| `ingestion/ingest_prices.py` | Polygon OHLCV ingestor | `ingest_prices()` |
| `ingestion/ingest_sector_etfs.py` | Sector ETF daily returns | `ingest_sector_etfs()` |
| `ingestion/ingest_earnings_estimates.py` | Earnings estimates and surprises | `ingest_earnings()` |
| `ingestion/ingest_gdelt.py` | GDELT news articles | `ingest_gdelt()` |
| `ingestion/ingest_sec.py` | SEC EDGAR Form 4 and 8-K filings | `ingest_sec()` |
| `governance/feature_flags.py` | Feature flag registry and shadow adjustment logic | `FeatureFlagRegistry`, `apply_shadow_adjustments()` |
| `governance/signal_governance.py` | Proposal / approval / rollback audit log | `create_proposal()`, `approve_proposal()`, `rollback_proposal()` |
| `outcomes/tracker.py` | Populate forward returns for candidate outcomes | `populate_forward_returns()` |
| `storage/schema.sql` | DuckDB table definitions | — |
| `storage/migrations.py` | Schema migration runner | `run_migrations()` |

---

## Storage — DuckDB Schema Overview

Single file: `data/mhde.duckdb`

Key tables:

| Table | Contents |
|---|---|
| `companies` | Ticker master: name, exchange, sector, market_cap, CIK, universe tier |
| `prices_daily` | OHLCV daily prices (Polygon + Stooq + sector ETFs) |
| `fundamentals_raw` | Raw XBRL fundamentals from SEC EDGAR and Alpha Vantage |
| `fundamentals_features` | Derived fundamentals: revenue growth, margins, dilution |
| `scores` | Per-run scoring output: component scores, total_score, tier |
| `hypotheses` | Ranked investment hypotheses with thesis text and evidence JSON |
| `candidate_outcomes` | Forward returns, drawdown, runup, hit flags for each candidate |
| `missed_opportunity_events` | Detected missed spikes with return value and context flags |
| `missed_opportunity_investigations` | Root cause classification results for each missed spike |
| `missed_opportunity_root_causes` | Individual root cause entries with confidence scores |
| `earnings_estimates` | Earnings estimates, actuals, and surprise values |
| `macro_series` | FRED macro data: rates, spreads, VIX |
| `short_interest` | FINRA bi-weekly short interest data |
| `events` | Earnings calendar and press release events |
| `pipeline_runs` | Per-run metadata: universe size, source outcomes, timing |
| `source_runs` | Per-source ingest outcomes: records attempted/inserted/failed |
| `llm_runs` | LLM call log: provider, model, cost, input/output hashes |
| `scorecard_experiments` | Experiment registry for proposed scoring changes |
| `promotion_gate_results` | Quantitative gate results for promotion decisions |
| `health_checks` | System health check results |
| `alerts` | Alert log (email, webhook) |
| `review_notes` | Operator notes attached to tickers or hypotheses |
| `candidate_reviews` | Structured operator reviews of candidates |

---

## Automation — Systemd Timer Topology

All timers are system-level (not user-level) and installed under `/etc/systemd/system/`.

```
Mon–Fri 23:15 UTC
    |
    +-- mhde-daily-analysis.timer
    |       calls mhde-daily-analysis.service
    |       runs: run_mhde_daily_analysis.sh
    |       does: full pipeline (ingest → features → score → hypotheses →
    |             missed spikes → forward returns → root cause enrichment)
    |
    +-- mhde-daily-catalyst-queue.timer
            calls mhde-daily-catalyst-queue.service
            runs: run_daily_catalyst_queue.sh
            does: LLM catalyst evaluation → HTML + email digest

Always-on services (no timer):
    mhde-review-server.service  → Flask server on port 8765
    mhde-bridge-relay.service   → Unix socket relay for nginx reverse proxy
```

Both timers use `Persistent=true`, meaning if the machine was off during the scheduled time, the service will run once when it comes back online.

Service unit files and deploy instructions: `.claude/local_scripts/systemd_deploy/`
