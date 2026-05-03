# MHDE Data Sources

This document covers every external data source used by MHDE: what it provides, how it is authenticated, its rate limits, and where its configuration lives.

---

## Source Registry

| Source | What it provides | Auth | Free tier limits | Key config |
|---|---|---|---|---|
| Polygon.io | Daily OHLCV prices, ticker details (market_cap, exchange, SIC code) | `POLYGON_API_KEY` | 5 calls/min on free tier | `config/sources.yaml`, `config/settings.yaml` (polygon section) |
| Alpha Vantage | Fundamentals (income statement, balance sheet, cash flow), earnings surprises | `ALPHA_VANTAGE_API_KEY` | 25 calls/day on free tier | `config/sources.yaml`, `config/settings.yaml` (alpha_vantage section) |
| GDELT 2.0 DOC API | News articles and non-SEC catalyst events | None (free, public) | ~1 request/sec recommended | `ingestion/ingest_gdelt.py` |
| SEC EDGAR | Form 4 (insider buy/sell filings), 8-K events, XBRL fundamentals | None (free, public) | Soft rate limit (~10 req/sec); `User-Agent` header required | `config/settings.yaml` (sec_edgar section) |
| CIK validator | Maps ticker symbol → CIK number for SEC API lookups | None (free, public) | — | `universe/cik_validator.py` |
| Sector ETFs | XLK/XLF/XLE/XLV/XLI/XLP/XLU/XLB/XLRE/XLC/XLY daily returns | `POLYGON_API_KEY` (shared) | Counts against Polygon budget | `ingestion/ingest_sector_etfs.py` |
| FRED | Macro series: interest rates, credit spreads, VIX, economic calendar | `FRED_API_KEY` | 120 calls/min on free tier | `config/settings.yaml` (fred section) |
| FINRA | Short interest bi-weekly data (via public CDN, no auth) | None (free, public) | Polite crawl rate | `config/sources.yaml`, `ingestion/` |
| CFTC | Commitments of Traders (TFF and disaggregated reports) | None (free, public) | Polite crawl rate | `config/settings.yaml` (cftc section) |

---

## Source Details

### Polygon.io

**What it provides:**
- Daily OHLCV bars for the full S&P 500 universe
- Ticker details: market cap, primary exchange, SIC industry code
- Used by: `ingestion/ingest_prices.py`, `ingestion/ingest_sector_etfs.py`

**Authentication:**
- Environment variable: `POLYGON_API_KEY`
- Set in `.env` (git-ignored), loaded via `source .env` or systemd `EnvironmentFile`

**Rate limits:**
- Free tier: 5 API calls/minute
- `config/settings.yaml` sets `polygon.rate_limit_delay: 0.5` seconds between calls (120 calls/min ceiling, but the free tier cap is enforced server-side)
- For a 500-ticker universe, ingestion takes ~5–10 minutes on the free tier

**Key config:**
```yaml
# config/settings.yaml
polygon:
  base_url: https://api.polygon.io
  rate_limit_delay: 0.5
```

---

### Alpha Vantage

**What it provides:**
- Income statement, balance sheet, and cash flow statement (annual + quarterly)
- Earnings actuals vs. estimates and surprise percentages
- Used by: `ingestion/ingest_earnings_estimates.py`, and the fundamentals feature builder

**Authentication:**
- Environment variable: `ALPHA_VANTAGE_API_KEY`

**Rate limits:**
- Free tier: 25 API calls/day (hard limit enforced server-side)
- `config/settings.yaml` sets `alpha_vantage.rate_limit_delay: 12.0` seconds (5 calls/min, conservative for free tier)
- With 25 calls/day, fundamentals coverage is limited to a rotating subset of the universe

**Key config:**
```yaml
# config/settings.yaml
alpha_vantage:
  base_url: https://www.alphavantage.co
  rate_limit_delay: 12.0
```

**Operational note:** On the free tier, do not attempt to refresh fundamentals for the full 500-ticker universe in a single day. The ingestor should rotate through the universe over multiple days.

---

### GDELT 2.0 DOC API

**What it provides:**
- Global news articles with entity extraction, tone scores, and event categorization
- Used to detect non-SEC catalyst events (partnerships, regulatory news, product launches)
- Used by: `ingestion/ingest_gdelt.py`

**Authentication:**
- None required. GDELT is fully public and free.

**Rate limits:**
- No published hard limit, but the GDELT project asks for ~1 request/second and batch processing during off-peak hours
- The ingestor includes a conservative delay

**Key config:**
- Configuration lives directly in `ingestion/ingest_gdelt.py`
- No entry in `config/sources.yaml` (listed as `stub` — the ingestor exists but GDELT is not yet wired into the daily production run)

