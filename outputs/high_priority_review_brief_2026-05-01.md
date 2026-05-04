# MHDE High-Priority Candidate Review Brief
**Run:** `04f667da0b68492c` — 2026-05-01  
**Scope:** 7 high-priority candidates (C-tier or 3+ section appearances)  
**Purpose:** Decide review_status for each candidate

---

## Summary Table

| Ticker | Company | Tier | Total | Cheap | Quality | Catalyst | Main reason surfaced | Main weakness | Suggested status | Usefulness |
|--------|---------|------|-------|-------|---------|----------|----------------------|---------------|-----------------|------------|
| AIG | AIG | C | 46.4 | 54.6 | 67.5 | 30.0 | C-tier (borderline) | Net margin signal noisy; weak catalyst | needs_more_evidence | 3 |
| AFL | Aflac | C | 45.1 | 43.7 | 76.3 | 30.0 | C-tier; strong quality | Cheap barely qualifies; catalyst thin | needs_more_evidence | 3 |
| CHTR | Charter Comm. | Reject | 44.9 | 81.7 | 51.7 | 50.0 | 3 sections (cheap+catalyst) | P/S=0.003 / P/B=0.002 are wrong — scaling bug | invalid_due_to_data_issue | 1 |
| ACGL | Arch Capital | Reject | 44.1 | 75.0 | 76.3 | 30.0 | 3 sections (cheap+quality) | Just below C threshold; no catalyst; no momentum | needs_more_evidence | 4 |
| BIDU | Baidu | Reject | 42.8 | 69.0 | 70.0 | 0.0 | 3 sections (cheap+quality) | Ratios in CNY not USD; EPS from 2010 | invalid_due_to_data_issue | 1 |
| BAC | Bank of America | Reject | 42.4 | 63.3 | 83.8 | 30.0 | 4 sections (highest count) | Revenue growth 302% is data artifact; no momentum | needs_more_evidence | 3 |
| CFG | Citizens Financial | Reject | 42.4 | 63.3 | 83.8 | 30.0 | 4 sections (highest count) | Revenue concept mismatch ($1.64B vs ~$7B actual) | invalid_due_to_data_issue | 2 |

---

## Candidate Briefs

---

### AIG — American International Group

**Tier:** C | **Total:** 46.4 | **Sections:** C-tier only | **Confidence:** high  
**Suggested status:** `needs_more_evidence` | **Priority:** high

#### Scores
| Component | Score | Notes |
|-----------|-------|-------|
| Cheap | 54.6 | P/S=1.59, P/E=13.8, P/B=1.04 |
| Quality | 67.5 | NI positive, dilution 0%, but net_margin feature=0.81% (conflicting) |
| Catalyst | 30.0 | 1 catalyst point (recent 8-K) |
| Momentum | 56.5 | 62 days of Polygon data |
| Sentiment | null | No short interest data |
| Risk | 0.0 | No risk penalty |

#### Valuation Metrics
| Metric | Value | Source |
|--------|-------|--------|
| Price | $74.80 | Polygon, 2026-04-30 |
| Market cap | ~$42.6B | 570M shares × $74.80 |
| P/S | 1.59 | Revenue $26.8B (Dec 2025) |
| P/E | 13.8 | EPS $5.43 (Dec 2025) |
| P/B | 1.04 | Near book value |

#### Quality Evidence
- Revenue: $26.8B (FY 2025-12-31) — insurance premiums + investment income
- Net income: $3.1B (FY 2025-12-31)
- **Net margin conflict:** feature shows 0.81% but $3.1B / $26.8B = 11.6%. Suggests the revenue concept used in the margin feature ($3.1B NI / ~$382B gross) may be inflating denominator with total assets or gross premiums. **Data quality flag — margin signal unreliable for insurance companies.**
- Dilution rate: 0% — no share dilution
- Revenue growth: 0% (YoY change may be within rounding; two-period comparison)
- EPS: $5.43 diluted (FY 2025)

