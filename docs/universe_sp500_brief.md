# Universe Correctness — Implementation Brief

_Date: 2026-05-03_

## Problem Statement

MHDE markets itself as a Russell-1000 / S&P-500-style discovery engine, but the
universe it actually scores every day is neither. It is "the first ~500 valid
US-listed equities, ordered by SEC CIK, that survive a name-keyword filter." The
result is a stable but arbitrary slice of the SEC company list that mixes large
caps (AAPL, ABBV, ABT) with mid- and micro-caps (ALAB, ABEV) and excludes
hundreds of bona fide S&P 500 names that happen to have higher CIK numbers
(e.g. mainstream financials, industrials, and recent IPOs).

The `companies` table also has no `market_cap`, no `sector`, and no `industry`
data populated. Today's audit (510 active rows, see Step 4 output below) shows
**0 distinct sectors, 0 distinct industries, 510 NULL `market_cap` values, and
only 6 `primary`-tier rows** (the six tickers in `config/universe.yaml`'s
`fallback_tickers` block). Every downstream consumer — ingestors, feature
builders, scoring — works off a list whose composition is essentially defined
by SEC's CIK assignment order.

The practical impact is twofold. First, ranking outputs cannot be benchmarked
against a known index because the universe has no documented membership rule.
Second, sector-relative features (sympathy, sector rotation, peer-relative
valuation) are impossible because every company's sector is `NULL`. Fixing the
universe is a prerequisite for almost every subsequent improvement.

## Current State

### How the universe is built

Code path on every `pipelines/daily_radar.py` run:

1. `pipelines/daily_radar.py:62-66` calls `build_universe(conn, cfg)`.
2. `universe/universe_builder.py:33` calls `fetch_sec_company_tickers()`.
3. `universe/sec_company_tickers.py:14-37` GETs `https://www.sec.gov/files/company_tickers.json`
   and returns ~10k records as a Python list, in the natural iteration order of
   the JSON dict (which is keyed by CIK rank).
4. `universe/universe_builder.py:40` calls `filter_non_equities(raw, cfg)`.
5. `universe/filters.py:40-70` drops:
   - tickers containing `.`, `-`, or `+`
   - tickers longer than 5 chars
   - names matching `WARRANT|UNIT SER|PREFERRED|NOTE DUE|BOND DUE|DEP SHS|DEP SHARES|DEPOSITARY|ADR`
   - if `exclude_etfs=true`: names matching `ETF|ISHARES|SPDR|VANGUARD|...`
   - if `exclude_funds=true`: names matching ` FUND|TRUST|REIT|BDC|MLP |PARTNERSHIP`
6. `universe/universe_builder.py:46-65` prepends every ticker listed in
   `cfg["universe"]["fallback_tickers"]` (currently 6 tickers in
   `config/universe.yaml`) and tags them `universe_tier = "primary"`.
7. `universe/universe_builder.py:68-74` walks the *filtered SEC list in CIK order*
   and appends each unseen ticker as `universe_tier = "extended"` until
   `max_symbols` (currently 500) is reached.
8. `universe/universe_builder.py:78-112` upserts all of them into `companies`.

`exchange`, `sector`, `industry`, and `market_cap` are never written.

### Why the first 500 are wrong

Three independent reasons:

1. **Wrong sort order.** SEC's `company_tickers.json` is iterated in the
   undocumented order of its top-level keys (effectively CIK rank order from
   when each filer first registered). It is not a market-cap, liquidity, or
   index-membership ranking. The first 500 names that survive filtering are a
   historical artifact of EDGAR registration order, not the 500 largest US
   companies.
2. **`fallback_tickers` is essentially empty by default.** `config/universe.yaml`
   only lists six tickers (`AAPL, NVDA, TSLA, JPM, UBER, RKLB`). Everything
   else falls into `extended` tier purely on CIK rank. Today's audit confirms
   only 6 primary-tier rows out of 510.
3. **No market-cap secondary sort.** Even if `fallback_tickers` were larger,
   `universe_builder.py` has no notion of size: there is no market-cap field
   to sort by because nothing populates `companies.market_cap` (see below).

### What is NULL and why

| Column | Status | Reason |
|---|---|---|
| `companies.market_cap` | 100% NULL (510/510) | No ingestor writes it. The column exists in `storage/schema.sql:21` but no `INSERT/UPDATE` in the codebase touches it. |
| `companies.sector` | 100% NULL (510/510) | SEC `company_tickers.json` carries only `cik_str / ticker / title`. No GICS, no SIC. `universe_builder.py` writes only the seven fields it has. |
| `companies.industry` | 100% NULL (510/510) | Same as `sector`. |
| `companies.exchange` | 100% NULL | Same as `sector`. |

