# MHDE Implementation Status and Missing Phases Audit

_Generated: 2026-05-03_

---

## System Snapshot

| Metric | Value |
|--------|-------|
| Tests passing | 875 |
| Universe (primary) | 504 tickers |
| Universe (extended) | 174 tickers |
| Sectors populated | 11 of 11 (1 NULL in primary) |
| Score history depth | 3 days (2026-05-01 → 2026-05-03) |
| Latest run tiers | Reject: 409, Incomplete: 109, C: 2, A/B: 0 |
| Prices history | 500 tickers × 1 year (2025-05-02 → 2026-05-01) |
| Missed events detected | 4,305 (30d window: 1,518) |
| Events with prior score | 124 (all from 2026-05-01 only) |
| Investigations completed | 1,238 |
| Candidate outcomes | 1,236 rows (forward returns: all NULL) |
| Candidate reviews | 7 submitted |
| Scorecard experiments | 4 applied, 4 proposed, 1 rejected |
| Short interest | 0 rows (FINRA CDN down) |

---

## Phase Status Table

| Phase | Name | Status | Evidence | Key Gaps | Next Step | Risk |
|-------|------|--------|----------|----------|-----------|------|
| 1 | Universe correctness | **Partial** | 504 primary tickers seeded, CIK validation live, 11 sectors populated | TEAM not in S&P 500 YAML; market cap / liquidity filter missing; 174 extended tickers sector=NULL | Add market cap filter; verify YAML covers all current S&P 500 members | Low |
| 2 | Price / move detection | **Built** | All 7 windows operational: 1d/3d/5d/10d/20d/60d gains + 52wk breakouts; 4,305 events across 500 tickers | Drawdown / loss detection missing; some duplicate multi-window events | Add drawdown detection; deduplicate clustered events in report | Low |
| 3 | Base scoring and tiering | **Partial** | Scoring engine runs daily; 629 tickers scored; 5-component scorecard; tiers assigned | 0 A-tier, 0 B-tier candidates; 36% Incomplete (missing fundamentals); weights uncalibrated | Run calibration against candidate_outcomes once forward returns populate | Medium |
| 4 | Catalyst attribution | **Partial** | 1.4M SEC filings ingested, LLM pipeline implemented, daily-catalyst-queue produces shadow projections | LLM requires real API key (running mock); ~50% source coverage; reactive not prospective | Configure NVIDIA/OpenAI key to enable real classification; add EFTS fallback for PDF filings | Medium |
| 5 | Dashboard / manual review | **Partial** | Multi-page Streamlit dashboard, HTML queue report, 7 reviews submitted, scorecard tracking | Under-used: 7 reviews insufficient for calibration; no alert triggers for tier crossings | Run daily pipeline consistently to accumulate reviews | Low |
| 6 | Prediction-vs-actual report | **Partial** | CLI live (`missed prediction-vs-actual`), score join works, 3 artifacts, 12 tests | Score history only 3 days → 91.8% of events unscored_mover; 0 scored_correct | Accumulate score history across weeks to improve join coverage | Low |
| 7 | Root-cause enrichment (missed spikes) | **Partial** | `missed_opportunity_investigations` table exists; 1,238 investigations run | 976 "text_evidence_available_not_classified" (LLM blocked); no deterministic root cause for true_miss / scored_missed / near_threshold rows in prediction report | **Next build**: add deterministic root-cause labeling for prediction-report rows without LLM | High |
| 8 | Sector / theme / sympathy attribution | **Missing** | (none) | No sector ETF prices; no cross-ticker theme clustering; GDELT and Stocktwits are stubs | Seed sector ETF tickers (XLF/XLK/XLE/XLV etc.); add peer relative-strength feature | High |
| 9 | Earnings / estimates / revisions data | **Partial** | 63 earnings calendar events in `events` table | No EPS estimates, no EPS actuals, no surprise magnitude, no revision momentum | Add Alpha Vantage or Polygon Financials adapter for EPS estimates | High |
| 10 | News / contract / product catalyst sources | **Missing** | GDELT and Stocktwits ingestors are registered stubs (zero records) | No news text, no press-release scraping, no IR page ingestion at scale | Implement GDELT 2.0 Events API adapter (free, no key) | Medium |
| 11 | Outcome tracking and learning loop | **Partial** | `candidate_outcomes` has 1,236 rows; `candidate_reviews` has 7 reviews; `learning/insights.py` exists | All forward_return columns NULL — outcomes not yet populated; reviews insufficient | Run `backtest smoke` daily to populate forward returns; encourage review submission | High |
| 12 | Production scoring / feature flags | **Partial** | `scorecard_experiments` table (4 applied, 4 proposed); human approval gate in `learning_loop.md` | No automated feature flag mechanism; "applied" experiments require manual re-scoring run | Define what "applied" means operationally: add experiment_applied_at to pipeline run log | Medium |
| 13 | Operational automation | **Partial** | `systemd` units exist; `main.py daily-radar`; operational scripts present | Daily pipeline not consistently running; no automated report delivery; no Telegram/email configured | Schedule daily-radar via systemd or cron; configure at least one notification channel | Medium |

