# MHDE Decision Log

## 2026-05-01 — v1 engine build

Built the full MHDE engine surface area: universe construction, ingestion, feature engineering,
scoring, hypotheses, LLM briefs, notifications, candidate outcome tracking, backtesting,
XGBoost smoke, governance, pipelines, and Streamlit dashboard.

**Key decisions:**

- Paper trading was explicitly excluded. MHDE is a discovery and evidence system, not a trading
  simulator. Candidate outcome tracking (`candidate_outcomes` table) replaces paper trading as
  the evaluation mechanism.

- Universe selection is name-filtered only (SEC company_tickers.json). No market cap, liquidity,
  or price filter is applied at universe construction time. This is a known v1 limitation.

- All external credentials are optional. Every ingestor, LLM provider, and notification channel
  degrades gracefully to stub/mock/no-op when credentials are absent.

- DuckDB is the storage layer. File-based, no server. All engine data persists at `data/mhde.duckdb`.

- XGBoost model is quarantined: experimental only, not used for alerts or rankings.

- Dashboard authentication is enabled by default for VPS deployment.

## 2026-05-01 — Predictive philosophy clarified

**Decision:** MHDE is a current-evidence hypothesis discovery engine, not a historical
pattern-matching engine.

**Rationale:** MHDE must be predictive, but prediction is grounded in observable signals
as of today — filings, fundamentals, prices, short interest, events. It does not back-fit
scoring rules to historical price returns. Historical outcomes (candidate_outcomes) are used
to evaluate whether the current-evidence logic produces useful hypotheses, not to derive the
logic itself.

**Impact:** Updated `docs/learning_loop.md`, `docs/scorecard_v1.md`, `docs/model_governance.md`,
and `docs/known_limitations.md` to state this explicitly. Governance now requires that any
model trained on historical returns must be validated for current-evidence generalization before
influencing production outputs.

## 2026-05-01 — Learning loop added

Added the MHDE learning loop: candidate review table, scorecard experiment table, structured
error taxonomy, calibration analytics, insights engine, and dashboard learning page.

**Key decisions:**

- Learning is driven by **human review quality signals** (`candidate_reviews`), not only forward
  returns. The two questions are: "did the stock go up?" AND "was the hypothesis good?"

- MHDE does **not** automatically apply scorecard changes. All experiments require human
  approval (`approved_by` field) before being applied.

- A structured false-positive taxonomy (15 reason codes) replaces free-form notes as the
  primary failure classification signal.

- Scorecard experiments may be proposed automatically by `learning/insights.py` but are never
  applied without human approval.

- `python main.py learn summarize` generates a calibration report from all accumulated outcome
  and review data.

- Dashboard page 13 (`13_learning_calibration.py`) provides a review submission form and
  calibration visualizations.


## 2026-05-01 — Experiment applied: c129101b591b43a4

**Hypothesis:** Broken shares, equity, or denominator values can create fake cheap scores. Implausible P/S, P/E, or P/B values should be nulled rather than used.
**Approved by:** jp_goncalves
**Applied by:** jp_goncalves
**Applied at:** 2026-05-01T22:47:40.910451
**Affected components:** features/valuation.py, features/quality.py, scoring/scorecard.py
**Proposed change:** `{"ps_bounds": "null if P/S < 0.05 or P/S > 100", "pe_bounds": "null if P/E < 0 or P/E > 500", "pb_negative_equity": "null if book value <= 0 (negative equity)", "shares_sanity": "require shares_outstanding > 1M; null if stale > 5 years", "revenue_growth_sanity": "require same XBRL concept across both periods; flag cross-concept comparisons", "action": "set affected ratio to null; add missing_reason=valuation_denominator_invalid"}`
**Notes:** Applied: valuation sanity checks in features/valuation.py and quality.py. P/S null if <0.05 or >100; P/E null if >150; P/B null if >50 or equity<=0; shares null if <1M; revenue growth null if cross-concept or |growth|>500%.


## 2026-05-01 — Experiment applied: 3fa9a31cc3704b85

**Hypothesis:** Foreign private issuers using 20-F/6-K and non-USD reporting currencies can produce misleading USD valuation ratios unless currency and reporting units are normalized.
**Approved by:** jp_goncalves
**Applied by:** jp_goncalves
**Applied at:** 2026-05-01T23:16:36.993515
**Affected components:** features/valuation.py, features/catalyst.py, features/risk.py
**Proposed change:** `{"detect_foreign_private_issuer": true, "detect_reporting_currency": true, "action_non_usd_no_fx": "set cheap_score=null, valuation_missing_reason=foreign_currency_not_normalized", "catalyst_6k": "include in catalyst evidence, mark as foreign_issuer_disclosure not business_catalyst"}`
**Notes:** Applied: foreign filer guard in features/filer_utils.py + features/valuation.py. Detects 20-F/6-K/40-F filers; nulls P/S/P/E/P/B when reporting currency is non-USD. 6-K recorded as disclosure_evidence in catalyst metadata — not auto-scored. USD-reporting foreign filers (e.g. Israeli tech) proceed normally.


## 2026-05-01 — Experiment applied: 5c873dcb5ac24a80

