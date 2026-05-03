# MHDE Gap Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce `docs/mhde_gap_analysis.md` and `data/processed/mhde_gap_analysis.csv` documenting what the MHDE system can support today for the prediction-vs-actual spike loop, and what data is missing.

**Architecture:** This is a pure documentation task. No scoring, no new modules, no CLI commands. Two files are written directly using the pre-researched findings below. Tests are run as a regression check only (no new tests needed — no new code).

**Tech Stack:** Python (venv/bin/python), git. No external API calls.

---

## Pre-Researched Findings

The following analysis is based on reading the codebase at `/home/jpcg/MHDE`. Use these findings directly when writing the output files — do not re-research.

### Q1 — S&P 500 / US large-cap daily scan

**Status: PARTIAL**

- Universe source: `universe/sec_company_tickers.py` fetches SEC's `company_tickers.json` (~10k US issuers with SEC filings).
- Size cap: `max_symbols = 500` by default (config: `universe.max_symbols`). Fallback tickers go first (`universe_tier = "primary"`), rest are `"extended"` in insertion order — NOT by market cap.
- `companies.market_cap` column exists in schema but is never populated by the current pipeline (no market-cap data source wired in).
- `companies.sector` and `companies.industry` columns exist but are NOT populated — SEC's company_tickers.json does not provide GICS/SIC codes.
- **Gap 1**: No S&P 500 constituent list — the 500 symbols scanned are arbitrary from SEC ordering, not S&P 500 members.
- **Gap 2**: No market-cap sort or liquidity filter — micro-caps are included alongside mega-caps.
- **Gap 3**: `sector` and `industry` are always NULL in practice.

### Q2 — 1d, 3d, 5d, 10d move episode detection

**Status: PARTIAL (5d and 20d only)**

- `missed/detector.py` calls `_detect_gains()` for three windows:
  - `gain_5d_10pct` — ≥10% gain in 5 days (`GAIN_5D_THRESHOLD = 0.10`)
  - `gain_20d_20pct` — ≥20% gain in 20 days (`GAIN_20D_THRESHOLD = 0.20`)
  - `gain_60d_30pct` — ≥30% gain in 60 days (`GAIN_60D_THRESHOLD = 0.30`)
- `candidate_outcomes` stores: `forward_return_1d`, `forward_return_5d`, `forward_return_20d`, `forward_return_60d`, `forward_return_120d`.
- `prices_daily` has OHLCV data sufficient to compute any window.
- **Gap 1**: No 1d episode detection (single-day ≥X% move). 1d column in candidate_outcomes exists but no detector feeds it in missed pipeline.
- **Gap 2**: No 3d window in detector or candidate_outcomes.
- **Gap 3**: No 10d window in detector or candidate_outcomes.
- **Gap 4**: Down-move / drawdown episodes are not detected (only gains). `max_drawdown_20d` / `max_drawdown_60d` exist in candidate_outcomes but not in missed_opportunity_events.
- **Note**: All gaps are computable from existing `prices_daily` data — purely additive implementation.

### Q3 — Catalyst attribution from filings/source text

**Status: PARTIAL (SEC filings only, LLM-dependent)**

- `filings` table holds SEC EDGAR accession numbers and doc URLs for all tracked tickers.
- `missed/catalyst_source_resolver.py` fetches actual filing text from EDGAR.
- `missed/catalyst_classifier.py` uses LLM (OpenAI/NVIDIA/mock) to classify catalyst type.
- The full pipeline (`missed daily-catalyst-queue`) samples near-threshold Reject tickers, resolves source text, and produces structured enrichment (catalyst_type, materiality, sentiment, shadow score).
- Source coverage in pilot runs: ~50% of events have resolvable filing text (≥200 chars). PDF filings and missing CIK/accession numbers are the main failure modes.
- **Gap 1**: Attribution is reactive (post-move analysis only) — not prospective (pre-event prediction).
- **Gap 2**: No structured extraction of press-release text (IR page scraping is `"experimental"` in company_ir adapter).
- **Gap 3**: No earnings transcript / conference call text.
- **Gap 4**: LLM classification requires an API key (OpenAI or NVIDIA). Mock mode produces low-quality classifications.

### Q4 — Sector sympathy / theme momentum

**Status: MISSING**