---

## Target Ticker Analysis

Observed data from `data/processed/prediction_vs_actual_rows.csv` (30-day lookback).

### Why MHDE missed these moves

The primary failure pattern: nearly all notable moves happened between 2026-02-02 and 2026-04-30, when the scores table had no records. The MHDE scoring pipeline only started producing records from 2026-05-01. Scores 3 days deep cannot classify moves from the prior 3 months.

The secondary failure pattern: on 2026-05-01 (the only scoreable date), most tickers received `Incomplete` tier — meaning insufficient fundamental data coverage to rank, not a scoring judgment.

| Ticker | Detected Events | Best Return | Classification | Score (May 1) | Root Cause |
|--------|----------------|-------------|----------------|---------------|------------|
| GOOGL | 11 events | +24.6% (20d) | `near_threshold` (May 1: 41.7, Reject tier) | 41.7 | Pre-earnings run-up; score borderline — near C-tier threshold. Closest to catchable. |
| INTC | 11 events | +119.0% (60d) | `true_miss` (May 1: 22.9, Incomplete) | 22.9 | Earnings + continuation; Incomplete tier = missing fundamentals. Not a scoring miss — a data gap. |
| AMD | 21 events | +80.8% (60d) | `true_miss` (May 1: 20.7, Reject) | 20.7 | Sector AI hardware sympathy + earnings beat; scored Reject with thin fundamentals coverage. |
| SNDK | 17 events | +50.6% (20d) | `true_miss` (May 1: 17.9, Incomplete) | 17.9 | Storage thematic momentum; Incomplete tier = missing fundamentals. No sector-peer signal. |
| STX | 14 events | +91.5% (60d) | `true_miss` (May 1: 23.4, Incomplete) | 23.4 | Storage thematic momentum alongside SNDK; same root cause — missing sector correlation. |
| RDDT | 8 events | +34.4% (20d) | `true_miss` (May 1: 21.0, Incomplete) | 21.0 | Revenue forecast beat; Incomplete — no revenue estimate data source to detect surprise. |
| PLTR | 4 events | +19.2% (10d) | `unscored_mover` (all pre-May 1) | — | Government AI contract expansion; all events pre-date scoring. Would likely be C or near-threshold. |
| PYPL | 5 events | +13.8% (10d) | `unscored_mover` (all pre-May 1) | — | Guidance upgrade; pre-scoring. Score on May 1 not available in 30d window. |
| TEAM | 0 events | — | Not detected | — | Not in S&P 500 YAML (not a US-listed S&P 500 member in the YAML seed). |
| DIS | 0 events | — | Not detected | — | No threshold-crossing moves in the 30-day window at the required ≥5%/1d, ≥8%/3d thresholds. |

### Key patterns across target tickers

1. **Incomplete tier = data gap, not scoring logic failure.** INTC, SNDK, STX, RDDT all received `Incomplete` tier on May 1 because fundamental coverage (revenue, net income, shares outstanding) is below the 2-component minimum for a real tier assignment. The scoring logic works — the inputs are missing.

2. **Sector sympathy undetected.** SNDK and STX both moved on storage sector momentum. They moved together, yet there is no cross-ticker or sector-ETF signal in the system. AMD moved on AI hardware sympathy. No peer relative-strength feature exists.

3. **GOOGL is the most actionable insight.** With score 41.7 and Reject tier, GOOGL was 3.3 points from the 45.0 C-tier threshold. The `near_threshold` label is correct. A pre-earnings momentum feature or analyst revision signal might have pushed it over.

