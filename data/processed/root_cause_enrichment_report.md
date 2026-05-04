# Root Cause Enrichment Report

Generated: 2026-05-04 | Total rows: 1518

> **Shadow-only: no production scores were changed.**

## Root Cause Group Summary

| Group | Count |
|-------|-------|
| `data_gap` | 1482 |
| `scoring_gap` | 9 |
| `feature_gap` | 16 |
| `near_miss` | 4 |
| `unknown` | 7 |

## Detailed Root Cause Breakdown

| Root Cause | Group | Count | Confidence |
|------------|-------|-------|------------|
| `pre_score_history` | data_gap | 1394 | high |
| `ifrs_mapping_gap` | data_gap | 72 | medium |
| `sector_cluster_move` | feature_gap | 15 | medium |
| `polygon_fundamentals_missing` | data_gap | 15 | medium |
| `low_catalyst_score` | scoring_gap | 8 | medium |
| `unknown` | unknown | 7 | low |
| `near_threshold_scored` | near_miss | 4 | medium |
| `missing_earnings_context` | feature_gap | 1 | medium |
| `sector_specific_model_gap` | scoring_gap | 1 | medium |
| `no_evidence_no_filing` | data_gap | 1 | medium |

## Incomplete Fundamentals — Subcause Breakdown

Total Incomplete-tier rows: 88

| Subcause | Count | Suggested Fix |
|----------|-------|---------------|
| `ifrs_mapping_gap` | 72 | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| `polygon_fundamentals_missing` | 15 | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| `sector_specific_model_gap` | 1 | Implement sector-adjusted scoring model for Financials/RE/Utilities (book value, FFO, rate sensitivity). |

### Top Tickers by Subcause

**ifrs_mapping_gap**: ORCL, INTC, SNDK, STX, NBIS, CRDO, WDC, SBUX, FIX, LITE
**polygon_fundamentals_missing**: DDOG, SHOP, MSTR, NET, RDDT, CRWV
**sector_specific_model_gap**: IRM

## Top Enriched Rows (true_miss / scored_missed / near_threshold)

| Ticker | Date | Window | Score | Root Cause | Group | Fix |
|--------|------|--------|-------|------------|-------|-----|
| ORCL | 2026-05-01 | 1d | 21.6 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| AIG | 2026-05-01 | 1d | 46.4 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring. |
| CBOE | 2026-05-01 | 1d | 29.2 | `missing_earnings_context` | feature_gap | Add EPS estimates adapter; wire earnings-proximity feature to scoring. |
| DDOG | 2026-05-01 | 1d | 16.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| INTC | 2026-05-01 | 1d | 22.9 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| SNDK | 2026-05-01 | 1d | 17.9 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| STX | 2026-05-01 | 1d | 23.4 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| SHOP | 2026-05-01 | 1d | 15.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| MSTR | 2026-05-01 | 1d | 10.2 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| NET | 2026-05-01 | 1d | 0.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| NBIS | 2026-05-01 | 1d | 11.6 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| RDDT | 2026-05-01 | 1d | 21.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| CRWV | 2026-05-01 | 1d | 2.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| CRDO | 2026-05-01 | 1d | 15.4 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| LLY | 2026-05-01 | 3d | 41.5 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring. |
| GOOGL | 2026-05-01 | 3d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| WDC | 2026-05-01 | 3d | 22.9 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| SBUX | 2026-05-01 | 3d | 26.2 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| FIX | 2026-05-01 | 3d | 27.5 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| COHR | 2026-05-01 | 3d | 38.1 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring. |
| LITE | 2026-05-01 | 3d | 19.7 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| MCHP | 2026-05-01 | 3d | 10.0 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| IRM | 2026-05-01 | 3d | 26.2 | `sector_specific_model_gap` | scoring_gap | Implement sector-adjusted scoring model for Financials/RE/Utilities (book value, FFO, rate sensitivity). |
| CAT | 2026-05-01 | 3d | 34.7 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring. |
| CIEN | 2026-05-01 | 3d | 20.2 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring. |
| INTC | 2026-05-01 | 3d | 22.9 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| SNDK | 2026-05-01 | 3d | 17.9 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| PWR | 2026-05-01 | 3d | 26.2 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| GD | 2026-05-01 | 3d | 25.3 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| ON | 2026-05-01 | 3d | 11.9 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| AMD | 2026-05-01 | 3d | 20.7 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring. |
| CARR | 2026-05-01 | 3d | 32.9 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring. |
| STX | 2026-05-01 | 3d | 23.4 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| NXPI | 2026-05-01 | 3d | 21.0 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| IFNNY | 2026-05-01 | 3d | 4.7 | `no_evidence_no_filing` | data_gap | Add EFTS fallback or press-release scraper to increase filing coverage. |
| NBIS | 2026-05-01 | 3d | 11.6 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| UMC | 2026-05-01 | 3d | 0.0 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| RDDT | 2026-05-01 | 3d | 21.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| BE | 2026-05-01 | 3d | 33.0 | `unknown` | unknown | Manual investigation required. |
| CRWV | 2026-05-01 | 3d | 2.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. |
| STM | 2026-05-01 | 3d | 26.9 | `low_catalyst_score` | scoring_gap | Investigate catalyst source coverage for this ticker and date. |
| CRDO | 2026-05-01 | 3d | 15.4 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| MTZ | 2026-05-01 | 3d | 22.5 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| ALAB | 2026-05-01 | 3d | 20.4 | `low_catalyst_score` | scoring_gap | Investigate catalyst source coverage for this ticker and date. |
| CLS | 2026-05-01 | 3d | 32.9 | `unknown` | unknown | Manual investigation required. |
| GFS | 2026-05-01 | 3d | 0.0 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |
| GOOGL | 2026-05-01 | 5d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| GOOGL | 2026-05-01 | 10d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| GOOGL | 2026-05-01 | 5d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| SNDK | 2026-05-01 | 5d | 17.9 | `ifrs_mapping_gap` | data_gap | Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names. |