#### Catalyst Evidence
- 3× 8-K filings on 2026-04-30 — scored as 1 catalyst point
- No earnings calendar event found (events table empty)
- No short interest data available (FINRA returned 0 records)
- No press release events ingested
- Momentum: 20d return −1.01% (flat), 60d return +0.9% (flat), volume spike 2.79× (notable — above-average volume recently)

#### Momentum
- Close: $74.80 (2026-04-30)
- 62 days of Polygon history available
- 20d return: −1.01% | 60d return: +0.9%
- Drawdown from 62d high: −4.93%
- Volume spike: 2.79× 20d avg — elevated activity noted

#### Risks / Missing Data
- Sentiment: null (no short interest)
- Net margin signal unreliable for insurance (uses gross revenue concept)
- Catalyst is 3 boilerplate 8-Ks; no earnings date found
- AIG has been restructuring (divested life/retirement businesses 2021-2022); core insurance business quality needs manual verification

**System thesis:** Large-cap insurer trading near book value (P/B=1.04) with positive earnings and low P/E — cheapness is real if core insurance margins hold.

**Skeptical counter:** The 8-Ks are likely routine SEC filings, not earnings catalysts. Net margin ambiguity weakens quality confidence. No upcoming catalyst visible. Insurance companies near book value can stay there indefinitely without a trigger.

**Suggested scores:**
- usefulness_score: 3
- thesis_quality_score: 3
- evidence_quality_score: 3

**Suggested missing_evidence:** Actual 8-K descriptions (were they material events?), earnings call transcript, reserve release or claims ratio trend, short interest data

**Suggested review_notes:** Net margin feature inconsistent with NI/revenue calculation — insurance-specific revenue concepts inflate denominator. P/E=13.8 and P/B=1.04 look real. Volume spike warrants watching. Catalyst is too thin for conviction. Hold at C for now.

---

### AFL — Aflac Incorporated

**Tier:** C | **Total:** 45.1 | **Sections:** C-tier only | **Confidence:** high  
**Suggested status:** `needs_more_evidence` | **Priority:** high

#### Scores
| Component | Score | Notes |
|-----------|-------|-------|
| Cheap | 43.7 | P/S=3.54, P/E=16.7, P/B=2.06 — passes but not compelling |
| Quality | 76.3 | Strong net margin 21.24%, 0% dilution |
| Catalyst | 30.0 | 1 catalyst point (recent 8-K) |
| Momentum | 54.1 | 62 days of Polygon data; slight uptrend |
| Sentiment | null | No short interest data |
| Risk | 0.0 | No risk penalty |

#### Valuation Metrics
| Metric | Value | Source |
|--------|-------|--------|
| Price | $113.67 | Polygon, 2026-04-30 |
| Market cap | ~$60.8B | 535M shares × $113.67 |
| P/S | 3.54 | Revenue $17.16B (FY 2025) |
| P/E | 16.7 | EPS $6.82 diluted (FY 2025) |
| P/B | 2.06 | Moderate book premium |

#### Quality Evidence
- Revenue: $17.16B (FY 2025-12-31)
- Net income: $3.65B (FY 2025-12-31)
- Net margin: 21.24% — **strong and reliable for insurance** (Aflac is a supplemental insurer, not a P&C generalist; margins are characteristically stable)
- Dilution rate: 0% — active buyback program historically
- Revenue growth: 0% YoY — **flag**: Aflac's actual growth is ~low single digits; this likely reflects a single-period comparison. Not a red flag but worth noting.
- EPS: $6.82 diluted (FY 2025)
- Price vs 52w high: 95.26% — near 52-week high

#### Catalyst Evidence
- 8-K filed 2026-04-29 + multiple Schedule 13G (institutional ownership updates — not catalysts)
- **No earnings date found in events table**
- No short interest data (FINRA 0 records)
- Momentum: 20d +3.68%, 60d +1.45%, volume spike 2.05× — mild upward trend

#### Risks / Missing Data
- P/S=3.54 is the weakest component — cheap score 43.7 barely qualifies
- Near 52-week high ($107/108 range historically) — limited upside from price compression alone
- Catalyst score 30 = routine filing only, no material event
- Sentiment null

**System thesis:** High-quality supplemental insurer with 21% net margin, 0% dilution, and positive earnings — quality is genuine. P/E=16.7 is reasonable for this business quality.

