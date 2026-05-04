# IFRS Mapping Gap Diagnostics

Generated: 2026-05-04

## Summary

| Category | Count | Tickers |
|----------|-------|---------|
| True IFRS filers — USD-reporting | 2 | GFS, UMC |
| True IFRS filers — non-USD | 2 | CVE, NOK |
| False-positive (US-GAAP, was temporarily Incomplete) | 20 | CRDO, CRWD, FIX, GD, INTC, JBL, LITE, MCHP... |

**Total enriched root cause rows:** 72 events across 24 tickers

---

## True IFRS Filers

These tickers have `ifrs-full/*` concepts in `fundamentals_raw` but no US-GAAP revenue or shares concepts.
The foreign filer guard in `features/valuation.py` nulls all valuation ratios when currency ≠ USD,
resulting in near-zero `cheap_score`.

### USD-reporting IFRS filers (safe to map)

| Ticker | IFRS Revenue Unit | IFRS EPS Unit | Safe Rev Mapping | Safe EPS Mapping |
|--------|------------------|---------------|-----------------|-----------------|
| GFS | USD | USD/shares | ✓ | ✓ |
| UMC | USD | USD/shares | ✓ | ✓ |

**Fix:** Add `ifrs-full/Revenues` (unit=USD) and `ifrs-full/EarningsPerShareDiluted` (unit=USD/shares)
as fallback concepts in `features/valuation.py`. Use a USD-unit filter via `_latest_usd_unit()`.

### Non-USD IFRS filers (cannot safely map without currency normalisation)

| Ticker | IFRS Revenue Unit | IFRS EPS Unit |
|--------|------------------|---------------|
| CVE | CAD | CAD/shares |
| NOK | EUR | EUR/shares |

**Fix:** Requires FX normalisation pipeline before valuation ratios are meaningful.
Not addressed in this change.

---

## False-Positive IFRS Tickers (US-GAAP — Were Temporarily Incomplete)

These 20 tickers have full US-GAAP concept coverage (200–700+ concepts including revenue, EPS, shares).
They appear in `ifrs_mapping_gap` because enrichment Rule 8 fires for any domestic active SEC reporter
with a filing date and `tier=Incomplete`, regardless of whether they are actually IFRS.

At the time of the missed spike event (within the 30-day lookback), these tickers had
`tier=Incomplete` — likely because `fundamentals_raw` was not yet populated for that scoring run.
Today they score as `tier=Reject` with reasonable sub-scores.

**Root cause of mislabelling:** Enrichment Rule 8 is too broad — it catches US-GAAP filers
that were temporarily incomplete. A stricter rule would check whether `ifrs-full/*` concepts exist
in `fundamentals_raw` before assigning `ifrs_mapping_gap`.

**Fix (future):** Add `ifrs-full/*` concept presence check to Rule 8 so US-GAAP filers
that had transient incompleteness are assigned a different subcause (e.g. `transient_incomplete`).
This is a root cause enrichment refinement, not a features/valuation change.

---

## Available IFRS Concepts (across all 93 IFRS-filing tickers in DB)

| Concept | Tickers |
|---------|---------|
| ifrs-full/NetIncomeLoss | 93 |
| ifrs-full/StockholdersEquity | 93 |
| ifrs-full/CashAndCashEquivalentsAtCarryingValue | 93 |
| ifrs-full/EarningsPerShareBasic | 87 |
| ifrs-full/EarningsPerShareDiluted | 86 |
| ifrs-full/Revenues | 80 |
| ifrs-full/AssetsCurrent | 68 |
| ifrs-full/OperatingIncomeLoss | 68 |
| ifrs-full/GrossProfit | 44 |

## Safe Mappings Added in This Change

| Target metric | New concept alias | Unit filter | Tickers helped |
|--------------|-------------------|-------------|----------------|
| revenue (P/S numerator) | `ifrs-full/Revenues` | unit=USD | GFS, UMC |
| EPS (P/E) | `ifrs-full/EarningsPerShareDiluted` | unit LIKE 'USD%' | GFS, UMC |
| EPS (P/E fallback) | `ifrs-full/EarningsPerShareBasic` | unit LIKE 'USD%' | GFS, UMC |

Shares outstanding remains unmapped (no IFRS shares concept in SEC XBRL).
P/S still requires shares unless computed via Polygon market_cap (future work).