- `companies.sector` and `companies.industry` are schema columns but always NULL (SEC source has no GICS data).
- `features/industry_utils.py` does bank/insurer detection via XBRL concept presence (keyword-based, not GICS).
- `features/macro.py` uses FRED macro series (interest rates, CPI, etc.) — no sector ETF prices.
- `ingest_gdelt.py` and `ingest_stocktwits.py` are both `StubIngestor` — they exist in the class hierarchy but do not fetch any data.
- No cross-ticker or peer-group analysis exists anywhere in the pipeline.
- **Gap 1**: No GICS sector codes — cannot group tickers by sector to detect sympathy moves.
- **Gap 2**: No sector ETF price feed (XLF, XLK, XLE, etc.) for sector momentum baseline.
- **Gap 3**: No news/theme clustering — GDELT and Stocktwits are stubs.
- **Gap 4**: No peer relative-strength or sector-relative return computation.

### Q5 — Prediction vs actual outcomes

**Status: PARTIAL (infrastructure exists, not fully wired)**

- `candidate_outcomes` stores forward returns for scored candidates. Populated by `backtest/labels.py` during `main.py backtest smoke`.
- `candidate_reviews` stores manual usefulness scores (1-5) and false-positive reasons.
- `backtest_runs` stores hit-rate and avg-return metrics per backtest execution.
- `learning/summarize.py` generates a calibration report from reviews and outcomes.
- `scorecard_experiments` tracks proposed and applied scorecard changes.
- **Gap 1**: `candidate_outcomes` is not auto-populated in the daily pipeline — requires a separate `backtest smoke` run after enough price history accumulates.
- **Gap 2**: No automated precision/recall or calibration curve against actual outcomes in the scoring pipeline itself.
- **Gap 3**: Prediction vs outcome matching depends on `(run_id, ticker)` pairs — old run_ids may not have price history far enough forward to label them yet.
- **Gap 4**: No outcome labeling for 3d or 10d windows.

### Q6 — Missing data assessment

| Data | Status | Evidence |
|------|--------|----------|
| Earnings calendar | PARTIAL | `EventsIngestor` fetches Nasdaq earnings calendar (±60d window), status=`experimental`. Stored in `events` table as `event_type="earnings"`. No EPS estimates or surprises. |
| Analyst estimates/revisions | MISSING | No adapter. No schema table. |
| News feed | STUB | `GDELTIngestor` and `StocktwitsIngestor` are `StubIngestor` — they register but fetch nothing. |
| Sector/industry mappings | PARTIAL | Schema columns exist in `companies` but always NULL. Keyword-based bank/insurer detection only. |
| Short interest | SUPPORTED | `FINRAIngestor` fully implemented. Biweekly FINRA reports → `short_interest` table. Used in `features/sentiment.py`. |
| Options / implied volatility | MISSING | No adapter, no schema table. |
| Institutional/insider transactions | PARTIAL | Form 4 filings are ingested into the `filings` table by `SECIngestor` (form_type filtering does not exclude Form 4). But no structured parsing of transaction quantity, price, or insider identity. |
| Real-time volume shock | PARTIAL | `features/momentum.py` computes `volume_spike` (current vs 20d avg from `prices_daily`). Daily granularity only — no intraday or real-time alerting. |

---

## File Structure

| File | Action |
|------|--------|
| `docs/mhde_gap_analysis.md` | **Create** — human-readable gap analysis report |
| `data/processed/mhde_gap_analysis.csv` | **Create** — machine-readable gap summary |

No Python modules are created or modified. No scoring files are touched.

---

## Task 1: Write `docs/mhde_gap_analysis.md`

**Files:**
- Create: `docs/mhde_gap_analysis.md`

- [ ] **Step 1: Write the file**

Write the following content exactly to `docs/mhde_gap_analysis.md`:

