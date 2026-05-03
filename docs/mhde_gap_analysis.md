# MHDE Gap Analysis — Prediction-vs-Actual Spike Loop

_Generated: 2026-05-03_

This document assesses what the MHDE system can support today for a full
prediction-vs-actual spike loop, identifies gaps, and recommends data sources
to close them. Based on `docs/data_inventory.md` and direct codebase review.

---

## 1. Universe Coverage — Can we scan all S&P 500 / US large-cap tickers daily?

**Status: PARTIAL**

### What works
- `universe/universe_builder.py` fetches all ~10,000 US issuers from SEC's
  `company_tickers.json` and upserts them into the `companies` table.
- The daily pipeline builds the universe fresh each run.
- Configurable cap: `universe.max_symbols` (default 500).

### Gaps
| Gap | Detail | Severity |
|-----|--------|----------|
| No S&P 500 constituent list | Universe is arbitrary SEC-ordered tickers, not S&P 500 members | High |
| No market-cap sort/filter | `companies.market_cap` column is never populated; micro-caps mix with mega-caps | High |
| `sector` / `industry` always NULL | SEC source provides no GICS codes; columns exist but are empty | Medium |
| No liquidity filter | Volume or float thresholds not applied | Medium |

### Recommended fix
See `docs/universe_sp500_brief.md` for the full implementation brief.
Short version: seed `universe/sp500_tickers.yaml` with ~503 S&P 500 tickers
and load them as `fallback_tickers` — no schema changes required.

---

## 2. Move Episode Detection — Can we detect 1d, 3d, 5d, and 10d moves?

**Status: PARTIAL (5d and 20d only)**

### What works
- `missed/detector.py` detects three event types from `prices_daily`:
  - `gain_5d_10pct` — ≥10% gain in 5 days (`GAIN_5D_THRESHOLD = 0.10`, `missed/labels.py:77`)
  - `gain_20d_20pct` — ≥20% gain in 20 days (`GAIN_20D_THRESHOLD = 0.20`, `missed/labels.py:78`)
  - `gain_60d_30pct` — ≥30% gain in 60 days (`GAIN_60D_THRESHOLD = 0.30`, `missed/labels.py:79`)
- `candidate_outcomes` stores `forward_return_1d`, `_5d`, `_20d`, `_60d`, `_120d`.
- All required price data (`prices_daily`) exists to compute any window.

### Gaps
| Gap | Detail | Severity |
|-----|--------|----------|
| No 1d episode detection | Single-day ≥X% move not detected (column exists in `candidate_outcomes`) | High |
| No 3d window | Neither detector nor `candidate_outcomes` covers 3d | Medium |
| No 10d window | Neither detector nor `candidate_outcomes` covers 10d | Medium |
| Gains only | Drawdown / down-move episodes not detected in missed pipeline | Medium |

### Recommended fix
Add `GAIN_1D_THRESHOLD = 0.05`, `GAIN_3D_THRESHOLD = 0.08`, `GAIN_10D_THRESHOLD = 0.12`
to `missed/labels.py`. Add `_detect_gains(conn, cutoff, 1, ..., "gain_1d_5pct", events)` etc.
in `missed/detector.py`. Add `forward_return_3d`, `forward_return_10d` columns to
`candidate_outcomes` via a DB migration. All source data already present.

---

## 3. Catalyst Attribution — Can we attribute direct catalysts from filings/source text?

**Status: PARTIAL (SEC filings only, LLM-dependent)**

### What works
- `filings` table holds SEC EDGAR accession numbers and doc URLs for all tickers.
- `missed/catalyst_source_resolver.py` fetches filing text from EDGAR (~50%
  coverage for text-format filings with resolvable accession numbers).
- `missed/catalyst_classifier.py` + LLM pipeline classifies catalyst type
  (merger_acquisition, regulatory, earnings, etc.) with materiality and sentiment.
