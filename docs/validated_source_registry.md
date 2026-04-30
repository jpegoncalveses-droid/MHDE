# Validated Source Registry

## 1. Executive Summary

| Item | Value |
|------|-------|
| Sources validated | 7 |
| Use cases validated | 14 |
| Automated tests | 109 |
| Date of validation | 2026-04-30 |

All 7 sources were validated against a live basket of 6 equities (AAPL, NVDA, TSLA, JPM, UBER, RKLB) plus 2 ETFs (IWM, XLE) where applicable. Results are based on actual HTTP responses, not assumptions.

---

## 2. Core Sources

Six use cases across three sources scored Core. All are free-tier accessible, require no paid subscription, and returned complete, well-structured data.

### SEC EDGAR — `filings`

- **Why Core:** No authentication required. Accesses the authoritative regulatory filing index directly. All required fields present. Same-day freshness for new filings. Score: 33/35.
- **Caveats:** ETFs (IWM, XLE) have no EDGAR filings — CIK lookup returns no results for non-reporting entities. Rate limit is self-imposed (0.12s delay); SEC recommends ≤10 req/sec.

### SEC EDGAR — `fundamentals`

- **Why Core:** Same authoritative source as filings. Structured financial data (XBRL) available for 18+ years. All required fields present. Score: 33/35.
- **Caveats:** Freshness is "regulatory" — data is as current as the most recent 10-Q or 10-K. ETFs excluded. Parsing requires CIK resolution from ticker.

### FRED — `macro_series`

- **Why Core:** Official Federal Reserve data. Six key series validated (FEDFUNDS, DGS10, CPIAUCSL, UNRATE, PAYEMS, GDP). All fields present. No published rate limit. Score: 33/35.
- **Caveats:** Requires a free FRED API key (`FRED_API_KEY` env var). GDP is quarterly — worst-case lag ~90 days, within the 150-day quarterly tolerance. Freshness label is `1q` (quarterly) not daily.

### FRED — `release_calendar`

- **Why Core:** Provides upcoming publication dates for all 6 validated macro series via `/release/dates`. All 6 series returned upcoming dates. Score: 33/35.
- **Caveats:** Same API key requirement as `macro_series`. If FRED publishes no upcoming dates for a series, the adapter falls back to recent historical dates and labels the result `recent_fallback`.

### FINRA — `short_interest`

- **Why Core:** Official FINRA bi-weekly short interest data via public CDN. All 6 basket symbols found in the latest file. All required fields present. Score: 29/35.
- **Caveats:** CDN returns HTTP 403 for unpublished settlement dates (e.g., end-of-month before publication) — this is not an authentication error. Exchange-listed files only available from June 2021 onward. CSV is pipe-delimited despite `.csv` extension and requires latin-1 decoding.

### FINRA — `short_interest_history`

- **Why Core:** Same CDN as above. Four bi-weekly periods retrieved. All 6 basket symbols found across all periods. Score: 29/35.
- **Caveats:** Same CDN behavior as `short_interest`. History depth is limited to the last 4 bi-weekly periods (~2 months) in the current implementation. No direct public equivalent exists if FINRA CDN changes.

---

## 3. Useful but Optional Sources

Seven use cases scored "Useful but optional." All are accessible and return real data, but each has a constraint that prevents Core classification: rate limits, partial coverage, unreliable parsing, or unofficial API status.

### Polygon — `recent_daily_prices`

- **Why optional:** Free tier returns last 5 days of OHLCV data. Fields complete, JSON clean, easy to parse. Score: 27/35 (just below Core threshold of 28).
- **Caveats:** Free tier is rate-limited to 5 calls/min. 4 of 8 tickers hit HTTP 429 during the validation run due to quota exhaustion from prior use cases in the same session. In isolation, recent prices work reliably. A Polygon subscription would elevate this to Core.

### Alpha Vantage — `transcripts`

- **Why optional:** Earnings call transcripts available for 3 of 6 equity tickers. 2-year historical depth. Score: 23/35.
- **Caveats:** Free tier: 25 requests/day or 5/min — not viable for production use without a paid plan. ETFs excluded. Only 3/6 basket tickers returned transcript data during validation.

### Alpha Vantage — `estimates`

- **Why optional:** Consensus EPS and revenue estimates available for 6 of 6 equity tickers. 31-year history. Score: 22/35.
- **Caveats:** Same free-tier limits as transcripts (25 req/day). ETFs excluded. Requires paid plan for any production-scale use.

### Company IR — `press_releases`

- **Why optional:** IR press release pages are accessible (HTTP 200) for all tested tickers. Fields validated as present. Score: 23/35.
- **Caveats:** 0 of 4 tickers were successfully parsed during live validation. Target pages are JavaScript-heavy and return empty content to a plain HTTP client. Parse difficulty rated "hard." Results are unreliable without a JS-capable scraper or headless browser.

### Company IR — `events`

- **Why optional:** Same as press releases — IR events pages are reachable. Score: 23/35.
- **Caveats:** 0 of 1 tickers successfully parsed. Same JS rendering problem as press releases. Treating this as useful requires solving the JS parsing problem first.