4. **Earnings surprise signal is entirely absent.** RDDT revenue beat, INTC guidance, PYPL guidance upgrade — none of these can be detected without EPS/revenue estimates to compute surprise magnitude.

5. **Time horizon: scores history is too shallow for meaningful classification.** 3 days of score history vs. 3 months of move history. The prediction-vs-actual report will not reach full utility until scores accumulate for 30–60 days.

---

## Investigation Root Causes (from `missed_opportunity_investigations`)

| Root Cause | Count | Meaning |
|-----------|-------|---------|
| `text_evidence_available_not_classified` | 976 | Filing text found but LLM not run (no API key) |
| `catalyst_not_classified` | 126 | No filing text resolved; classification impossible |
| `missing_price_data` | 66 | Price history gap prevented move measurement |
| `missing_fundamentals` | 31 | Fundamentals absent; Incomplete tier root |
| `price_move_without_known_catalyst` | 27 | Move occurred but no corresponding filing found |
| `routine_event_correctly_suppressed` | 11 | Normal volatility, correctly filtered |
| `truly_unpredictable` | 1 | Classified as genuinely unpredictable |

**The dominant root cause (78.9%) is LLM dependency.** The catalyst pipeline has source text but cannot classify it without an API key. This is not a scoring architecture problem.

---

## Next Build Recommendation

### Recommended: Deterministic root-cause enrichment for prediction-report rows

**Why this is the right next step:**
- The prediction-vs-actual report currently produces `root_cause_hint` as a shallow label (e.g. `scoring_blind_spot`, `data_gap`) derived from classification alone — no actual evidence is inspected.
- 115 `true_miss` rows and 9 scored rows have joined score data. These can be enriched deterministically (no LLM required) with specific root causes from the existing data.
- This does not touch production scoring, does not require new data sources, and produces immediate diagnostic value.

**Specific enrichments possible without LLM:**

| Root Cause | Detection Logic | Data Source |
|-----------|----------------|-------------|
| `incomplete_fundamentals` | `tier_before_event == "Incomplete"` AND `fundamentals_features` has < 2 non-null components | `fundamentals_features` join |
| `low_catalyst_score` | `was_scored=True` AND `catalyst_score < 30` | `scores` join (catalyst_score) |
| `missing_earnings_context` | Event within 5 days of an earnings event for the ticker | `events` table join |
| `sector_move` | 3+ tickers in same sector moved ≥ threshold in same 3-day window | cross-ticker grouping |
| `scoring_reject_with_data` | `tier == "Reject"` AND fundamentals present | `fundamentals_features` |
| `no_filing_in_window` | `had_catalyst_evidence=False` AND no filing in ±30 days | `filings` table |

**Build scope:**
- Modify `missed/prediction_report.py` to join `scores` (for component scores), `fundamentals_features`, and `events` tables
- Replace single `root_cause_hint` with a structured list
- Add a new "Root Cause Detail" column to CSV
- No new tables, no migration, no LLM

**After that — two parallel candidates:**

| Candidate | Impact | Effort |
|-----------|--------|--------|
| Sector ETF seeding (XLF/XLK/XLE/etc.) | Enables Phase 8 entirely | Low — add to YAML, no schema change |
| Populate `candidate_outcomes` forward returns daily | Enables Phase 11 calibration loop | Low — existing `outcomes/tracker.py` needs daily scheduling |

---

## What "Full MHDE Loop" Requires

The intended loop is:

```
scan S&P 500 daily
  → score candidates
  → observe actual moves (✓ built)
  → classify: catalyst / sector / momentum / miss (⚠ partial — LLM blocked)
  → enrich root causes (⚠ next build)
  → identify missing data / features (⚠ manual today)
  → analyst review + outcomes → scorecard experiment (⚠ 7 reviews, 4 applied)
  → production scoring unchanged until evidence approved (✓ governance exists)
```

**Blockers in priority order:**

1. **Score history depth** — accumulates automatically with daily runs. No code change needed.
2. **Root-cause deterministic enrichment** — next build. No LLM required.
3. **Forward return population** — schedule `backtest smoke` as a daily step after pipeline.
4. **Sector ETF prices** — add 11 sector ETF tickers to YAML. No schema change.
5. **LLM API key** — enables 976 investigations to be resolved automatically.
6. **Earnings estimates** — requires new data adapter (Alpha Vantage or Polygon).

---

_No code was changed in producing this document. All data from live DB at 2026-05-03._