`features/industry_utils.py:34-75` works around the missing `sector` column for
banks and insurers only, by inferring industry from XBRL concept presence
(`NetInterestIncome`, `PremiumsEarnedNet`, etc.) and falling back to keyword
matching on `companies.company_name`. This is enough to gate bank-vs-non-bank
valuation logic, but it does not give any sector-level grouping for sympathy,
rotation, or peer features.

### Who consumes the companies table

`grep "FROM companies"` across `ingestion/`, `features/`, `scoring/`,
`pipelines/` returns:

| File:line | Query | Purpose |
|---|---|---|
| `ingestion/orchestrator.py:63` | `SELECT ticker FROM companies WHERE is_active = true ORDER BY universe_tier, ticker` | Master ticker list every ingestor (`SECIngestor`, `PricesIngestor`, `StooqPricesIngestor`, `YahooHistoricalIngestor`, `FREDIngestor`, `FINRAIngestor`, `CFTCIngestor`, `EventsIngestor`, `FDAIngestor`, `StocktwitsIngestor`, `GDELTIngestor`) iterates over. |
| `pipelines/daily_radar.py:65` | `SELECT COUNT(*) FROM companies WHERE is_active = true` | `RunSummary.universe_size`. |
| `pipelines/daily_radar.py:77` | `SELECT ticker FROM companies WHERE is_active = true ORDER BY universe_tier, ticker` | List passed to feature builder, scorer, and packet builder. |
| `ingestion/ingest_sec.py:84` | `SELECT cik FROM companies WHERE ticker = ?` | CIK lookup before SEC submissions/companyfacts fetch. |
| `features/industry_utils.py:66` | `SELECT company_name FROM companies WHERE ticker = ?` | Name-keyword industry inference. |

The scoring layer (`scoring/scorecard.py`) does not query `companies` directly;
it reads `features` keyed by ticker, so it inherits whichever ticker list the
upstream pipeline assembled.

## Proposed Fix — Minimal Safe Approach

### Option A: S&P 500 YAML seed (recommended, zero cost)

Maintain a static `universe/sp500_tickers.yaml` containing the ~503 current
S&P 500 constituents (ticker + company_name + last_updated). Wire the
universe builder to load that list and feed it into the existing
`fallback_tickers` mechanism.

Why this is the minimal-safe change:

- `universe_builder.py:46-65` already promotes any `fallback_tickers` entry to
  `universe_tier = "primary"` and creates a stub `companies` row if SEC's list
  doesn't contain it. We just need to populate that list from a file.
- No schema changes. No new tables. No new ingestors. No scoring changes.
- `max_symbols` already caps total universe size, and the loop in
  `universe_builder.py:68-74` will continue to fill the remainder from the
  filtered SEC list with `universe_tier = "extended"`. So the change is
  purely *additive*: S&P 500 names get prepended in `primary` slots; everything
  else still runs.

Maintenance: update the YAML quarterly when S&P rebalances (~20-30 changes per
quarter, often only ~5-10). Source: Wikipedia "List of S&P 500 companies" (free,
parseable HTML, refreshed within hours of any change). The YAML is committed
to the repo, so the universe is reproducible and diff-able.

### Option B: Polygon Ticker Details for `market_cap` + `sector` (supplemental)

Polygon's `/v3/reference/tickers/{ticker}` endpoint returns `market_cap`,
`sic_code`, `sic_description`, `primary_exchange`, `share_class_shares_outstanding`.
A lightweight `TickerDetailsIngestor` could:

- iterate over `companies WHERE is_active=true`
- call `/v3/reference/tickers/{ticker}`
- write `market_cap`, `sector` (mapped from `sic_description`), `exchange` to `companies`

Free tier rate limit: 5 req/sec — ~100s for 500 tickers. Refresh weekly, since
market_cap and sector change slowly. Polygon adapter scaffolding already exists
in `adapters/polygon.py`.

**Note:** Polygon free tier requires account registration and an API key (`POLYGON_API_KEY`). This is already wired in `adapters/polygon.py` and `config/sources.yaml` — if the key is set, Option B can reuse it.