- `missed daily-catalyst-queue` produces structured enrichment with shadow score projection.

### Gaps
| Gap | Detail | Severity |
|-----|--------|----------|
| Reactive only | Attribution runs post-move, not as a prospective pre-event signal | High |
| ~50% source coverage | PDF filings, missing CIK/accession numbers excluded | Medium |
| LLM API dependency | Real classification requires OpenAI or NVIDIA API key | Medium |
| No press-release text | IR page scraping is `experimental`/stub | Low |
| No earnings transcripts | Conference call text not ingested | Low |

### Recommended fix
Move SEC source resolution into the daily pipeline (pre-scoring). For coverage,
add a fallback to SEC full-text search API (EFTS) when accession number
resolution fails.

---

## 4. Sector Sympathy / Theme Momentum — Can we detect cross-ticker patterns?

**Status: MISSING**

### What works
- Nothing at the sector level. `companies.sector` / `industry` are always NULL.
- Bank/insurer detection uses XBRL concept keywords — not GICS-based grouping.
- `features/macro.py` uses FRED macro data; no sector ETF prices.

### Gaps
| Gap | Detail | Severity |
|-----|--------|----------|
| No GICS sector codes | `companies.sector` never populated from any source | High |
| No sector ETF prices | XLF, XLK, XLE, XLV, etc. not in `prices_daily` | High |
| News / theme clustering missing | GDELT and Stocktwits are `StubIngestor` — fetch nothing | High |
| No peer relative strength | No cross-ticker or sector-relative return feature | Medium |

### Recommended fix
1. Populate `companies.sector` via Polygon Ticker Details API (free tier with key)
   or a static GICS CSV seeded from Wikipedia/iShares.
2. Add sector ETF tickers (XLF, XLK, XLE, XLV, XLI, XLP, XLU, XLB, XLRE, XLC)
   to `fallback_tickers` so they are priced daily.
3. Add a `sector_momentum` feature to `features/` computing ticker return
   relative to its sector ETF over 5d/20d.

---

## 5. Prediction vs Actual Outcomes — Can we track prediction vs actual?

**Status: PARTIAL (infrastructure exists, not fully wired)**

### What works
- `candidate_outcomes` stores forward returns for every scored candidate.
- `candidate_reviews` stores structured human review (usefulness, quality, FP reason).
- `backtest_runs` and `model_runs` track historical evaluation runs.
- `learning/summarize.py` generates calibration reports from reviews + outcomes.
- `scorecard_experiments` tracks proposed and applied changes to the scoring model.

### Gaps
| Gap | Detail | Severity |
|-----|--------|----------|
| Not auto-populated daily | `candidate_outcomes` requires a separate `backtest smoke` run | High |
| No 3d / 10d outcome columns | `forward_return_3d` / `forward_return_10d` missing from schema | Medium |
| No automated precision/recall | Calibration is manual (`learning/summarize.py` report) | Medium |
| Run_id staleness | Old predictions may not yet have enough forward price history | Low |

### Recommended fix
Add `candidate_outcomes` population to the daily pipeline (after scoring,
look back 20d and label candidates with now-known forward returns).
Add `forward_return_3d`, `forward_return_10d` to schema via migration.

---

## 6. Data Gap Assessment — Required Data Sources

### 6.1 Earnings Calendar
**Status: PARTIAL**

`EventsIngestor` fetches the Nasdaq earnings calendar for ±60 days from today
(`ingestion/ingest_events.py:13-94`). Stored as `event_type="earnings"` in the
`events` table. Status: `experimental`.

**Missing:** EPS estimates, EPS actuals, surprise magnitude, revenue estimates.
These require a paid source (Alpha Vantage Premium, Polygon Financials, Zacks API).

---

### 6.2 Analyst Estimates / Revisions
**Status: MISSING**

No adapter, no schema table. Estimate revisions are a well-documented
leading catalyst signal (revision momentum).