---

### SEC EDGAR

**What it provides:**
- Form 4 insider buy/sell transaction filings
- 8-K material event filings
- XBRL financial data (income statement, balance sheet, cash flow)
- Used by: `ingestion/ingest_sec.py`

**Authentication:**
- None required. SEC EDGAR is a public government database.
- A valid `User-Agent` header with a contact email is required by SEC policy (set in `config/settings.yaml` as `sec_edgar.user_agent`)

**Rate limits:**
- SEC imposes a soft limit of ~10 requests/second. Exceeding it results in temporary 429 responses.
- `config/settings.yaml` sets `sec_edgar.rate_limit_delay: 0.12` seconds (approximately 8 req/sec)

**Key config:**
```yaml
# config/settings.yaml
sec_edgar:
  base_url: https://data.sec.gov
  user_agent: "MHDE-Validation contact@example.com"
  rate_limit_delay: 0.12
```

---

### CIK Validator

**What it provides:**
- Maps ticker symbol → CIK (Central Index Key) for use in SEC EDGAR API calls
- SEC EDGAR identifies companies by CIK, not ticker
- Used by: `universe/cik_validator.py`, referenced during SEC ingestion

**Authentication:**
- None required.

**Rate limits:**
- The CIK lookup uses SEC's company search endpoint, subject to the same soft limits as EDGAR.

---

### Sector ETFs

**What it provides:**
- Daily returns for the 11 SPDR sector ETFs: XLK (Technology), XLF (Financials), XLE (Energy), XLV (Healthcare), XLI (Industrials), XLP (Consumer Staples), XLU (Utilities), XLB (Materials), XLRE (Real Estate), XLC (Communication Services), XLY (Consumer Discretionary)
- Used for sympathy move detection and sector momentum scoring
- Used by: `ingestion/ingest_sector_etfs.py`, `missed/sector_attribution.py`, `features/momentum.py`

**Authentication:**
- Uses the same `POLYGON_API_KEY` as the price ingestor

**Rate limits:**
- 11 ETFs × 1 call each = 11 calls per day, a small fraction of the Polygon budget

---

### FRED (Federal Reserve Economic Data)

**What it provides:**
- Interest rates (Fed Funds, 2Y/10Y Treasury yields)
- Credit spreads (IG/HY)
- VIX (CBOE Volatility Index)
- Economic calendar and release dates
- Used by: `ingestion/ingest_fred.py` (if active), `features/macro.py`

**Authentication:**
- Environment variable: `FRED_API_KEY`
- Free registration at https://fred.stlouisfed.org/docs/api/api_key.html

**Rate limits:**
- Free tier: 120 API calls/minute (generous)
- `config/settings.yaml` sets `fred.rate_limit_delay: 0.5`

---

### FINRA Short Interest

**What it provides:**
- Bi-weekly short interest data per ticker (shares short, days-to-cover)
- Used by: `features/sentiment.py` for short interest component of sentiment score
- Pulled from FINRA's public CDN (no API, no auth)

**Authentication:**
- None. Public CDN file downloads.

**Rate limits:**
- Be polite. This is a static file server, not an API. Rate limit in config: `finra.rate_limit_delay: 1.0`

---

### CFTC (Commitments of Traders)

**What it provides:**
- TFF (Traders in Financial Futures) report: positioning by trader category
- Disaggregated COT report
- Used for macro sentiment and positioning signals in `features/macro.py`

**Authentication:**
- None. Public government data via Socrata open data endpoints.

**Rate limits:**
- `config/settings.yaml` sets `cftc.rate_limit_delay: 0.5`
- Default lookback: 4 weeks of history (`cftc.history_weeks: 4`)

---

## Sources Not Yet Active

These sources appear in `config/sources.yaml` with a non-active status:

| Source | Status | Notes |
|---|---|---|
| FDA advisory calendar | `stub` | Adapter not yet implemented (`ingestion/ingest_fda.py` exists as a stub) |
| Stocktwits | `stub` | Retail sentiment / message volume; adapter not yet implemented |
| Events (earnings calendar) | `experimental` | Uses unofficial endpoints; brittle; use with caution |

---

## Adding a New Source

1. Create an adapter in `ingestion/ingest_<name>.py` following the pattern in `ingestion/base_ingestor.py`.
2. Write results to DuckDB and record a row in `source_runs` (status `ok` or `error`, with record counts).
3. Add a config entry in `config/sources.yaml` with `status: experimental` initially.
4. Add any rate limit delays to `config/settings.yaml` under a named section.
5. If an API key is required, read it from the environment. Document the variable name in `config/sources.yaml` as a comment.
6. Wire the adapter into `pipelines/daily_radar.py` once validated.
