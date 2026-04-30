# Source Validation Briefs — Wave 2

**Status:** Research complete. No implementation. Each source requires design approval before an adapter is built.

**Date:** 2026-04-30
**Sources covered:** GDELT, Benzinga, CFTC CoT, FDA Advisory Calendar, Nasdaq Data Link, Stocktwits

---

## Brief 1: GDELT (Global Database of Events, Language, and Tone)

**Signal type:** News narrative / macro sentiment

### Access Method
Bulk file download only. No REST query API for structured records. All data is published as flat compressed files on GDELT's own CDN. Google BigQuery hosts the full GDELT 2.0 dataset as a public dataset (`gdelt-bq.gdeltv2.*`) and is the only practical filtered-query interface. There is also a limited article-search API (`https://api.gdeltproject.org/api/v2/doc/doc`) that accepts keyword queries and returns article-level tone scores — the closest thing to a finance-friendly REST endpoint, but undocumented for production use.

### Auth / Cost
- Direct file download: free, no auth, no key
- BigQuery: free up to 1 TB query data/month (Google Cloud account required); $5/TB beyond that
- DOC API: free, no auth, undocumented rate limits

### Available Datasets
- **GDELT 2.0 Events** — 15-minute CSV zip; ~60 fields; CAMEO event codes, Goldstein conflict score, actor names, tone, source URLs
- **GDELT 2.0 GKG (Global Knowledge Graph)** — 15-minute CSV zip; the finance-relevant dataset; contains `Organizations`, `Themes`, `Tone`, `Persons`, GCAM sentiment dimensions

Master file list: `http://data.gdeltproject.org/gdeltv2/masterfilelist.txt` (updated continuously)

File URL pattern: `http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip`

### Use Cases
- Narrative/sentiment time series per company: aggregate GKG `Tone` and `Themes` over `Organizations` mentions
- Event detection: CAMEO codes tied to actor names (protests, regulatory actions, executive changes)
- Macro-regime signal: aggregate theme codes (`ECON_RECESSION`, `ECON_INFLATION`) across all sources

### Required Fields
From GKG: `DATE`, `DocumentIdentifier` (URL), `Organizations`, `Themes`, `Tone` (AvgTone, PositiveScore, NegativeScore), `Extras` (GCAM financial sentiment dimensions)

### Expected Cadence
Files published every 15 minutes. A single 15-minute GKG file is 80–200 MB uncompressed. One full day is ~8–15 GB uncompressed. For a targeted equity basket, BigQuery is the practical query layer; local batch processing of raw files at this volume is a significant infrastructure commitment.

### Parsing Difficulty
**Hard.** GDELT has no ticker symbols. Company identification requires fuzzy-matching `Organizations` free text to company names (e.g., "Apple" → AAPL). False positives are frequent. A name-to-ticker mapping table and string matching must be applied to every row. The `Organizations` field is a semicolon-delimited string derived from GDELT's own NLP pipeline — not a clean entity list.

### Likely v1 Role
**Skip for v1.** The file volume and entity resolution requirement add substantial infrastructure cost for an uncertain signal. Viable as a v2 supplement once the Core pipeline is stable. If prototyping, use the DOC API with company name queries rather than raw file ingestion.

---

## Brief 2: Benzinga

**Signal type:** Financial news, analyst ratings, event calendar, options flow

### Access Method
REST API. Base URL: `https://api.benzinga.com/api/v[N]/[endpoint]`. Versioning varies by endpoint (v1, v2, v2.1). JSON responses throughout. Official Python SDK available (`pip install benzinga`, GitHub: `Benzinga/benzinga-python-client`).

### Auth / Cost
- Auth: API key passed as `token=YOUR_KEY` query param or `Authorization: Bearer` header
- **No public free tier.** Benzinga is a paid B2B vendor. Pricing is not publicly listed. Trial/sandbox access available on request at `developers.benzinga.com`. Community estimates: ~$50–$150/month for basic news, scaling to thousands/month for real-time institutional feeds. Verify current pricing before committing.
- Benzinga news has historically been licensed by retail brokerages (TD Ameritrade, Robinhood); it is a production-grade source, not a hobbyist feed.

### Available Endpoints and Key Fields