```markdown
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
Wire in an S&P 500 constituent list (iShares IVV holdings CSV, Slickcharts
scrape, or a curated YAML) as a `fallback_tickers` override. Sort by Polygon
`market_cap` field to bias toward large-caps. Populate `companies.sector` from
a GICS mapping file or Polygon Ticker Details API.

---

## 2. Move Episode Detection — Can we detect 1d, 3d, 5d, and 10d moves?

**Status: PARTIAL (5d and 20d only)**

### What works
- `missed/detector.py` detects three event types from `prices_daily`:
  - `gain_5d_10pct` — ≥10% gain in 5 days
  - `gain_20d_20pct` — ≥20% gain in 20 days
  - `gain_60d_30pct` — ≥30% gain in 60 days
- `candidate_outcomes` stores `forward_return_1d`, `_5d`, `_20d`, `_60d`, `_120d`.
- All required price data (`prices_daily`) exists to compute any window.

### Gaps
| Gap | Detail | Severity |
|-----|--------|----------|
| No 1d episode detection | Single-day ≥X% move not detected (column exists in candidate_outcomes) | High |
| No 3d window | Neither detector nor candidate_outcomes covers 3d | Medium |
| No 10d window | Neither detector nor candidate_outcomes covers 10d | Medium |
| Gains only | Drawdown / down-move episodes not detected in missed pipeline | Medium |

### Recommended fix
Add `GAIN_1D_THRESHOLD = 0.05`, `GAIN_3D_THRESHOLD = 0.08`, `GAIN_10D_THRESHOLD = 0.12`
to `missed/labels.py`. Add corresponding `_detect_gains()` calls in `missed/detector.py`.
Add `forward_return_3d` and `forward_return_10d` columns to `candidate_outcomes`
via a migration. All data is already present — purely additive implementation.

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
| Reactive only | Attribution runs post-move, not as prospective pre-event signal | High |
| ~50% source coverage | PDF filings, missing CIK/accession numbers excluded | Medium |
| LLM API dependency | Real classification requires OpenAI or NVIDIA API key | Medium |
| No press-release text | IR page scraping is experimental/stub | Low |
| No earnings transcripts | Conference call text not ingested | Low |

### Recommended fix
Move SEC source resolution into the daily pipeline (pre-scoring), not just
post-move analysis. For coverage, add a fallback to SEC full-text search API
(EFTS) when accession number resolution fails.

---

## 4. Sector Sympathy / Theme Momentum — Can we detect cross-ticker patterns?

**Status: MISSING**

### What works
- Nothing. `sector` and `industry` columns in `companies` are always NULL.
- Bank/insurer detection uses XBRL concept keywords — not GICS-based grouping.
- `features/macro.py` uses FRED macro data (interest rates, CPI) — no sector ETFs.

### Gaps
| Gap | Detail | Severity |
|-----|--------|----------|
| No GICS sector codes | `companies.sector` never populated from any source | High |
| No sector ETF prices | XLF, XLK, XLE, XLV, etc. not in prices_daily | High |
| News / theme clustering missing | GDELT and Stocktwits are stub ingestors | High |
| No peer relative strength | No cross-ticker or sector-relative return feature | Medium |

### Recommended fix
1. Populate `companies.sector` using Polygon Ticker Details API (free tier)
   or a GICS mapping CSV seeded from Wikipedia/iShares.
2. Add sector ETF tickers (XLF, XLK, XLE, XLV, XLI, XLP, XLU, XLB, XLRE, XLC)
   to `fallback_tickers` so they're priced daily.
3. Add a `sector_momentum` feature to `features/` that computes ticker return
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
| No 3d/10d outcome columns | `forward_return_3d` / `forward_return_10d` missing from schema | Medium |
| No automated precision/recall | Calibration is manual (learning/summarize.py produces a report) | Medium |
| Run_id staleness | Old predictions may not have enough forward price history yet | Low |

### Recommended fix
Add `candidate_outcomes` population to the daily pipeline (after scoring,
look back 20d and label any scored candidates with now-known forward returns).
Add `forward_return_3d`, `forward_return_10d` to schema via migration.

---

## 6. Data Gap Assessment — Required Data Sources

### 6.1 Earnings Calendar
**Status: PARTIAL**

`EventsIngestor` fetches Nasdaq earnings calendar for ±60 days from today.
Stored as `event_type="earnings"` in the `events` table. Status: `experimental`.

**Missing:** EPS estimates, EPS actuals, surprise magnitude, revenue estimates.
These require a paid source (Alpha Vantage Premium, Polygon, Zacks API).

### 6.2 Analyst Estimates / Revisions
**Status: MISSING**

No adapter exists. No schema table. Estimate revisions are a well-documented
leading catalyst signal (revision momentum).

**Recommended source:** Alpha Vantage `EARNINGS` endpoint (free, limited),
Polygon.io Financials API, or Zacks Research Wizard.

### 6.3 News Feed
**Status: STUB**

`GDELTIngestor` and `StocktwitsIngestor` both inherit from `StubIngestor` and
fetch nothing. They are registered in `_ALL_INGESTORS` but produce zero records.

**Recommended source:** GDELT 2.0 Events API (free), Polygon News API (paid),
or Benzinga News (paid). For Stocktwits, the public endpoint is free but
rate-limited.

### 6.4 Sector / Industry Mappings
**Status: PARTIAL (schema exists, never populated)**

`companies.sector` and `companies.industry` columns exist. SEC data does not
provide GICS codes. Keyword detection in `features/industry_utils.py` covers
bank and insurer only.

**Recommended source:** Polygon Ticker Details v3 API (`sic_description`,
`sic`, `market` fields) — free tier covers ~1,000 requests/day. Alternatively,
seed from a static GICS mapping CSV (Wikipedia/iShares).

### 6.5 Short Interest
**Status: SUPPORTED**

`FINRAIngestor` fetches biweekly FINRA short-interest reports. Stores
`short_interest`, `avg_daily_volume`, `days_to_cover` in the `short_interest`
table. Used by `features/sentiment.py`. No action needed.

### 6.6 Options / Implied Volatility
**Status: MISSING**

No adapter, no schema table. IV percentile is a reliable signal for identifying
elevated-uncertainty events (earnings plays, FDA dates).

**Recommended source:** Polygon Options API (paid, ~$29/mo Starter),
or CBOE bulk data. Add `implied_volatility` table with
`(ticker, date, iv_30d, iv_rank, put_call_ratio)`.

### 6.7 Institutional / Insider Transactions
**Status: PARTIAL (raw filings only, not structured)**

Form 4 (insider) and 13F (institutional quarterly) filings appear in the
`filings` table via `SECIngestor` (all form types are ingested). But the filing
text is not parsed — no structured table of transaction quantity, price, or
filer identity.

**Recommended fix:** Add a `form4_transactions` table and a parser in
`ingestion/ingest_sec.py` that extracts Table I of Form 4 XML
(nonDerivativeTransaction rows).

### 6.8 Real-Time Volume Shock
**Status: PARTIAL (daily only)**

`features/momentum.py` computes `volume_spike` (today's volume vs 20d average)
from `prices_daily`. This is end-of-day, not intraday.

For daily use, this is sufficient as a signal. If intraday alerting is needed,
a real-time price feed (Polygon WebSocket or IEX Cloud SSE) would be required.
Out of scope for current batch pipeline.

---

## Priority Matrix

| Item | Spike Loop Impact | Implementation Effort | Priority |
|------|-------------------|----------------------|----------|
| S&P 500 constituent list | High — fixes universe quality | Low (YAML + fallback_tickers) | P1 |
| Sector/industry mapping | High — enables sector features | Low (CSV seed + Polygon API) | P1 |
| 1d/3d/10d episode detection | High — closes labeling gaps | Low (additive to detector) | P1 |
| Auto-populate candidate_outcomes | High — enables live calibration | Medium (wire into daily pipeline) | P1 |
| Analyst estimates/revisions | Medium — known leading signal | High (new adapter + schema) | P2 |
| News feed (GDELT/Stocktwits) | Medium — theme/sentiment signal | Medium (implement stubs) | P2 |
| Options/IV | Medium — event-uncertainty signal | High (paid API + new schema) | P2 |
| Form 4 structured parser | Low — insider as lagging signal | Medium (XML parsing) | P3 |
| Intraday volume shock | Low — daily granularity sufficient | High (real-time feed) | P3 |
```