**Recommended source:** Alpha Vantage `EARNINGS` endpoint (free tier, limited),
Polygon.io Financials API, or Zacks Research Wizard.

---

### 6.3 News Feed
**Status: STUB**

`GDELTIngestor` (`ingestion/ingest_gdelt.py`) and `StocktwitsIngestor`
(`ingestion/ingest_stocktwits.py`) both inherit from `StubIngestor` — they are
registered in `_ALL_INGESTORS` but produce zero records.

**Recommended source:** GDELT 2.0 Events API (free, no key), Polygon News API
(paid), or Benzinga News (paid). Stocktwits public endpoint is free but
rate-limited.

---

### 6.4 Sector / Industry Mappings
**Status: PARTIAL (schema exists, never populated)**

`companies.sector` and `companies.industry` exist in schema (`storage/schema.sql:15-16`)
but are always NULL. SEC's `company_tickers.json` provides no GICS codes.
`features/industry_utils.py` covers bank/insurer only via XBRL keyword matching.

**Recommended source:** Polygon Ticker Details v3 API (`sic_description`, `market`
fields — free tier, API key required). Alternatively, seed from a static GICS
mapping CSV (Wikipedia / iShares IVV holdings).

---

### 6.5 Short Interest
**Status: SUPPORTED**

`FINRAIngestor` fully implemented (`ingestion/ingest_finra.py`). Biweekly FINRA
reports populate `short_interest` table with `short_interest`, `avg_daily_volume`,
`days_to_cover`. Consumed by `features/sentiment.py`. No action needed.

---

### 6.6 Options / Implied Volatility
**Status: MISSING**

No adapter, no schema table. IV percentile is a reliable signal for identifying
elevated-uncertainty events (earnings plays, FDA dates).

**Recommended source:** Polygon Options API (~$29/mo Starter tier).
New table needed: `implied_volatility (ticker, date, iv_30d, iv_rank, put_call_ratio)`.

---

### 6.7 Institutional / Insider Transactions
**Status: PARTIAL (raw filings only, not structured)**

Form 4 (insider) and 13F (institutional) filings appear in `filings` table via
`SECIngestor` (all form types ingested). However, the filing XML is not parsed —
no structured table of transaction quantity, price, or filer identity exists.

**Recommended fix:** Add a `form4_transactions` table and a parser in
`ingestion/ingest_sec.py` that extracts `nonDerivativeTransaction` rows from
Form 4 XBRL XML.

---

### 6.8 Real-Time Volume Shock
**Status: PARTIAL (daily only)**

`features/momentum.py` computes `volume_spike` (today's volume vs 20d average)
from `prices_daily`. Daily granularity — not intraday or real-time.

For the current batch pipeline, end-of-day volume is sufficient. Intraday
alerting would require a Polygon WebSocket or IEX Cloud SSE feed (out of scope).

---

## Priority Matrix

| Item | Spike Loop Impact | Implementation Effort | Priority |
|------|-------------------|----------------------|----------|
| S&P 500 constituent YAML | High — fixes universe quality | Low (YAML + config) | P1 |
| Sector/industry mapping | High — enables sector features | Low (CSV seed or Polygon) | P1 |
| 1d/3d/10d episode detection | High — closes labelling gaps | Low (additive to detector) | P1 |
| Auto-populate `candidate_outcomes` | High — enables live calibration | Medium | P1 |
| Analyst estimates/revisions | Medium — leading signal | High (new adapter + schema) | P2 |
| News feed (GDELT/Stocktwits) | Medium — theme/sentiment signal | Medium (implement stubs) | P2 |
| Options / IV | Medium — event-uncertainty signal | High (paid API + schema) | P2 |
| Form 4 structured parser | Low — insider as lagging signal | Medium (XML parsing) | P3 |
| Intraday volume shock | Low — daily granularity sufficient | High (real-time feed) | P3 |