| Endpoint | Fields |
|----------|--------|
| `GET /api/v2/news` | `id`, `created`, `title`, `body`, `url`, `stocks[]` (ticker list), `channels[]`, `author` |
| `GET /api/v2/analyst/ratings` | `ticker`, `analyst`, `rating_current`, `rating_prior`, `pt_current`, `pt_prior`, `action_company` (upgrade/downgrade/initiation/reiteration), `date` |
| `GET /api/v2.1/calendar/earnings` | `ticker`, `date`, `eps`, `eps_est`, `rev`, `rev_est`, `period`, `time` (BMO/AMC) |
| `GET /api/v2.1/calendar/economics` | event name, `actual`, `consensus`, `prior`, `date` |
| `GET /api/v1/signal/option_activity` | `ticker`, `expiration_date`, `strike_price`, `put_call`, `option_activity_type` (sweep/block), `sentiment`, `cost_basis`, `volume` |
| `GET /api/v2.1/calendar/conference_calls` | `ticker`, `date`, `time` |
| `GET /api/v2.1/calendar/dividends` | `ticker`, `date`, `dividend`, `dividend_prior` |

The `stocks[]` field on news articles provides pre-tagged ticker symbols — no entity resolution needed. This is the key structural advantage over GDELT.

### Use Cases
- Analyst ratings feed: upgrades, downgrades, initiations, price target changes per ticker
- News sentiment per ticker: tagged articles allow per-stock sentiment aggregation
- Earnings calendar with actual vs. consensus fields
- Unusual options activity as a flow-based signal

### Expected Cadence
Real-time during market hours for news and analyst ratings. Calendar endpoints are updated daily with forward-looking dates.

### Parsing Difficulty
**Low.** Clean JSON, consistent schema, native ticker tags. The main handling requirement is pagination and null `pt_current` for actions that don't include a price target.

### Likely v1 Role
**Useful but optional (budget-dependent).** The analyst ratings feed is the strongest product — it covers upgrades/downgrades from major sell-side firms with clean structure and is not easily replicated from free sources. If a Benzinga API key is obtained, `analyst_ratings` and `news` are v1-viable. Without a paid key, this source cannot be evaluated or used.

---

## Brief 3: CFTC Commitments of Traders (CoT)

**Signal type:** Institutional and hedge fund positioning on index futures

### Access Method
Two methods, both free:
1. **Socrata REST API** — `https://publicreporting.cftc.gov/resource/[dataset-id].json`. Supports standard Socrata query params (`$where`, `$limit`, `$order`, `$select`, `$offset`). Returns JSON or CSV.
2. **Direct file download** — Annual zip files and weekly TXT/CSV files at `https://www.cftc.gov/MarketReports/CommitmentsofTraders/`

Recommended ingestion: Socrata API with `$where=report_date_as_yyyy_mm_dd > 'YYYY-MM-DD'` for incremental weekly updates.

### Auth / Cost
Free. No API key, no account, no authentication required.

### Available Reports

| Report | Socrata Dataset ID | Best for |
|--------|--------------------|----------|
| Traders in Financial Instruments (TFF) | `gpe5-46if` | Equity index futures — best for this project |
| Legacy (Futures Only) | `6dca-aqww` | Older broad positioning |
| Disaggregated (Futures Only) | `kh3c-gbw2` | Commodity/commercial breakdown |

**The TFF report is the correct one for equity basket analysis.** It covers E-mini S&P 500 (ES), E-mini NASDAQ-100 (NQ), E-mini Russell 2000 (RTY), and E-mini Dow Jones (YM).

### Key Fields (TFF)

| Field | Description |
|-------|-------------|
| `report_date_as_yyyy_mm_dd` | Friday publication date |
| `as_of_date_in_form_yyyy_mm_dd` | Tuesday position-of-record date |
| `market_and_exchange_names` | Contract name (e.g., "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE") |
| `dealer_positions_long_all` / `_short_all` | Dealer/Intermediary longs and shorts |
| `asset_mgr_positions_long_all` / `_short_all` | Asset Manager (institutional) longs and shorts |
| `lev_money_positions_long_all` / `_short_all` | Leveraged Funds (hedge funds) longs and shorts |
| `nonrept_positions_long_all` / `_short_all` | Small speculators |
| `open_interest_all` | Total open interest |
| `*_changes_*` | Week-over-week deltas for all position fields |
| `*_percent_oi_*` | Position fields as % of open interest |
| `*_traders_*` | Number of reporting traders per category |

