# Root Cause Enrichment Report

Generated: 2026-05-15 | Total rows: 973

> **Shadow-only: no production scores were changed.**

## Root Cause Group Summary

| Group | Count |
|-------|-------|
| `data_gap` | 937 |
| `scoring_gap` | 9 |
| `feature_gap` | 19 |
| `near_miss` | 4 |
| `unknown` | 4 |

## Detailed Root Cause Breakdown

| Root Cause | Group | Count | Confidence |
|------------|-------|-------|------------|
| `pre_score_history` | data_gap | 849 | high |
| `ifrs_mapping_gap` | data_gap | 72 | medium |
| `polygon_fundamentals_missing` | data_gap | 15 | medium |
| `sector_cluster_move` | feature_gap | 14 | medium |
| `low_catalyst_score` | scoring_gap | 8 | medium |
| `missing_earnings_context` | feature_gap | 5 | medium |
| `near_threshold_scored` | near_miss | 4 | medium |
| `unknown` | unknown | 4 | low |
| `sector_specific_model_gap` | scoring_gap | 1 | medium |
| `no_evidence_no_filing` | data_gap | 1 | medium |

## Incomplete Fundamentals — Subcause Breakdown

Total Incomplete-tier rows: 88

| Subcause | Count | Suggested Fix |
|----------|-------|---------------|
| `ifrs_mapping_gap` | 72 | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| `polygon_fundamentals_missing` | 15 | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| `sector_specific_model_gap` | 1 | Implement sector-adjusted scoring model for Financials/RE/Utilities (book value, FFO, rate sensitivity). |

### Top Tickers by Subcause

**ifrs_mapping_gap**: ORCL, INTC, SNDK, STX, NBIS, CRDO, WDC, SBUX, FIX, LITE
**polygon_fundamentals_missing**: DDOG, SHOP, MSTR, NET, RDDT, CRWV
**sector_specific_model_gap**: IRM

## Top Enriched Rows (true_miss / scored_missed / near_threshold)

| Ticker | Date | Window | Score | Root Cause | Group | Fix |
|--------|------|--------|-------|------------|-------|-----|
| ORCL | 2026-05-01 | 1d | 21.6 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| AIG | 2026-05-01 | 1d | 46.4 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering. Sector ETF returns are now used for attribution context. Run: python main.py data sector-diagnostics for detailed subcause breakdown. |
| CBOE | 2026-05-01 | 1d | 29.2 | `missing_earnings_context` | feature_gap | Add EPS estimates adapter; wire earnings-proximity feature to scoring. |
| DDOG | 2026-05-01 | 1d | 16.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| INTC | 2026-05-01 | 1d | 22.9 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| SNDK | 2026-05-01 | 1d | 17.9 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| STX | 2026-05-01 | 1d | 23.4 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| SHOP | 2026-05-01 | 1d | 15.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| MSTR | 2026-05-01 | 1d | 10.2 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| NET | 2026-05-01 | 1d | 0.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| NBIS | 2026-05-01 | 1d | 11.6 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| RDDT | 2026-05-01 | 1d | 21.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| CRWV | 2026-05-01 | 1d | 2.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| CRDO | 2026-05-01 | 1d | 15.4 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| LLY | 2026-05-01 | 3d | 41.5 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering. Sector ETF returns are now used for attribution context. Run: python main.py data sector-diagnostics for detailed subcause breakdown. |
| GOOGL | 2026-05-01 | 3d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| WDC | 2026-05-01 | 3d | 22.9 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| SBUX | 2026-05-01 | 3d | 26.2 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| FIX | 2026-05-01 | 3d | 27.5 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| COHR | 2026-05-01 | 3d | 38.1 | `missing_earnings_context` | feature_gap | Add EPS estimates adapter; wire earnings-proximity feature to scoring. |
| LITE | 2026-05-01 | 3d | 19.7 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| MCHP | 2026-05-01 | 3d | 10.0 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| IRM | 2026-05-01 | 3d | 26.2 | `sector_specific_model_gap` | scoring_gap | Implement sector-adjusted scoring model for Financials/RE/Utilities (book value, FFO, rate sensitivity). |
| CAT | 2026-05-01 | 3d | 34.7 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering. Sector ETF returns are now used for attribution context. Run: python main.py data sector-diagnostics for detailed subcause breakdown. |
| CIEN | 2026-05-01 | 3d | 20.2 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering. Sector ETF returns are now used for attribution context. Run: python main.py data sector-diagnostics for detailed subcause breakdown. |
| INTC | 2026-05-01 | 3d | 22.9 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| SNDK | 2026-05-01 | 3d | 17.9 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| PWR | 2026-05-01 | 3d | 26.2 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| GD | 2026-05-01 | 3d | 25.3 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| ON | 2026-05-01 | 3d | 11.9 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| AMD | 2026-05-01 | 3d | 20.7 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering. Sector ETF returns are now used for attribution context. Run: python main.py data sector-diagnostics for detailed subcause breakdown. |
| CARR | 2026-05-01 | 3d | 32.9 | `sector_cluster_move` | feature_gap | Sector move detected via peer clustering. Sector ETF returns are now used for attribution context. Run: python main.py data sector-diagnostics for detailed subcause breakdown. |
| STX | 2026-05-01 | 3d | 23.4 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| NXPI | 2026-05-01 | 3d | 21.0 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| IFNNY | 2026-05-01 | 3d | 4.7 | `no_evidence_no_filing` | data_gap | Add EFTS fallback or press-release scraper to increase filing coverage. |
| NBIS | 2026-05-01 | 3d | 11.6 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| UMC | 2026-05-01 | 3d | 0.0 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| RDDT | 2026-05-01 | 3d | 21.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| BE | 2026-05-01 | 3d | 33.0 | `unknown` | unknown | Manual investigation required. |
| CRWV | 2026-05-01 | 3d | 2.0 | `polygon_fundamentals_missing` | data_gap | Run Polygon ticker details enrichment: python main.py data enrich-ticker-details. These tickers have prices and filing dates but no market_cap — added to priority queue at P2 as polygon_fundamentals_missing_miss. |
| STM | 2026-05-01 | 3d | 26.9 | `low_catalyst_score` | scoring_gap | Investigate catalyst source coverage for this ticker and date. |
| CRDO | 2026-05-01 | 3d | 15.4 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| MTZ | 2026-05-01 | 3d | 22.5 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| ALAB | 2026-05-01 | 3d | 20.4 | `low_catalyst_score` | scoring_gap | Investigate catalyst source coverage for this ticker and date. |
| CLS | 2026-05-01 | 3d | 32.9 | `unknown` | unknown | Manual investigation required. |
| GFS | 2026-05-01 | 3d | 0.0 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |
| GOOGL | 2026-05-01 | 5d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| GOOGL | 2026-05-01 | 10d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| GOOGL | 2026-05-01 | 5d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| SNDK | 2026-05-01 | 5d | 17.9 | `ifrs_mapping_gap` | data_gap | USD-reporting IFRS filers (GFS, UMC): ifrs-full/Revenues and EPS aliases added to features/valuation.py. Non-USD filers (CVE=CAD, NOK=EUR): require FX normalisation before valuation ratios are usable. US-GAAP filers in this bucket were transiently Incomplete — no code fix needed. |

