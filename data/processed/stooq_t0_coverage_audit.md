# Stooq T-0 Coverage Audit — Primary Universe (504 tickers)

_Investigation date: 2026-05-14. Read-only. No code changes._

Companion to `finding3_ml_pipeline_gap_root_cause.md`. After
identifying Polygon free-tier's current-day 403 as the upstream cause
of the May 13/14 ML pipeline gap, this audit asks: can Stooq fill the
T-0 role as the equity engine's primary daily-price source?

## TL;DR

**Stooq is NOT a viable T-0 primary source as currently configured.**
The best Stooq-only day in the last 14 trading days covered **343 of
504 primary tickers (68.1%)**, far short of the 98% bar. The 67 primary
tickers Stooq has never produced a row for include recent IPOs and
obvious mega-caps; whether Stooq's endpoint actually serves them is
unknown because the orchestrator only asks Stooq for tickers Polygon
failed on. The data we have is a lower bound — but the ceiling we *can*
observe (87% over 90d, 68% on fallback-dominant days) is still well
below the threshold to use Stooq alone for T-0.

## 1. Source-mix history (last 14 days, all tickers)

```
date         polygon  pol_sect  stooq   yahoo   total
2026-05-13         0         0     3       1     4    ← Polygon 403, only fallback noise
2026-05-12       511         0     1       1    513
2026-05-11         2         0   514       2    518   ← Stooq-dominant
2026-05-08       461         0    51       4    516
2026-05-07        57         0   455       6    518   ← Stooq-dominant
2026-05-06       513         0     3       3    519
2026-05-05       507         0    10       4    521
2026-05-04         4         0   505      15    524   ← Stooq-dominant
2026-05-01        18        11   554      51    634
2026-04-30       104        11     2     517    634   ← Yahoo-dominant
```

Three "Stooq-dominant" days (2026-05-04, 2026-05-07, 2026-05-11) where
Polygon largely failed and Stooq took over — these are the fairest
natural experiments for "Stooq as primary."

## 2. Coverage of the 504-ticker primary universe by source × day

```
date         polygon  stooq  yahoo   primary_covered / 504    pct
2026-05-12       345      0      0           345 / 504        68.5%
2026-05-11         2    343      0           345 / 504        68.5%   ← Stooq-only run
2026-05-08       315     30      0           345 / 504        68.5%
2026-05-07        32    313      1           346 / 504        68.7%   ← Stooq-only run
2026-05-06       345      1      0           346 / 504        68.7%
2026-05-05       346      0      0           346 / 504        68.7%
2026-05-04         0    344      0           344 / 504        68.3%   ← Stooq-only run
2026-05-01        12    409     26           447 / 504        88.7%   ← cross-source pile-up
2026-04-30        65      0    382           447 / 504        88.7%   ← Yahoo backfill
```

**Two structural observations:**

- Daily primary coverage from *any* source caps at **~345 / 504 =
  68.5%**. The remaining ~159 primary tickers are not landing from any
  live source on most days. (The 447 on 2026-05-01/04-30 was a
  cross-source backfill burst, not a daily-cadence number.)
- On the three Stooq-dominant days, Stooq alone produced **343 / 313 /
  344** primary rows — matching the 345 daily-ceiling almost exactly.
  So when Stooq is asked, it can deliver close to the ceiling — but
  the ceiling itself is 68%, not 98%.

## 3. Coverage ceilings (lifetime in window)

| Metric | Count | % of primary |
|---|---|---|
| Primary tickers with ≥1 Stooq row in last 7d | **343** | 68.1% |
| Primary tickers with ≥1 Stooq row in last 30d | **437** | 86.7% |
| Primary tickers with ≥1 Stooq row in last 90d | **437** | 86.7% |
| Primary tickers Stooq has **never** produced (90d) | **67** | 13.3% |
| ADRs in primary universe | **1** of 504 | 0.2% |

**Latest Stooq date per ticker (90d, only tickers Stooq ever wrote):**

```
latest_stooq_date    n_tickers
2026-05-11           343        ← last batch
2026-05-06             1
2026-05-01            93        ← prior batch
TOTAL                437
```

Coverage arrives in **bursts** (Polygon-failure days), not on a daily
cadence. There is no Stooq write between 2026-05-01 → 2026-05-04 →
2026-05-07 → 2026-05-11 spanning all 437 tickers; instead each batch
is a different ~340-ticker cohort, and 93 of them haven't been seen
since 2026-05-01 (2 weeks stale).

## 4. The 67 primary tickers Stooq has never produced

`is_adr=True` count in this set: **0** of 67. So the "ADR weak-spot"
hypothesis does **not** explain the gap.

Cross-source presence of those 67 in the last 30 days:

- polygon: 12 of 67
- yahoo: 9 of 67
- On the Polygon-fresh day 2026-05-12: only **2 of 67** were filled by
  any source.

Sample of the 67 (selected — full list available on request):