Net positioning (e.g., Managed Money Net = `lev_money_positions_long_all` − `lev_money_positions_short_all`) is not pre-computed and requires a one-line calculation.

### Expected Cadence
Weekly. Published every Friday at approximately 3:30 PM Eastern. Reflects positions as of the prior Tuesday's close — 3–4 business day lag. No intraday updates.

### Parsing Difficulty
**Low.** The Socrata JSON API returns consistently named fields. The main handling requirement is the long field names (40–60 characters) and computing net positioning as a derived field. Historical data goes back to 2010 for TFF, earlier for Legacy.

### Likely v1 Role
**Useful but optional (macro overlay).** CoT is index-futures-only — it does not cover single stocks. It is best used as a slow-moving regime indicator: e.g., tracking whether hedge funds (Leveraged Funds) are historically net long or short S&P 500 futures as a macro risk signal. The 3-day lag and weekly cadence make it unsuitable for intraday or short-horizon signals. Clean, free, and stable — low implementation cost for moderate signal value.

---

## Brief 4: FDA Advisory Committee Calendar

**Signal type:** Biotech/pharma event detection — regulatory meeting dates

### Access Method
Two paths:

**Path A — FDA.gov HTML scrape:**
`https://www.fda.gov/advisory-committees/committees-and-meeting-materials/calendar-upcoming-advisory-committee-meetings`
Plain HTML table. No structured download, no iCal feed. Table structure is inconsistent across committee pages. Fragile to layout changes.

**Path B — Federal Register API (recommended):**
`https://www.federalregister.gov/api/v1/documents.json`
Free REST API. Query by `conditions[agencies][]=food-and-drug-administration&conditions[type]=NOTICE`. Returns structured JSON with document metadata. The FDA is legally required to publish advisory committee notices in the Federal Register ≥15 days before the meeting — this is the upstream primary source.

### Auth / Cost
- FDA.gov: free, no auth
- Federal Register API: free, no auth, no key required
- Third-party structured feeds (BioPharma Catalyst, Evaluate Pharma): paid, ~$thousands/year

### Key Fields

From Federal Register API:
- `document_number`, `publication_date`, `title`, `abstract`, `full_text_url`, `agencies`
- Meeting date and drug/product name are embedded in `title` and `abstract` as free text — not discrete fields. Require regex or NLP extraction.

From FDA HTML (scraped):
- Meeting date, committee name, topic/drug name, meeting format (in-person/virtual), Federal Register notice link

### Expected Cadence
Irregular. New notices appear on weekday mornings when the Federal Register publishes them. Typical lead time: 2–8 weeks before the meeting. The Federal Register API is effectively same-day as print publication.

### Parsing Difficulty
**Medium (Federal Register API) / Medium-High (FDA HTML scrape).**
The Federal Register API is a stable, well-structured JSON endpoint. The difficulty lies in extracting the meeting date and drug/product name from free-text `title` and `abstract` fields. A regex or small NLP step is required. The FDA HTML scrape is harder — table structure varies by committee and is fragile.

### Use Cases
- Pre-meeting signal for biotech/pharma tickers awaiting FDA review
- Event-risk flagging for positions in PDUFA-adjacent names
- Meeting cancellation/postponement detection (requires change detection on a prior pulled state)

For the current equity basket (AAPL, NVDA, TSLA, JPM, UBER, RKLB), none are directly FDA-subject. This source becomes relevant only if the basket expands to include pharma/biotech names.

### Likely v1 Role
**Skip for current basket / Useful but optional for pharma-extended basket.** The Federal Register API is clean and free. The implementation cost is moderate (text parsing for meeting dates and drug names, plus a ticker→drug-name cross-reference table). For v1 with the current equity basket, there is no applicable signal. Include in scope only if the validated basket expands to cover pharma names.

---

## Brief 5: Nasdaq Data Link (formerly Quandl)

**Signal type:** Fundamentals, price history, macro factors, short interest (structured datasets)

### Access Method
REST API. Base URL: `https://data.nasdaq.com/api/v3/`

Two endpoint types:
- **Time-series:** `GET /api/v3/datasets/{DB}/{DATASET}/data.json?api_key=KEY`
- **Datatable (relational, filtered):** `GET /api/v3/datatables/{VENDOR}/{TABLE}.json?api_key=KEY&ticker=AAPL`

