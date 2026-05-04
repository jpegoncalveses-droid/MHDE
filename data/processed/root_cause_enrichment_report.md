# Root Cause Enrichment Report

Generated: 2026-05-03 | Total rows: 1518

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
| `price_only_scored` | data_gap | 72 | low |
| `sector_cluster_move` | feature_gap | 15 | medium |
| `recent_ipo_or_short_history` | data_gap | 15 | medium |
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
| `price_only_scored` | 72 | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| `recent_ipo_or_short_history` | 15 | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| `sector_specific_model_gap` | 1 | Implement sector-adjusted scoring model for Financials/RE/Utilities (book value, FFO, rate sensitivity). |

### Top Tickers by Subcause

**price_only_scored**: INTC, ORCL, SNDK, STX, CRDO, NBIS, FIX, GD, LITE, MCHP
**recent_ipo_or_short_history**: DDOG, CRWV, MSTR, NET, RDDT, SHOP
**sector_specific_model_gap**: IRM

## Top Enriched Rows (true_miss / scored_missed / near_threshold)

| Ticker | Date | Window | Score | Root Cause | Group | Fix |
|--------|------|--------|-------|------------|-------|-----|
| AIG | 2026-05-01 | 1d | 46.4 | `sector_cluster_move` | feature_gap | Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature. |
| CBOE | 2026-05-01 | 1d | 29.2 | `missing_earnings_context` | feature_gap | Add EPS estimates adapter; wire earnings-proximity feature to scoring. |
| DDOG | 2026-05-01 | 1d | 16.0 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| INTC | 2026-05-01 | 1d | 22.9 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| ORCL | 2026-05-01 | 1d | 21.6 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| SNDK | 2026-05-01 | 1d | 17.9 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| STX | 2026-05-01 | 1d | 23.4 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| CRDO | 2026-05-01 | 1d | 15.4 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| CRWV | 2026-05-01 | 1d | 2.0 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| MSTR | 2026-05-01 | 1d | 10.2 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| NBIS | 2026-05-01 | 1d | 11.6 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| NET | 2026-05-01 | 1d | 0.0 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| RDDT | 2026-05-01 | 1d | 21.0 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| SHOP | 2026-05-01 | 1d | 15.0 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| GOOGL | 2026-05-01 | 3d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| LLY | 2026-05-01 | 3d | 41.5 | `sector_cluster_move` | feature_gap | Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature. |
| AMD | 2026-05-01 | 3d | 20.7 | `sector_cluster_move` | feature_gap | Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature. |
| CARR | 2026-05-01 | 3d | 32.9 | `sector_cluster_move` | feature_gap | Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature. |
| CAT | 2026-05-01 | 3d | 34.7 | `sector_cluster_move` | feature_gap | Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature. |
| CIEN | 2026-05-01 | 3d | 20.2 | `sector_cluster_move` | feature_gap | Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature. |
| COHR | 2026-05-01 | 3d | 38.1 | `sector_cluster_move` | feature_gap | Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature. |
| FIX | 2026-05-01 | 3d | 27.5 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| GD | 2026-05-01 | 3d | 25.3 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| INTC | 2026-05-01 | 3d | 22.9 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| IRM | 2026-05-01 | 3d | 26.2 | `sector_specific_model_gap` | scoring_gap | Implement sector-adjusted scoring model for Financials/RE/Utilities (book value, FFO, rate sensitivity). |
| LITE | 2026-05-01 | 3d | 19.7 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| MCHP | 2026-05-01 | 3d | 10.0 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| NXPI | 2026-05-01 | 3d | 21.0 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| ON | 2026-05-01 | 3d | 11.9 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| PWR | 2026-05-01 | 3d | 26.2 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| SBUX | 2026-05-01 | 3d | 26.2 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| SNDK | 2026-05-01 | 3d | 17.9 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| STX | 2026-05-01 | 3d | 23.4 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| WDC | 2026-05-01 | 3d | 22.9 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| ALAB | 2026-05-01 | 3d | 20.4 | `low_catalyst_score` | scoring_gap | Investigate catalyst source coverage for this ticker and date. |
| BE | 2026-05-01 | 3d | 33.0 | `unknown` | unknown | Manual investigation required. |
| CLS | 2026-05-01 | 3d | 32.9 | `unknown` | unknown | Manual investigation required. |
| CRDO | 2026-05-01 | 3d | 15.4 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| CRWV | 2026-05-01 | 3d | 2.0 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| GFS | 2026-05-01 | 3d | 0.0 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| IFNNY | 2026-05-01 | 3d | 4.7 | `no_evidence_no_filing` | data_gap | Add EFTS fallback or press-release scraper to increase filing coverage. |
| MTZ | 2026-05-01 | 3d | 22.5 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| NBIS | 2026-05-01 | 3d | 11.6 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| RDDT | 2026-05-01 | 3d | 21.0 | `recent_ipo_or_short_history` | data_gap | No action needed — scoring history will accumulate. Verify ticker is in universe YAML. |
| STM | 2026-05-01 | 3d | 26.9 | `low_catalyst_score` | scoring_gap | Investigate catalyst source coverage for this ticker and date. |
| UMC | 2026-05-01 | 3d | 0.0 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |
| GOOGL | 2026-05-01 | 5d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| GOOGL | 2026-05-01 | 10d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| GOOGL | 2026-05-01 | 5d | 41.7 | `near_threshold_scored` | near_miss | Calibrate threshold — consider 43.0 as a watch-list boundary. |
| GD | 2026-05-01 | 5d | 25.3 | `price_only_scored` | data_gap | Ingest last_financial_filing_date and fundamental ratios (P/E, revenue, margins) via SEC EDGAR XBRL or financials pipeline. |