**Skeptical counter:** P/S=3.54 makes it the least cheap of the C-tier. Aflac is a mature, stable, widely-covered large-cap — the "alpha" from MHDE surfacing it is low. No catalyst visible. Near 52-week high suggests limited near-term mean reversion trade.

**Suggested scores:**
- usefulness_score: 3
- thesis_quality_score: 3
- evidence_quality_score: 3

**Suggested missing_evidence:** Earnings announcement date for Q1 2026, short interest trend, Japan currency exposure impact (Aflac earns ~70% revenue in Japan)

**Suggested review_notes:** Most credible of the C-tier pair. Quality is real. But this is a well-known, analyst-covered name — MHDE adds little edge here. Value as a system calibration test case: if AFL consistently scores C, threshold calibration may be off for large-cap insurers. No catalyst found.

---

### CHTR — Charter Communications

**Tier:** Reject | **Total:** 44.9 | **Sections:** Reject (top), Cheap (top), Catalyst (top)  
**Suggested status:** `invalid_due_to_data_issue` | **Priority:** high

#### Scores
| Component | Score | Notes |
|-----------|-------|-------|
| Cheap | 81.7 | **P/S=0.003, P/B=0.002 — likely unit scaling error** |
| Quality | 51.7 | Net income $1.16B, revenue −75% growth (red flag) |
| Catalyst | 50.0 | 2 catalyst points — Form 4 insider filings |
| Momentum | null | Stooq only (1 day) |
| Risk | 25.0 | Standard baseline |

#### Valuation Metrics (FLAGGED — DATA QUALITY)
| Metric | Value | Notes |
|--------|-------|-------|
| Price | $173.02 | Stooq, 2026-05-01 |
| Market cap | Cannot compute | Shares not in DB (CHTR uses ADC/warrants structure) |
| P/S | **0.003** | ⚠ Impossible — actual CHTR P/S ~0.7–0.9 |
| P/E | 18.87 | $173/$9.17 EPS — plausible but EPS from Q1 2026 |
| P/B | **0.002** | ⚠ Impossible — Charter has negative book equity |

**Root cause of bad P/S and P/B:** CHTR shares are not in `fundamentals_raw` via the expected XBRL concept, so the formula fell back to a shares value that is implausibly small (or zero-adjacent), producing near-zero ratios. Charter is also heavily indebted with negative stockholders' equity (LBO capital structure), so `us-gaap/StockholdersEquity` is likely negative, yet P/B computed as positive near zero — both signals break the valuation model.

#### Quality Evidence
- Revenue: $13.6B (Q1 2026-03-31) — cable/broadband
- Net income: $1.16B (Q1 2026)
- Revenue growth: **−75.18%** — not credible for quarterly cable revenue; likely a period mismatch (TTM vs single quarter comparison)
- Net margin: 8.55% — plausible for cable
- No dilution rate (shares XBRL unavailable)

#### Catalyst Evidence
- Recent filings: Form 4 (insider trades) on 2026-04-28 and 2026-04-29 — **not material catalysts**
- Catalyst score 50 = 2 points. Form 4 filings are routine director/officer stock transactions
- No earnings date, no 8-K events
- Momentum: null (Stooq single-day only)

#### Risks / Missing Data
- P/S and P/B are data artifacts — cheap_score 81.7 is **not real**
- Negative book equity (typical for leveraged cable operators) breaks P/B formula
- Revenue growth −75% is a comparison artifact
- CHTR has real business challenges: cord-cutting, fiber competition (T-Mobile, Verizon, AT&T)