Response formats: JSON (default) or CSV (append `&format=csv`). Both are clean and well-structured. Official Python package: `pip install nasdaq-data-link` (formerly `quandl`).

### Auth / Cost
API key required for all calls (free signup at `https://data.nasdaq.com/sign-up`). Unauthenticated calls: ~50/day, functionally unusable. Key is passed as `api_key=` query parameter.

| Dataset | Cost |
|---------|------|
| Free tier (FRED, USTREASURY, MULTPL, AAII, ML, ODA, EIA) | Free with key |
| **Sharadar SF1** — 200+ fundamental items, 14,000+ US equities | Paid ~$50–$150/month (verify current pricing) |
| **Sharadar SEP** — Adjusted EOD equity prices | Paid ~$30–$50/month |
| **Sharadar SP500** — S&P 500 constituent history | Paid (bundled or separate) |
| FINRA short interest (FINRA/FNYX, FINRA/FNSQ) | Paid (same data as FINRA CDN, cleaner API wrapper) |
| Zacks earnings estimates / surprises | Paid |

**WIKI/PRICES (free EOD equity prices) was permanently discontinued in April 2018.** Any reference to WIKI prices is stale.

### Free Datasets (Relevant to This Project)

| Code | Description | Overlap |
|------|-------------|---------|
| `FRED/` | Federal Reserve macro series | Duplicates FRED adapter |
| `USTREASURY/YIELD` | Daily Treasury yield curve | Partial FRED overlap |
| `MULTPL/SHILLER_PE_RATIO_MONTH` | Shiller P/E ratio (monthly) | Unique |
| `AAII/AAII_SENTIMENT` | Weekly retail investor sentiment survey | Unique |
| `ML/AAAEY` etc. | BofA credit spread indices | Unique |

The AAII sentiment survey and BofA credit spreads are genuinely non-overlapping with Wave 1 sources and require minimal engineering effort to ingest.

### Key Fields (Sharadar SF1 — if subscribed)

Datatable with 200+ columns including: `ticker`, `calendardate`, `datekey`, `reportperiod`, `revenue`, `netinc`, `eps`, `dps`, `fcf`, `de` (debt/equity), `pe1` (P/E), `evebitda`, `grossmargin`, `currentratio`, `workingcapital`, and ~190 more. Clean, point-in-time corrected, backfill-safe.

### Expected Cadence
- Free datasets: daily to monthly depending on source
- Sharadar SF1: updated within 1–2 days of SEC filing; quarterly/annual cadence per company
- Rate limits: 300 calls/10 seconds, 2,000 calls/10 minutes (authenticated)

### Parsing Difficulty
**Low.** One of the cleanest financial data APIs in the market. JSON responses have consistent schema; `dataset_data.data` is row-major with a `column_names` header. Datatable responses are similarly structured. The Python SDK handles this transparently.

### Likely v1 Role
**Core (if Sharadar is subscribed) / Fallback only (free tier only).** On the free tier, all macro datasets overlap with the existing FRED adapter — there is no net new value. The AAII sentiment and credit spread series are small additions but don't justify an adapter alone. The Sharadar SF1 fundamentals dataset is the primary reason to engage Nasdaq Data Link — it provides clean, point-in-time corrected fundamentals that are significantly more ergonomic than EDGAR XBRL parsing. If a Sharadar subscription is obtained, this source should be re-evaluated as Core for the fundamentals use case.

---

## Brief 6: Stocktwits

**Signal type:** Retail equity sentiment (user-tagged bullish/bearish on posts)

### Access Method
REST API (polling). No free streaming. Base URL: `https://api.stocktwits.com/api/2/`

Key endpoints:
- `GET /streams/symbol/{symbol}.json` — 30 most recent messages for a ticker (cursor-paginated via `since` / `max` message ID params)
- `GET /streams/trending.json` — trending symbols
- `GET /graph/symbols/{symbol}.json` — symbol metadata including message volume and sentiment counts
- `GET /search/symbols.json?q={query}` — symbol search

### Auth / Cost
- **Unauthenticated:** ~200 requests/hour (community-reported, not officially documented). Limited but technically usable for a small basket.
- **OAuth 2.0 app registration:** Free. Register at `https://stocktwits.com/developers/apps/new`. Raises rate limit to ~400 requests/hour (community-reported). Required for user-stream endpoints.
- **Bulk/historical data:** Enterprise licensing deal with Stocktwits directly — no public pricing. The free and OAuth tiers provide no historical backfill beyond paginating backwards through recent messages.