- [ ] **Step 2: Verify the file was written correctly**

```bash
head -5 docs/mhde_gap_analysis.md
wc -l docs/mhde_gap_analysis.md
```

Expected: first line is `# MHDE Gap Analysis — Prediction-vs-Actual Spike Loop`, file is >100 lines.

- [ ] **Step 3: Commit**

```bash
git add docs/mhde_gap_analysis.md
git commit -m "docs: add MHDE gap analysis for prediction-vs-actual spike loop"
```

---

## Task 2: Write `data/processed/mhde_gap_analysis.csv`

**Files:**
- Create: `data/processed/mhde_gap_analysis.csv`

- [ ] **Step 1: Write the CSV file**

Write the following content exactly to `data/processed/mhde_gap_analysis.csv`:

```
question_id,question,capability_status,current_support,gaps,recommended_source,priority
Q1,Can we scan all S&P 500 / US large-cap tickers daily?,PARTIAL,"SEC company_tickers.json (~10k), capped at 500; universe_tier primary/extended; prices_daily updated daily","No S&P 500 constituent list; market_cap never populated; sector/industry always NULL; no liquidity filter","S&P 500 constituent YAML in fallback_tickers; Polygon Ticker Details for market_cap and sector",P1
Q2_1d,Can we detect 1d move episodes (≥5%)?,MISSING,"candidate_outcomes has forward_return_1d column but no detector populates it","No single-day episode in missed/detector.py","Add GAIN_1D_THRESHOLD=0.05 to missed/labels.py and _detect_gains(window=1) call in detector",P1
Q2_3d,Can we detect 3d move episodes?,MISSING,"prices_daily has data; no detector or outcome column for 3d","No 3d window anywhere in pipeline","Add GAIN_3D_THRESHOLD=0.08; add forward_return_3d to candidate_outcomes schema",P1
Q2_5d,Can we detect 5d move episodes (≥10%)?,SUPPORTED,"missed/detector.py: gain_5d_10pct; candidate_outcomes: forward_return_5d",None,No action needed,—
Q2_10d,Can we detect 10d move episodes?,MISSING,"prices_daily has data; no detector or outcome column for 10d","No 10d window anywhere in pipeline","Add GAIN_10D_THRESHOLD=0.12; add forward_return_10d to candidate_outcomes schema",P1
Q2_20d,Can we detect 20d move episodes (≥20%)?,SUPPORTED,"missed/detector.py: gain_20d_20pct; candidate_outcomes: forward_return_20d",None,No action needed,—
Q3,Can we attribute catalysts from filings/source text?,PARTIAL,"filings table + SEC source resolver + LLM classifier pipeline fully wired; daily-catalyst-queue command operational","~50% source coverage; reactive not prospective; LLM API key required; no earnings transcripts","Add SEC EFTS fallback for missing accession numbers; move attribution pre-scoring",P2
Q4,Can we detect sector sympathy / theme momentum?,MISSING,"companies.sector/industry columns exist but always NULL; GDELT/Stocktwits are stubs","No GICS codes; no sector ETF prices; no news feed; no peer analysis","Seed sector from Polygon Ticker Details; add sector ETF tickers (XLF/XLK/etc.) to universe; implement GDELT stub",P1
Q5,Can we track prediction vs actual outcomes?,PARTIAL,"candidate_outcomes + candidate_reviews + backtest_runs schema fully defined; learning/summarize.py generates reports","Not auto-populated in daily pipeline; no 3d/10d outcome columns; no automated precision/recall","Wire outcome labeling into daily pipeline; add 3d/10d schema migration",P1
D1,Earnings calendar,PARTIAL,"EventsIngestor fetches Nasdaq calendar ±60d; stored in events table; status=experimental","No EPS estimates/actuals/surprise; experimental status","Alpha Vantage EARNINGS or Polygon Financials for estimates",P2
D2,Analyst estimates / revisions,MISSING,No adapter no schema table,No estimate or revision data at all,"Alpha Vantage EARNINGS endpoint (free limited) or Polygon Financials",P2
D3,News feed,STUB,"GDELTIngestor and StocktwitsIngestor are StubIngestor — registered but fetch nothing",No news or sentiment data ingested,"Implement GDELT 2.0 Events API (free); Polygon News API (paid)",P2
D4,Sector / industry mappings,PARTIAL,"companies.sector and companies.industry columns exist in schema","Never populated — SEC source has no GICS codes; bank/insurer only via keyword detection","Polygon Ticker Details v3 (sic_description); static GICS CSV seed",P1
D5,Short interest,SUPPORTED,"FINRAIngestor fully implemented; short_interest table populated biweekly; used by features/sentiment.py",None,No action needed,—
D6,Options / implied volatility,MISSING,No adapter no schema table,No IV or options flow data at all,"Polygon Options API ($29/mo); add implied_volatility table",P2
D7,Institutional / insider transactions,PARTIAL,"Form 4 and 13F appear in filings table (all form types ingested by SECIngestor)","Filing text not parsed; no transaction table with quantity/price/filer","Add form4_transactions table; parse Form 4 XML nonDerivativeTransaction rows",P3
D8,Real-time volume shock,PARTIAL,"features/momentum.py computes volume_spike (current vs 20d avg) from prices_daily daily","End-of-day only — no intraday or real-time alerting","Polygon WebSocket or IEX SSE for intraday (out of scope for batch pipeline)",P3
```