**System thesis (system's view):** Cable operator appearing cheap by P/S — not real.

**Skeptical counter:** Every metric driving this candidate's score is broken data. Do not trust.

**Suggested scores:**
- usefulness_score: 1
- thesis_quality_score: 1
- evidence_quality_score: 1

**Suggested missing_evidence:** N/A — data quality issue, not evidence gap

**Suggested review_notes:** CHTR surfaced because of a P/S and P/B scaling bug. Shares XBRL concept missing → near-zero market cap in formula → P/S≈0. Charter has negative equity → P/B breaks. Catalyst score driven by Form 4 routine insider trades. Revenue growth −75% is a period mismatch artifact. Full `invalid_due_to_data_issue`. Separately: need to fix shares concept lookup for companies with unusual capital structures.

---

### ACGL — Arch Capital Group

**Tier:** Reject | **Total:** 44.1 | **Sections:** Reject (top), Cheap (top), CheapQuality/NoCatalyst  
**Suggested status:** `needs_more_evidence` | **Priority:** high (strongest non-C-tier candidate)

#### Scores
| Component | Score | Notes |
|-----------|-------|-------|
| Cheap | 75.0 | P/S=1.78, P/E=8.15, P/B=1.47 |
| Quality | 76.3 | Net margin 22%, NI positive, 0% dilution |
| Catalyst | 30.0 | 1 point — Schedule 13G filings only |
| Momentum | null | Stooq only (1 day), no history |
| Sentiment | null | No short interest |
| Risk | 25.0 | Standard baseline |

#### Valuation Metrics
| Metric | Value | Source |
|--------|-------|--------|
| Price | $94.49 | Stooq, 2026-05-01 |
| Market cap | ~$35.5B | 375.9M shares × $94.49 |
| P/S | 1.78 | Revenue $19.93B (FY 2025) |
| P/E | **8.15** | EPS $11.60 (FY 2025) — **compelling** |
| P/B | 1.47 | Moderate book premium, reasonable for reinsurer |

#### Quality Evidence
- Revenue: $19.93B (FY 2025-12-31) — specialty insurance + reinsurance + mortgage
- Net income: $4.4B (FY 2025-12-31)
- Net margin: **22.07%** — strong and credible for a specialty reinsurer
- Dilution rate: 0% — no dilution
- Revenue growth: 0% — likely reflects period comparison; Arch Capital has been a strong grower; needs manual verification
- EPS: $11.60 diluted (FY 2025)

#### Catalyst Evidence
- Filings: Schedule 13G × 3 on 2026-04-29 — **institutional ownership reports, not catalysts**
- **No earnings date found**
- No short interest, no price events
- Momentum: null — Stooq single-day, no historical price data ingested

**Why just below C-tier:** Total=44.1 vs threshold=45. With no momentum (null) and catalyst=30, the formula puts it just below. If momentum were +5%, it would clear C.

#### Risks / Missing Data
- Momentum entirely missing (no Polygon data, Stooq single-day)
- Catalyst: only institutional ownership filings — no near-term event
- Bermuda-domiciled specialty reinsurer — less analyst visibility in standard databases
- Revenue growth YoY = 0% needs verification; Arch typically grows at ~10–15% but the XBRL comparison may not reflect that

**System thesis:** Specialty reinsurer at P/E=8.15 with 22% net margins and no dilution — genuinely undervalued relative to quality if growth is real.

**Skeptical counter:** 0.9 points below C-tier is marginal rejection. Missing momentum means we cannot confirm any near-term price confirmation. No catalyst. "Cheap quality" without a trigger is a value trap risk.

**Suggested scores:**
- usefulness_score: 4
- thesis_quality_score: 4
- evidence_quality_score: 3

**Suggested missing_evidence:** 20+ days of price history (accumulates over daily runs), Arch Capital Q1 2026 earnings date, short interest trend, revenue growth verification from 10-K

**Suggested review_notes:** Most interesting reject in this batch. P/E=8.15 and 22% net margin are credible. Only 0.9 pts below C-tier due to missing momentum. Once price history accumulates (20+ days) and catalyst appears (earnings), likely to clear C. Mark as watch — revisit after 2 weeks of daily runs.

---

### BIDU — Baidu, Inc.

**Tier:** Reject | **Total:** 42.8 | **Sections:** Reject (top), Cheap (top), CheapQuality/NoCatalyst  
**Suggested status:** `invalid_due_to_data_issue` | **Priority:** high

#### Scores
| Component | Score | Notes |
|-----------|-------|-------|
| Cheap | 69.0 | P/S=0.034, P/E=1.25 — values in CNY, not USD |
| Quality | 70.0 | NI positive, momentum positive 20d |
| Catalyst | 0.0 | No catalyst signals |
| Momentum | 45.8 | 62 days Polygon — mixed signals |
| Sentiment | null | No short interest |
| Risk | 0.0 | No risk penalty |

#### Valuation Metrics (FLAGGED — CURRENCY MISMATCH)
| Metric | Value | Notes |
|--------|-------|-------|
| Price | $126.53 | Polygon ADR price, USD |
| Market cap | Cannot compute | Shares from 2010 data (34.9M — wrong) |
| P/S | **0.034** | ⚠ Revenue $129B is CNY, price is USD |
| P/E | **1.25** | ⚠ EPS $15.30 from **2010** filing — stale by 15 years |
| P/B | **0.017** | ⚠ Book value likely CNY, price USD |

**Root cause:** Baidu files with the SEC as a foreign private issuer (20-F). Its XBRL data is denominated in **Chinese yuan (CNY)**. The valuation formula uses the price in USD against fundamentals in CNY. $126.53 USD / CNY 129B revenue produces a meaningless ratio. Additionally, the EPS value used is from a 2010 filing — 15-year-old data.

#### Quality Evidence
- Revenue: $129.08B (actually ≈CNY 129B ≈ USD $18B at 7:1 rate) — FY 2025
- Net income: $5.59B (CNY 5.59B ≈ USD $0.78B — Baidu's actual profit)
- Net margin: null — margin feature not computed (XBRL concept mismatch for margin)
- Revenue growth: null
- EPS: $15.30 from **2010** — **completely stale, do not use**
- Shares: 34.9M from 2010 — **stale and wrong** (actual ~1.37B ADS equivalent)

#### Catalyst Evidence
- 6-K filings (2026-04-23, 2026-04-30 × 2) — 6-K is the foreign private issuer equivalent of 8-K. Could be material but **description is null** (not ingested).
- Catalyst score = 0.0 — no points awarded despite 6-K filings (6-K not in catalyst scoring logic)
- No earnings date
- Momentum: 20d +13.07% (positive), 60d −12.65% (prior decline), volume spike 0.82× (below avg)

#### Risks / Missing Data
- All USD-denominated ratios are wrong (CNY mismatch)
- EPS from 2010 (stale 15 years)
- Shares outstanding from 2010 (stale)
- ADR structure (each ADS = 10 ordinary shares)
- Geopolitical / regulatory risk not captured by any feature
- 6-K catalyst not scored (foreign filer gap in scoring logic)

**System thesis (system's view):** Chinese tech giant appearing cheap by P/S — not real (currency mismatch).

**Skeptical counter:** Every USD-denominated valuation metric is wrong. Even correcting for CNY, Baidu's actual P/E (~15–20×) and P/S (~2–3×) are not especially cheap. Do not trust.

**Suggested scores:**
- usefulness_score: 1
- thesis_quality_score: 1
- evidence_quality_score: 1

**Suggested missing_evidence:** N/A — data quality issue

**Suggested review_notes:** BIDU is a foreign private issuer (20-F) reporting in CNY. The valuation formula breaks for ADRs with CNY fundamentals. EPS used is 15 years stale. Fix required: (1) detect foreign filer currency from XBRL metadata and skip USD-basis ratios, or (2) exclude ADRs from valuation scoring. Mark `invalid_due_to_data_issue`. Separately: 6-K filings should be added to catalyst scoring logic.

---

### BAC — Bank of America Corporation

**Tier:** Reject | **Total:** 42.4 | **Sections:** Reject (top), Cheap (top), Quality (top), CheapQuality/NoCatalyst (4 sections — highest)  
**Suggested status:** `needs_more_evidence` | **Priority:** high

#### Scores
| Component | Score | Notes |
|-----------|-------|-------|
| Cheap | 63.3 | P/S=3.62, P/E=14.0, P/B=1.35 |
| Quality | 83.8 | NI $30.5B, margin 27%, dilution −1.6% (buybacks) |
| Catalyst | 30.0 | 1 point — recent 424B2 filings |
| Momentum | null | Stooq only (1 day) |
| Sentiment | null | No short interest |
| Risk | 25.0 | Standard baseline |

#### Valuation Metrics
| Metric | Value | Source |
|--------|-------|--------|
| Price | $53.285 | Stooq, 2026-05-01 |
| Market cap | ~$409B | 7,680M shares × $53.285 |
| P/S | 3.62 | Revenue $113.1B (FY 2025) |
| P/E | 14.0 | EPS $3.81 diluted (FY 2025) |
| P/B | 1.35 | Moderate premium to book |

#### Quality Evidence
- Revenue: $113.1B (FY 2025-12-31) — total bank revenue (NII + non-interest)
- Net income: $30.51B (FY 2025-12-31)
- Net margin: **26.98%** — consistent with a large US bank
- Dilution: −1.6% (share count declining — active buyback)
- **Revenue growth: 302.65%** — ⚠ This is a data artifact. BAC's actual FY25 revenue did not grow 300%. This likely reflects a change in revenue concept across reporting periods (e.g., XBRL concept switch between quarters). Do not trust this figure.
- EPS: $3.81 diluted (FY 2025)

#### Catalyst Evidence
- Recent filings: 424B2 (prospectus supplement for structured notes) × 3 on 2026-04-30 and 2026-05-01
- **424B2 filings are routine capital markets activity — not a business catalyst**
- Catalyst score 30 = 1 point from filing recency only
- No earnings date found
- Momentum: null — Stooq single-day (no historical data)

#### Risks / Missing Data
- Revenue growth 302% is a XBRL period mismatch artifact — not real
- No momentum history (Stooq only)
- No sentiment / short interest
- BAC is a mega-cap ($400B market cap) — MHDE is not designed to edge large-caps effectively
- 424B2 catalyst signal is noise

**System thesis:** Large US bank at P/E=14 and P/B=1.35 with 27% net margins and active buybacks — reasonable value for quality.

**Skeptical counter:** BAC is one of the most-covered stocks in the world. Surfacing BAC as a high-priority candidate from a 100-stock sample reflects an algorithm calibrated for smaller names being applied to mega-caps. The quality score is real but offers no edge. Revenue growth 302% is wrong.

**Suggested scores:**
- usefulness_score: 3
- thesis_quality_score: 2
- evidence_quality_score: 2

**Suggested missing_evidence:** Historical price data (20+ days), short interest, Q1 2026 earnings results, revenue concept verification

**Suggested review_notes:** BAC's quality metrics (NI $30.5B, margin 27%, buybacks) are real. P/E=14 is fair for the sector. However: (1) revenue growth 302% is a bug — need to verify XBRL concept consistency, (2) 424B2 is not a catalyst, (3) this is a mega-cap where MHDE generates no informational edge. Useful as a data quality test case but not an actionable thesis. `needs_more_evidence` pending revenue growth fix.

---

### CFG — Citizens Financial Group

**Tier:** Reject | **Total:** 42.4 | **Sections:** Reject (top), Cheap (top), Quality (top), CheapQuality/NoCatalyst (4 sections — tied highest)  
**Suggested status:** `invalid_due_to_data_issue` | **Priority:** high

#### Scores
| Component | Score | Notes |
|-----------|-------|-------|
| Cheap | 63.3 | P/S=3.42, P/E=16.7, P/B=1.07 |
| Quality | 83.8 | Same score as BAC — suspicious |
| Catalyst | 30.0 | 1 point — Schedule 13G only |
| Momentum | null | Stooq only (1 day) |
| Sentiment | null | No short interest |
| Risk | 25.0 | Standard baseline |

#### Valuation Metrics
| Metric | Value | Source |
|--------|-------|--------|
| Price | $64.56 | Stooq, 2026-05-01 |
| Market cap | ~$28.2B | 436.9M shares × $64.56 |
| P/S | 3.42 | Revenue $1.64B (wrong concept — see below) |
| P/E | 16.7 | EPS $3.86 diluted (FY 2025) — plausible |
| P/B | 1.07 | Near book value |

#### ⚠ Critical Data Issue: Revenue Concept Mismatch
CFG's revenue in `fundamentals_raw` is **$1.64B** from `us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax`. This concept captures **fee income only** (credit card fees, service charges, etc.) — not CFG's total revenue. Citizens Financial's actual total revenue (net interest income + non-interest income) is approximately **$6.5–7.5B**. The P/S ratio of 3.42 is computed against $1.64B, not $7B, making it directionally wrong.

**Why BAC and CFG have the same quality score (83.8):** Both have net_income_positive, net_margin ≥ 22%, dilution ≤ 0%, and revenue_growth ≥ 200%. The revenue growth figure for CFG is **289.38%** — same artifact as BAC. Two-period XBRL comparison across different revenue concepts. Not real growth.

#### Quality Evidence
- Revenue: $1.64B (wrong concept — fee income only, not total bank revenue)
- Net income: $1.83B (FY 2025-12-31) — **net income exceeds fee revenue = denominator is wrong**
- Net margin: 22.2% (using $1.64B as denominator — meaningless; actual margin is ~25%)
- Dilution: −0.51% (mild buyback)
- Revenue growth: 289.38% — data artifact (concept switch)
- EPS: $3.86 diluted (FY 2025) — plausible

#### Catalyst Evidence
- Schedule 13G × 2 (2026-04-29) + Form 4 (2026-04-24) — **not catalysts**
- No 8-K, no earnings date
- Catalyst score 30 = 1 point from filing recency

#### Risks / Missing Data
- Revenue concept wrong ($1.64B fee income vs $7B total bank revenue)
- Net income > fee revenue is a logical impossibility — clear signal of wrong concept
- Revenue growth 289% is artifact
- No momentum history, no sentiment

**System thesis (system's view):** Regional bank near book value — looks cheap and quality. Not real.

**Skeptical counter:** The P/S ratio is computed against fee income, not total revenue. When corrected, P/S ≈ 1.2–1.5, not 3.4. Quality score shares the same revenue growth artifact as BAC. The real business (P/E=16.7, P/B=1.07) is fairly valued for a mid-tier regional bank — no edge signal.

**Suggested scores:**
- usefulness_score: 2
- thesis_quality_score: 1
- evidence_quality_score: 1

**Suggested missing_evidence:** N/A (data quality issue drives the problem)

**Suggested review_notes:** CFG's `RevenueFromContractWithCustomerExcludingAssessedTax` = fee income, not total bank revenue. NI ($1.83B) > revenue ($1.64B) is impossible — dead giveaway. Revenue growth 289% is cross-concept comparison artifact. Mark `invalid_due_to_data_issue`. Fix required: for banks, use `us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax` only as a fallback if other revenue concepts return nothing, or better yet, use bank-specific concepts (InterestAndDividendIncomeOperating + NoninterestIncome). P/E=16.7 and P/B=1.07 from the EPS data look plausible but insufficient to score without correct revenue.

---

## Data Quality Issues Surfaced by This Review

| Issue | Affected Tickers | Fix Required |
|-------|-----------------|--------------|
| Shares XBRL concept missing → P/S ≈ 0 | CHTR (negative equity, warrants structure) | Skip P/S when shares unavailable; detect negative equity for P/B |
| Foreign filer CNY denominator vs USD price | BIDU (and other ADRs: AMX, AZN, BBVA, etc.) | Detect 20-F filers or non-USD XBRL; skip USD ratio computation |
| Revenue concept for banks = fee income only | CFG (and likely other banks) | Add bank-specific revenue concepts; validate NI < revenue |
| Revenue growth % = XBRL concept switch artifact | BAC, CFG (302%, 289%) | Require same concept across both periods for growth computation |
| Net margin unreliable for insurance | AIG | Note insurance revenue concepts inflate denominator; consider skipping margin for SIC 6300 |
| 6-K filings not scored as catalysts | BIDU | Add 6-K to catalyst scoring (equivalent of 8-K for foreign filers) |
| Stale EPS / shares (BIDU 2010 data) | BIDU | Add staleness check — reject fundamental data > 5 years old |

---

*Generated: 2026-05-01 | Run ID: 04f667da0b68492c | Source: MHDE engine*