**Hypothesis:** Banks and insurers use industry-specific financial concepts that make generic revenue/margin metrics misleading or impossible. Banks reporting only fee-income revenue (RevenueFromContractWithCustomer) should have P/S nulled; NI > Revenue is financially impossible and indicates XBRL concept mismatch; bank and insurer quality metrics should be confidence-downgraded.
**Approved by:** jp_goncalves
**Applied by:** jp_goncalves
**Applied at:** 2026-05-01T23:38:43.849438
**Affected components:** features/industry_utils.py (new), features/valuation.py, features/quality.py
**Proposed change:** `{"bank_detection": "XBRL-primary (NetInterestIncome etc.) + name-keyword fallback (BANCORP, FINANCIAL GROUP etc.)", "insurer_detection": "XBRL-primary (SupplementaryInsuranceInformationPremiumRevenue etc.) + name-keyword fallback; overrides bank detection", "bank_ps_guard": "null P/S when bank has no us-gaap/Revenues concept (fee-income only)", "ni_revenue_mismatch": "null net_margin with financial_concept_mismatch when NI > Revenue", "bank_quality_confidence": "lower net_margin and revenue_growth_yoy confidence to low with bank_specific_quality_required warning", "insurer_quality_confidence": "lower net_margin confidence to low with insurance_specific_quality_required warning; score still computed"}`
**Notes:** Applied: features/industry_utils.py created with detect_industry() and bank_has_total_revenue(). Dual XBRL+name detection; insurer XBRL overrides bank XBRL. Bank P/S nulled when only RevenueFromContract... available. NI > Revenue triggers financial_concept_mismatch guard. Bank/insurer quality confidence lowered to 'low' with appropriate warning metadata.


## 2026-05-01 — Experiment applied: 5c873dcb5ac24a80

**Hypothesis:** Banks and insurers need industry-specific revenue and quality concepts. Generic revenue and margin logic creates false quality signals for these sectors.
**Approved by:** jp_goncalves
**Applied by:** jp_goncalves
**Applied at:** 2026-05-01T23:38:32.379488
**Affected components:** features/quality.py, features/valuation.py, features/risk.py
**Proposed change:** `{"detect_industry": "SIC, sector, or company metadata", "banks": "avoid RevenueFromContractWithCustomerExcludingAssessedTax as total revenue; flag NI > revenue as impossible", "insurers": "treat generic net_margin cautiously; note gross revenue concept inflates denominator", "action": "lower quality confidence or set quality_score=null when industry concept mismatch detected; add missing_reason=industry_specific_financials_required"}`


## 2026-05-02 — Phase 1: Stooq historical OHLCV ingestor + momentum source label fix

**Rationale:** Momentum features nulled for virtually all tickers because `StooqPricesIngestor`
only fetches the latest daily quote, not history. `compute_momentum()` requires 20+ days of
price data to produce non-null scores. Without history, the 10% momentum weight in the scorecard
was always missing, systematically lowering total scores and increasing the Incomplete rate.

**Changes applied:**
- `ingestion/ingest_stooq_historical.py` — new `StooqHistoricalIngestor` (experimental status).
  Bootstraps 252 days of daily OHLCV per ticker on first run; incremental thereafter.
  Uses Stooq `/q/d/l/` endpoint (free, no API key). Runs after `StooqPricesIngestor` in orchestrator.
- `ingestion/orchestrator.py` — added `StooqHistoricalIngestor` to `_ALL_INGESTORS`.
- `features/momentum.py` — replaced hardcoded `source="polygon"` with `source="prices_daily"`
  in all 5 return sites. The feature reads from `prices_daily` regardless of which ingestor
  populated it; claiming Polygon source was misleading when data came from Stooq.

**Tests added:** 13 tests in `tests/test_stooq_historical.py` (all passing).

**Governance:** Pure data enrichment — no scoring weight changes. Applied directly without
experiment lifecycle. The source label fix is a correctness correction.


## 2026-05-02 — Experiment applied: bd578b3127fc41e9

**Hypothesis:** 10-Q and 10-K filings are routine mandatory disclosures, not catalysts. 8-K filings without material keywords (earnings, merger, acquisition, guidance, etc.) are also routine. Both inflate catalyst_score and push borderline candidates into A-tier without genuine catalyst evidence.
**Approved by:** jp_goncalves
**Applied by:** jp_goncalves
**Applied at:** 2026-05-02T08:42:06.889930
**Affected components:** features/catalyst.py
**Proposed change:** `{"10q_10k_score": 5, "8k_material_score": 30, "8k_routine_score": 15, "8k_material_keywords": ["earn", "acqui", "merger", "divestiture", "agreement", "guidance", "revenue", "settlement", "dividend", "buyback", "restate"], "routine_filing_metadata_flag": true, "action": "10-Q/10-K: reduce from +20 to +5, add routine_filing=True to metadata. 8-K: +30 if description contains material keyword, +15 otherwise."}`
**Notes:** Applied: 10-Q/10-K scoring reduced from +20 to +5 with routine_filing=True metadata. 8-K scoring: +30 if description contains material keyword (earn/merger/acqui/guidance/etc.), +15 otherwise. New _8k_is_material() helper in catalyst.py. GOVERNANCE NOTE: implementation preceded formal approval; reconciled at application time.