- [ ] **Step 2: Verify the CSV is well-formed**

Write a script `/home/jpcg/MHDE/.claude/local_scripts/verify_gap_analysis_csv.py` with this content:

```python
import csv
import sys

path = "data/processed/mhde_gap_analysis.csv"
required_cols = {
    "question_id", "question", "capability_status",
    "current_support", "gaps", "recommended_source", "priority"
}

with open(path) as f:
    reader = csv.DictReader(f)
    rows = list(reader)

missing_cols = required_cols - set(reader.fieldnames or [])
if missing_cols:
    print(f"FAIL: Missing columns: {missing_cols}")
    sys.exit(1)

if len(rows) < 10:
    print(f"FAIL: Too few rows ({len(rows)}), expected >=10")
    sys.exit(1)

valid_statuses = {"SUPPORTED", "PARTIAL", "MISSING", "STUB", "—"}
bad = [r["question_id"] for r in rows if r["capability_status"] not in valid_statuses]
if bad:
    print(f"FAIL: Invalid capability_status in rows: {bad}")
    sys.exit(1)

print(f"OK: {len(rows)} rows, {len(reader.fieldnames)} columns")
for r in rows:
    print(f"  {r['question_id']:6} [{r['capability_status']:10}] {r['question'][:60]}")
```