### Key Fields Per Message

```
id, body, created_at,
user.username, user.followers, user.following, user.watchlist_stocks_count,
entities.sentiment.basic  ("Bullish" | "Bearish" | null),
entities.symbols[].symbol,
reshares_count, likes_count
```

`entities.sentiment.basic` is **user-tagged** — the author explicitly selects Bullish or Bearish. It is not algorithmically inferred. Approximately 30–40% of messages carry a tag; the remainder are null and require separate NLP if sentiment is needed.

### Expected Cadence
Polling only on free/OAuth tier. Each call returns 30 messages. For a 6-ticker basket at ~400 req/hour, a full cycle completes every ~1 minute — adequate for near-real-time monitoring. For a 50+ ticker universe, cycle time degrades significantly.

### API Stability
**Uncertain.** The Stocktwits API v2 endpoints are technically active as of mid-2025 but the developer documentation had not been meaningfully updated in several years. Stocktwits underwent an ownership change. Community reports from late 2024 indicate stricter unauthenticated rate limiting compared to prior years. There is meaningful risk of further restrictions or deprecation without notice.

### Parsing Difficulty
**Low.** Clean JSON, consistent schema, simple cursor pagination. The `entities.sentiment.basic` field is either a string (`"Bullish"`, `"Bearish"`) or absent.

### Signal Quality
Mixed. Academic research finds statistically significant but small predictive power for short-horizon returns, particularly for small/mid-cap stocks. Key concerns:
- User base skews retail day-traders; posts often follow rather than precede price moves
- ~60–70% of messages have no sentiment tag
- Message volume spikes may be a more actionable signal than sentiment direction

### Likely v1 Role
**Skip for v1.** API stability is uncertain, historical data is unavailable on the free tier, signal is noisy, and the basket (AAPL, NVDA, TSLA, JPM, UBER, RKLB) skews large-cap where Stocktwits' retail sentiment signal is weakest. Revisit for v2 if the basket expands to include small/mid-cap names where retail sentiment carries more weight, and only if API stability improves.

---

## Summary Table

| Source | Access | Auth | Cost | Cadence | Parsing | Likely v1 Role |
|--------|--------|------|------|---------|---------|----------------|
| GDELT | Bulk file download / BigQuery | None / GCloud account | Free (BigQuery: $5/TB after 1TB/mo) | 15 min | Hard (entity resolution) | Skip v1 |
| Benzinga | REST API | API key (paid) | Paid; no free tier; pricing on request | Real-time | Low | Useful but optional (budget-dependent) |
| CFTC CoT | REST API (Socrata) + CSV | None | Free | Weekly (Fri, 3-day lag) | Low | Useful but optional (macro overlay) |
| FDA Adv. Calendar | Federal Register API + HTML scrape | None | Free | Irregular (weekday mornings) | Medium | Skip v1 (current basket); Optional if pharma added |
| Nasdaq Data Link | REST API | API key (free signup) | Free tier limited; Sharadar ~$50–$150/mo | Daily–quarterly | Low | Core if Sharadar subscribed; Fallback only on free tier |
| Stocktwits | REST API (polling) | OAuth (free) | Free tier; bulk = enterprise deal | Polling (~30 msgs/call) | Low | Skip v1 |

---

## Implementation Priority Recommendation

Based solely on signal value, cost, and engineering feasibility from the research above:

1. **CFTC CoT** — Highest priority for free implementation. Clean Socrata API, no auth, weekly cadence, direct equity index positioning data. Low effort, real signal.
2. **Nasdaq Data Link (free tier only)** — AAII sentiment and credit spread series are quick wins with existing FRED-style adapter pattern. Full value locked behind Sharadar subscription.
3. **Benzinga** — Highest signal potential (analyst ratings, options flow) but requires a paid key before any validation is possible. Evaluate if budget is available.
4. **FDA Advisory Calendar** — Low cost via Federal Register API, niche signal. Only relevant if the equity basket expands to pharma/biotech.
5. **GDELT** — Real signal but high infrastructure cost. Defer until v2.
6. **Stocktwits** — Uncertain API, thin free-tier data, noisy signal for large-caps. Defer indefinitely unless specific use case emerges.