### NASDAQ Earnings — `earnings_calendar`

- **Why optional:** Provides upcoming earnings dates. Daily freshness. Score: 23/35.
- **Caveats:** Unofficial/undocumented API — not a contracted data source. Only 1 of 8 tickers returned a result during validation. Data is flagged **PLANNING ONLY** — must not be used as a truth source for earnings dates. May block scrapers at any time without notice.

---

## 4. Fallback Only

### Polygon — `deep_historical_prices`

- **Why fallback only:** The free tier returns HTTP 403 for data older than ~2 years. No data was retrievable for any of the 8 tested tickers. Score: 17/35.
- **Caveats:** A Polygon paid plan (Starter tier or above) grants full historical access. On the free tier, this use case returns no data and cannot be used. EDGAR fundamentals cover historical financial data; a separate price history source would be needed if deep OHLCV history is required.

---

## 5. Reject for v1

### Polygon — `recent_snapshot`

- **Reason rejected:** The snapshot endpoint (`/v2/snapshot/locale/us/markets/stocks/tickers`) returns HTTP 403 on the free tier. Access result is `plan_limited`. No data was returned. Score: 13/35.
- **What would change the decision:** Upgrading to a Polygon paid plan. The endpoint itself is well-structured and easy to parse — the only blocker is the free-tier restriction. If a Polygon subscription is added in a future version, `recent_snapshot` should be re-evaluated and is likely to score Core.

---

## 6. Source Status Table

| Source | Use Case | Access | Status | Key Caveat | v1 Role |
|--------|----------|--------|--------|------------|---------|
| sec_edgar | filings | ok | Core | ETFs have no EDGAR filings | Required |
| sec_edgar | fundamentals | ok | Core | ETFs excluded; regulatory freshness cadence | Required |
| fred | macro_series | ok | Core | Free API key required (FRED_API_KEY) | Required |
| fred | release_calendar | ok | Core | Free API key required; fallback to recent if no upcoming dates | Required |
| finra | short_interest | ok | Core | CDN 403 = unpublished date, not auth error; exchange-listed only from June 2021 | Required |
| finra | short_interest_history | ok | Core | Same CDN behavior; 4-period window (~2 months) | Required |
| polygon | recent_daily_prices | ok | Useful but optional | Free tier: 5 req/min; last 5 days only | Supplemental |
| alpha_vantage | transcripts | ok | Useful but optional | Free tier: 25 req/day; 3/6 tickers with data | Enrichment |
| alpha_vantage | estimates | ok | Useful but optional | Free tier: 25 req/day; ETFs excluded | Enrichment |
| company_ir | press_releases | ok | Useful but optional | 0% parse success on live run; JS-heavy sites | Skip v1 |
| company_ir | events | ok | Useful but optional | 0% parse success on live run; JS-heavy sites | Skip v1 |
| nasdaq_earnings | earnings_calendar | ok | Useful but optional | Unofficial API; 1/8 tickers found; planning only | Planning only |
| polygon | deep_historical_prices | ok | Fallback only | Free tier 403 for data >2 years old | Skip v1 |
| polygon | recent_snapshot | plan_limited | Reject for v1 | Free tier 403 on snapshot endpoint | Skip v1 |

---

## 7. Architecture Implications

The following statements are derived directly from the validation results. They are constraints for any v1 architecture, not recommendations for new sources.

**SEC EDGAR, FRED, and FINRA are the Core v1 sources.** All three are publicly accessible, require no paid subscription, returned complete data, and scored ≥29/35. Any v1 architecture must treat these as the primary data layer. EDGAR provides regulatory filings and structured fundamentals. FRED provides macro context and release timing. FINRA provides bi-weekly short interest for the equity basket.

**Polygon free tier is usable only for recent daily prices.** The last 5 days of OHLCV data are accessible and well-structured. Deep historical prices (>2 years) and intraday snapshots are blocked by the free tier. No v1 component should assume Polygon can supply historical price data or real-time quotes without a paid plan.

**Alpha Vantage is useful but not production-ready on the free tier.** Transcripts and estimates data are accessible and structured, but the 25 requests/day free-tier limit prevents any production-scale use. Alpha Vantage is appropriate as an enrichment layer if a paid key is available.

**Company IR is reachable but parsing is unreliable.** Both press releases and events pages return HTTP 200 but yield 0% successful parses against a plain HTTP client. This source requires a JS-capable rendering solution before it can be treated as usable. No v1 component should depend on Company IR data being available.

**NASDAQ Earnings is planning-only.** The earnings calendar endpoint is unofficial, returned data for only 1 of 8 tested tickers, and is explicitly flagged as not a truth source. It may be used to inform scheduling or prioritization but must not drive any data-dependent logic.

**No v1 architecture should depend on rejected or optional sources as hard requirements.** Components that need price data should degrade gracefully when Polygon rate limits are hit. Components that need earnings dates must have a fallback path when the NASDAQ endpoint returns no data. The three Core sources (SEC EDGAR, FRED, FINRA) are the only ones validated as unconditionally reliable on the free tier.