```
ticker  type / note
BF.B    Brown-Forman (Class B share — ticker punctuation likely Stooq-incompatible)
BRK.B   Berkshire Hathaway (same — '.B' suffix)
PEP     PepsiCo            ← mega-cap, no Stooq history
PFE     Pfizer             ← mega-cap, no Stooq history
PG      Procter & Gamble   ← mega-cap, no Stooq history
SCHW    Charles Schwab     ← mega-cap, no Stooq history
SMCI    Super Micro        ← recent volatility name
RKLB    Rocket Lab         ← recent IPO
SOLV    Solventum          ← 3M spinoff (2024)
PSKY    Paramount Skydance ← recent rename / IPO-like event
```

**This is not necessarily a Stooq-capability statement.** The
orchestrator calls Stooq's CSV endpoint via
`_tickers_needing_prices()` (`ingestion/ingest_stooq.py:30-47`), which
only requests tickers that lack a `prices_daily` row for *today*.
PEP/PFE/PG/SCHW get filled by Polygon every successful day → they're
never on Stooq's request list → "never seen from Stooq" is
uninformative for them. The two suspicious sub-classes are:

- **Ticker-punctuation tickers** (`BF.B`, `BRK.B`): Stooq's CSV
  endpoint uses `.us` suffix and is known to mishandle embedded `.` in
  the base symbol. These are likely real Stooq gaps.
- **Recent IPOs / corporate-action renames** (`RKLB`, `PSKY`, `SOLV`,
  `SMCI`): historically Stooq is slow to index these; the May 14
  daily-analysis log explicitly shows Yahoo handling `BRK.B 404`.
  Likely real Stooq gaps.

## 5. Compared to Polygon

Polygon (paid grouped daily endpoint, when not 403'd) returns ~504
rows / day for the in-universe primary on T-1 and earlier. T-0 is
blocked by free-tier policy. Combining the free-tier grouped behavior
with this Stooq audit:

| Tier needed | Polygon free | Stooq (current config) | Stooq (full 504-list test) |
|---|---|---|---|
| T-2 / older | ~100% | n/a (only asked when Polygon fails) | unknown |
| T-1 | ~100% (paid 200 status) | n/a | unknown |
| T-0 (today) | **0%** (403'd) | **0%** (today-exact freshness only fires when Polygon fails — KI-142 / ADR-030) | observed ceiling 68% (3 fallback-dominant days), 90d lifetime ceiling 87% |

## 6. Verdict against the ≥98% bar

Stooq is **not viable** as a sole T-0 primary source at the requested
98% bar, based on what we can observe:

- **Best naturally-observed day (2026-05-11): 68.1%** (343/504). 30+
  percentage points short.
- **Lifetime ceiling (90d ever-covered): 86.7%** (437/504). Still 11
  points short, and that's an upper bound assuming we could trigger a
  request for every ticker every day.
- The 67 never-seen tickers include a known-bad sub-class (ticker
  punctuation: BF.B, BRK.B) Stooq's endpoint format doesn't support
  cleanly.

**A fair test of Stooq's true ceiling requires actually calling
`https://stooq.com/q/l/?s=...&f=sd2t2ohlcv&h&e=csv` with the full 504
tickers** — current data only reflects Stooq-as-residual-fallback
(Polygon-misses + late-batch backfills). Such a test would write data
to the DB and is out of scope here.

## 7. Required fallback strategy if Stooq becomes primary

Independent of the exact Stooq ceiling, a fallback layer is required
for at least:

| Gap class | Examples | Suggested fallback |
|---|---|---|
| Ticker-punctuation (.B / .A class shares) | BF.B, BRK.B | Yahoo historical (already wired — its 404 for BRK.B is a separate adapter bug to fix) |
| Recent IPOs / spinoffs | RKLB, SOLV, PSKY | Yahoo + Polygon individual-ticker endpoint |
| Free-tier T-0 gap | All 504 on day-of | A paid T-0 source (Polygon paid tier, Tiingo IEX, or Alpaca) — none of the current sources solve this for free |

The May 1 / Apr 30 combined day (88.7% primary coverage) shows the
cross-source pile-up *can* exceed 88%, but only when all three of
Polygon / Stooq / Yahoo backfill the same date — that's not a T-0
capability, it's a T+1 reconciliation.

## 8. Caveats

- "Active" universe = `companies.is_active=TRUE`; 504 primary + 174
  extended = 678 total active.
- The `prices_daily.source` column distinguishes `polygon` /
  `polygon_sector_etf` / `stooq` / `yahoo`. No `tiingo` / `alpaca` /
  `iex` rows exist in the window — those adapters are not currently
  writing.
- Stooq is wrapped in `ingest_stooq.py` only — the separate
  `ingest_stooq_historical.py` is a one-shot bulk backfiller and not
  on any timer.
- ADR field reliability: `companies.is_adr` shows only 1 ADR in the 504
  primary tier. This is likely under-counted (BUD, the Anheuser-Busch
  ADR, was missing in the daily-analysis log too); if you act on the
  ADR conclusion downstream, treat that field as suspect.

Investigation script (left in repo at the time of investigation):
`.claude/local_scripts/stooq_coverage_investigation.py` (read-only,
idempotent).