This is *not* required for the S&P 500 universe fix and should be a separate
task. It unblocks sector-relative features and market-cap weighting later.

### Option C: Stooq for sector (not viable)

`ingestion/ingest_stooq.py` and `ingestion/ingest_stooq_historical.py` use
Stooq's `/q/d/l/` CSV endpoint, which returns OHLCV only. Stooq publishes no
fundamental, sector, or market-cap data. Not a fit.

### Recommended approach

Option A only, this sprint. Option B as a separate follow-up.

## Implementation Plan for Option A

### Files to create / modify

- **Create** `universe/sp500_tickers.yaml` — schema:
  ```yaml
  last_updated: 2026-05-03
  source: "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
  tickers:
    - { ticker: AAPL, company_name: "Apple Inc." }
    - { ticker: MSFT, company_name: "Microsoft Corporation" }
    - ...
  ```
- **Modify** `universe/universe_builder.py` — at the top of `build_universe`,
  before reading `fallback_tickers` from cfg, attempt to load
  `universe/sp500_tickers.yaml`. If it exists, merge its tickers into the
  `fallback_tickers` list (preserving the cfg list as well, deduped). If the
  file is missing, behaviour is unchanged.
- **Modify** `config/universe.yaml` — bump `max_symbols` from 500 to 510 (or
  520) so the full S&P 500 plus the existing six manual fallbacks fit without
  truncating any constituent.

No changes to `config/tickers.yaml` (that's a separate IR-URL basket, unrelated).
No changes to ingestion, feature, or scoring code.

### Schema changes needed

None. `companies.universe_tier`, `companies.market_cap`, `companies.sector`
already exist. The Option B follow-up will populate the latter two.

### Scoring impact

None. `scoring/scorecard.py` does not filter on `universe_tier`. It scores
whatever appears in `features` for the run. The change increases coverage of
real S&P names and reduces noise from arbitrary CIK-ordered micro-caps; it does
not change weights, thresholds, or tier assignment logic.

### Test plan

- Unit: `tests/test_universe_builder.py` — given a temp YAML with `[FOO, BAR]`
  and `max_symbols=10`, assert both end up in `companies` with
  `universe_tier='primary'` and SEC fillers occupy the remaining 8 slots as
  `extended`.
- Integration: run `venv/bin/python main.py health` after `build_universe` and
  confirm `Active companies: 510`, `Primary tier: ~503`, no exceptions.
- Smoke: spot-check that AAPL, MSFT, NVDA, GOOGL, JPM, BRK.B (ticker-character
  filter caveat — see Risks), JNJ, V are all `is_active=true`.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| YAML goes stale on S&P rebalances | Add `last_updated` field; schedule quarterly review; failure is graceful (universe still has filtered SEC fillers). |
| Some S&P 500 tickers are not in SEC's `company_tickers.json` (e.g. recent index additions before SEC sync) | `universe_builder.py:52-62` already creates a stub row with `cik=None`. Downstream `SECIngestor` will skip these for fundamentals; price ingestors still work. |
| Tickers with `.` or `-` (BRK.B, BF.B) currently rejected by `filters.py:53` | When loaded via `fallback_tickers`, the ticker bypasses `filter_non_equities` entirely. They will be inserted but `SECIngestor` may need a CIK from the YAML to fetch filings. Document this as a known follow-up. |
| `max_symbols=500` truncates fallback list | Bump to 510 in `config/universe.yaml`; make `max_symbols` a soft cap that never drops `primary`-tier rows in a future patch. |
| Non-S&P 500 tickers continue to be scored alongside | Acceptable; `extended` tier still has signal. A future task can add a `--tier primary` CLI flag if desired. |
| S&P rebalance removals leave `is_active = true` | `universe_builder.py` upserts `is_active = true` for YAML members but has no step to set `is_active = false` for tickers removed from the YAML. Add a reconciliation step: after upserting, set `is_active = false` for any `universe_tier='primary'` ticker NOT in the current YAML. |

## What This Does NOT Fix

- `companies.market_cap` is still NULL — needs Option B.
- `companies.sector` / `industry` still NULL — needs Option B or a GICS CSV seed.
- Sector-sympathy features, sector rotation, market-cap-weighted aggregates are
  still impossible — they depend on Option B.
- These are explicitly deferred to a follow-up brief.
- Deactivation of removed primary-tier tickers — after each YAML update, run a reconciliation query to set `is_active = false` for any `universe_tier='primary'` ticker no longer in the current YAML.