Then run it:

```bash
venv/bin/python .claude/local_scripts/verify_gap_analysis_csv.py
```

Expected output: `OK: 17 rows, 7 columns` followed by a row list.

- [ ] **Step 3: Commit**

```bash
git add data/processed/mhde_gap_analysis.csv .claude/local_scripts/verify_gap_analysis_csv.py
git commit -m "docs: add MHDE gap analysis CSV and verification script"
```

---

## Task 3: Run full test suite and final commit

**Files:** None modified.

- [ ] **Step 1: Run existing tests**

```bash
venv/bin/python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: all pass. Zero scoring, feature, or ingestion files were touched — no regressions possible, but verify anyway.

- [ ] **Step 2: Verify output files are both present**

```bash
ls -lh docs/mhde_gap_analysis.md data/processed/mhde_gap_analysis.csv
```

Expected: both files exist, both non-empty.

- [ ] **Step 3: Check first line of markdown and header of CSV**

```bash
head -1 docs/mhde_gap_analysis.md
head -1 data/processed/mhde_gap_analysis.csv
```

Expected:
```
# MHDE Gap Analysis — Prediction-vs-Actual Spike Loop
question_id,question,capability_status,current_support,gaps,recommended_source,priority
```

- [ ] **Step 4: No additional commit needed** — Tasks 1 and 2 already committed. Confirm `git log --oneline -3` shows the two doc commits.

```bash
git log --oneline -3
```

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|-------------|------|
| Use docs/data_inventory.md and CSV as inputs | ✅ Pre-researched in plan header |
| Q1: S&P 500 / large-cap daily scan | ✅ Task 1 §1, Task 2 Q1 row |
| Q2: 1d/3d/5d/10d move episodes | ✅ Task 1 §2, Task 2 Q2_* rows |
| Q3: Catalyst attribution from filings | ✅ Task 1 §3, Task 2 Q3 row |
| Q4: Sector sympathy / theme momentum | ✅ Task 1 §4, Task 2 Q4 row |
| Q5: Prediction vs actual outcomes | ✅ Task 1 §5, Task 2 Q5 row |
| Q6: Missing data (all 8 items) | ✅ Task 1 §6.1–6.8, Task 2 D1–D8 rows |
| Produce docs/mhde_gap_analysis.md | ✅ Task 1 |
| Produce data/processed/mhde_gap_analysis.csv | ✅ Task 2 |
| Do not change scoring | ✅ No scoring files touched |
| Do not call OpenAI | ✅ No API calls anywhere in this plan |
| Run full tests | ✅ Task 3 |

**Placeholder scan:** No TBD, TODO, or vague instructions. All file content is written in full.

**Type consistency:** N/A — no function signatures. CSV column names are consistent between the header row and verification script.
