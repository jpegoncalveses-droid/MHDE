# Finding 1 — Cross-asset ingestion root cause investigation

_Investigation date: 2026-05-14. Read-only. No code changes._

Companion to the 2026-05-14 equity prediction quality audit (`audit_equity_predictions_quality.py`).
Audit symptom: 6 of 32 features (`return_vs_spy_5d/20d`, `return_vs_sector_5d/20d`, `beta_60d`,
`vix_change_5d`) were 100% NULL on every live prediction since the 2026-05-10 retrain.

## TL;DR

| symptom | root cause |
|---|---|
| SPY, VIX, 11 sector ETFs stale ≥10 days in `prices_daily` | These tickers are **not in `companies`**, so the `YahooHistoricalIngestor` running nightly via `mhde-daily-analysis.service` never sees them. Their 251–252 rows came from a one-shot 1-year bootstrap, never a recurring schedule. |
| `polygon_sector_etf` source stale 13 days | `ingest_sector_etfs.py` is **not in `orchestrator._ALL_INGESTORS`**. Only runnable via the `data ingest-sector-etfs` CLI, which is manually invoked. |
| FRED `DGS2` stale 13 days | The FRED ingestor's `_SERIES` dict in `ingestion/ingest_fred.py:15-22` does not include `DGS2`. The 251 existing rows were written via an out-of-band manual run; the scheduled job only refreshes `DGS10`/`FEDFUNDS`/`CPIAUCSL`/`UNRATE`/`PAYEMS`/`GDP`. |
| FRED `VIXCLS` absent entirely | Same: not in `_SERIES`. (`vix_level` / `vix_change_5d` actually read `prices_daily WHERE ticker='VIX'` — see `ml/features.py:229-238` — not `macro_series`. So VIX has TWO failure modes; both broken.) |

**Common pattern:** the equity engine has two ingestion patterns — (a) ticker-driven (orchestrator iterates `companies.ticker`) and (b) series-driven (ingestor hardcodes a constant list). Every cross-asset reference is in pattern (b) but missing from the relevant constant list, or in pattern (a) but absent from `companies`. There is no third pattern for "always-needed reference tickers/series."

**Not a regression. Long-standing gap.** The FRED `_SERIES` dict has never included `DGS2`/`VIXCLS` since the initial commit `b1a47ae` (2026-05-01). The cross-asset Yahoo rows are exactly 251–252 trading days = the `_BOOTSTRAP_DAYS = 252` constant in `ingest_yahoo_historical.py:27`. Somebody backfilled them once around 2026-05-04/05 and they've been stale since — there has never been a recurring path.

---

## 1. Per-source ingestion map

### 1a. SPY, VIX, sector ETFs (XLK/XLF/XLV/XLE/XLY/XLI/XLP/XLB/XLU/XLRE/XLC)

| field | value |
|---|---|
| storage | `prices_daily` |
| ticker key | `SPY`, `VIX`, `XL*` |
| source values in DB | `yahoo` (one-time bootstrap), `polygon_sector_etf` (manual CLI) |
| feature consumers | `ml/features.py:_load_reference_prices` (line 209-226) — `WHERE ticker IN ('SPY', 'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLP', 'XLU', 'XLB', 'XLRE', 'XLY')`<br>`ml/features.py:_load_vix` (line 229-238) — `WHERE ticker = 'VIX'`<br>`ml/features.py:_compute_betas` (line 564-604) — `WHERE ticker = 'SPY'` |
| ingest module | `ingestion/ingest_yahoo_historical.py:YahooHistoricalIngestor` (path A); `ingestion/ingest_sector_etfs.py:ingest_sector_etfs_to_db` (path B) |
| orchestrator membership | path A is in `_ALL_INGESTORS` BUT operates only on `tickers` argument, which the orchestrator populates from `SELECT ticker FROM companies WHERE is_active=true ORDER BY universe_tier DESC, ticker` (orchestrator.py:81-85). SPY/VIX/XL* are **not in `companies`**. Confirmed: `SELECT ticker FROM companies WHERE ticker IN ('SPY','VIX','XLK','XLF') → []`.<br>path B is **not in `_ALL_INGESTORS`** at all. Only callable via `main.py:734 data_ingest_sector_etfs_cmd`. |
| systemd timer | None scheduled. `mhde-daily-analysis.timer` (Mon-Fri 23:15 UTC) → `mhde-daily-analysis.service` → `run_mhde_daily_analysis.sh` → `main.py run daily-radar` → orchestrator.run_all. This **does** call YahooHistoricalIngestor but with a `tickers` list that excludes the cross-assets. |
| last successful fetch | `SPY`: 2026-05-04 (yahoo, 251 rows back to 2025-05-05) ▸ `VIX`: 2026-05-05 (yahoo, 252 rows back to 2025-05-05) ▸ `XL*`: 2026-05-04 (yahoo, 251 rows each) ▸ `polygon_sector_etf` XL*: 2026-05-01 (11 rows) |

### 1b. DGS2 (and VIXCLS, T10Y2Y as expected-missing)

| field | value |
|---|---|
| storage | `macro_series` |
| series_id key | `DGS2` |
| source values in DB | `fred` (all 251 rows) |
| feature consumers | `ml/features.py:_load_yield_curve` (line 241-253) — joins `macro_series d10` (DGS10) with `macro_series d2` (DGS2) on `as_of_date` |
| ingest module | `ingestion/ingest_fred.py:FREDIngestor` |
| series enumerated in code | `_SERIES = { 'FEDFUNDS', 'DGS10', 'CPIAUCSL', 'UNRATE', 'PAYEMS', 'GDP' }` (line 15-22). **DGS2 is missing.** **VIXCLS is missing.** **T10Y2Y is missing.** |
| orchestrator membership | `FREDIngestor` is in `_ALL_INGESTORS` (orchestrator.py:12, 28) |
| systemd timer | Runs nightly via `mhde-daily-analysis` chain. Confirmed: `source_runs` shows 36 successful `fred` runs since 2026-04-14, last 2026-05-14 00:06. **Each writes 70 records** — corresponds to the 6 series × ~12 observations limit, not 7+ series. |
| last successful fetch | `DGS10`: 2026-05-12 ▸ `DGS2`: 2026-05-01 (251 rows from 2025-05-01) ▸ `VIXCLS`: never present ▸ Monthly/quarterly series last refresh ranges (`FEDFUNDS` 2026-04-01, `GDP` 2026-01-01) match FRED release cadence and are not stale. |

How did 251 DGS2 rows get there if the code never wrote them on a schedule? The same shape as the Yahoo bootstrap — manual one-shot. Likely someone temporarily added DGS2 to `_SERIES`, ran the ingestor once, then reverted (no commit in git log on `ingest_fred.py` since `b1a47ae`).

---

## 2. systemd timer map (relevant subset)

| timer | OnCalendar | service it triggers | calls | cross-asset coverage |
|---|---|---|---|---|
| `mhde-daily-analysis.timer` (user unit) | `Mon..Fri *-*-* 23:15:00 UTC` | `mhde-daily-analysis.service` | `run_mhde_daily_analysis.sh` → `main.py run daily-radar` → `orchestrator.run_all` → Polygon + Stooq + Yahoo + FRED + FINRA + CFTC + Events + FDA + Stocktwits + GDELT | **none.** Universe is `companies` only; FRED's `_SERIES` is the 6-series subset. |
| `mhde-predict.timer` (user unit) | `*-*-* 21:00:00` | `mhde-predict.service` | `main.py ml backfill-features` + `main.py ml predict` | **none.** Reads `prices_daily` + `macro_series` as-is; never ingests. |
| (no timer) | n/a | `data ingest-sector-etfs` CLI | `ingestion/ingest_sector_etfs.py:ingest_sector_etfs_to_db` | XL* via Polygon, only when manually invoked |

There is **no systemd timer covering SPY, VIX, or DGS2/VIXCLS/T10Y2Y refresh.**

---

## 3. Comparison with the working ingestion path (Stooq for primary universe)

The Stooq path works (last fresh 2026-05-13, T-1 from today). Why:

| pattern | Stooq | SPY/VIX/XL* | DGS2 |
|---|---|---|---|
| ticker source | `companies WHERE is_active=true` (504 primary + 174 extended) | hardcoded constant in features.py | hardcoded constant in `_SERIES` |
| ticker constant present? | n/a (uses universe table) | constant lives in `ml/features.py` AND `ingestion/ingest_sector_etfs.py` — both consumers; **no constant in any orchestrated ingestor** | constant lives in `ingest_fred.py:_SERIES` — but **DGS2 not in it** |
| in `_ALL_INGESTORS`? | yes (`StooqPricesIngestor`) | YahooHistoricalIngestor is in `_ALL_INGESTORS` but operates only on the universe list | yes (`FREDIngestor`) but with wrong series list |
| nightly schedule? | yes (Mon-Fri 23:15) | inherited schedule but the cross-asset tickers never enter the ticker loop | yes, but writes 6 wrong series |
| graceful degradation? | yes — Stooq fills gaps Polygon missed | n/a | n/a |
| outcome | T-1 freshness, 610 tickers | 1y old, never refreshed since bootstrap | DGS2 13d stale, VIXCLS absent |

**Structural difference:** for any ticker/series to be regularly refreshed, it must either (a) appear in `companies` so the orchestrator's universe query picks it up, OR (b) appear in a hardcoded constant list inside an ingestor that's registered in `_ALL_INGESTORS`. The cross-asset references satisfy neither. They are consumed by `ml/features.py` from constants that are defined only in the consumer, with no producer-side counterpart.

---

## 4. Git history — when did this break?

| ticker / series | first observed write | last observed write | last code change to the writer |
|---|---|---|---|
| SPY (yahoo) | 2025-05-05 | 2026-05-04 | `cfc1011` (2026-05-??): "fix(ingestion): add 429 retry with backoff in Yahoo historical ingestor" — no change to ticker selection |
| VIX (yahoo) | 2025-05-05 | 2026-05-05 | same |
| XL* (yahoo) | 2025-05-05 | 2026-05-04 | same |
| XL* (polygon_sector_etf) | (occasional) | 2026-05-01 | `68a7407` "feat(ingestion): add --delay param to sector ETF ingestor" + `785808e` "add data ingest-sector-etfs CLI command" — wires CLI, not orchestrator |
| DGS2 (fred) | 2025-05-01 | 2026-05-01 | `b1a47ae` (initial commit) is the only commit touching `ingest_fred.py`. `_SERIES` has never included DGS2 in tracked code. |
| VIXCLS (fred) | never | never | same |

**Not a regression.** No commit removed cross-asset ingestion — it was never wired up. The recent KI-142 / commit `2792056` ("fix(ingestion): stooq freshness today-exact; restore T-1 features after Polygon grouped-daily switch") restored T-1 freshness for the **main universe** after Polygon's endpoint switch; it did not address cross-asset references. The 251–252-row bootstrap appears to have been a one-time manual `main.py data update-prices --ticker SPY --ticker VIX ...` invocation or similar, run on or shortly after 2026-05-04 (consistent with `_BOOTSTRAP_DAYS=252` = 1y of trading days).

---

## 5. Fix recommendation (not implemented)

Two shapes; the first is simpler and reversible.

### Recommended — Option A: ReferenceTickersIngestor + FRED `_SERIES` extension

1. **New ingestor `ingestion/ingest_reference_tickers.py`** with a hardcoded list:
   ```python
   REFERENCE_TICKERS = ("SPY", "VIX",
                        "XLK", "XLF", "XLV", "XLE", "XLY",
                        "XLI", "XLP", "XLB", "XLU", "XLRE", "XLC")
   ```
   Reuses `YahooHistoricalIngestor`'s `_parse_yf_response` logic but bypasses the universe lookup — runs unconditionally each call. Writes to `prices_daily` with `source='yahoo'`.
2. **Register in `orchestrator._ALL_INGESTORS`** so it runs as part of the existing 23:15 chain.
3. **Extend `ingest_fred.py:_SERIES`** to include `DGS2` and `VIXCLS` (the latter as a backup; primary VIX path is `prices_daily WHERE ticker='VIX'` per `ml/features.py:233`). Optionally also `T10Y2Y` (pre-spread series) for a separate validation path.
4. **One-time backfill** post-deploy: invoke the new ingestor with a 1-year window to fill 2026-05-05 → today gap.
5. **Smoke check** in `health/ml_checks.py`: assert latest `SPY`/`VIX`/`XLK` `trade_date` ≥ T-3.

Why this shape:
- No schema change. No `companies` row pollution (SPY isn't a tradeable engine candidate; putting it in `companies` would distort tier counts).
- Decouples reference data from universe lifecycle — universe can shrink/grow without breaking macro features.
- Mirrors the existing pattern of `ingest_sector_etfs.py` but actually wires it into the scheduled run.

### Alternative — Option B: add reference tickers to `companies` with a new tier

Add `companies.universe_tier='reference'` rows for SPY/VIX/XL*. Modify `orchestrator.py:83` to include reference tier in the universe SELECT. Pro: zero new code; the universe query already drives YahooHistoricalIngestor and Polygon. Con: pollutes `companies` (used for sector statistics, universe size in `pipeline_runs`, dashboard universe counts) and adds reference tickers to every per-ticker query, requiring downstream filters. Higher blast radius than A.

### Out of scope but worth filing

- KI- to track: `polygon_sector_etf` source manual-CLI is redundant once the Yahoo reference path is wired. The `data ingest-sector-etfs` command should either be removed or re-purposed as a Polygon fallback when Yahoo 429s.
- KI- to track: `health/ml_checks.py` does not assert cross-asset reference data freshness. Adding the assertion would have caught this gap on day one rather than via a manual prediction audit on day 14.

---

## Appendix — query receipts

```sql
-- SPY/VIX/XL* freshness in prices_daily
SELECT ticker, source, MAX(trade_date), COUNT(*)
FROM prices_daily
WHERE ticker IN ('SPY','VIX','XLK','XLF','XLV','XLE','XLY','XLI','XLP','XLB','XLU','XLRE','XLC')
GROUP BY ticker, source ORDER BY ticker, source;
-- → all rows show last trade_date 2026-05-04 or 2026-05-05, counts 251–252

-- SPY/VIX/XL* in companies?
SELECT ticker FROM companies
WHERE ticker IN ('SPY','VIX','XLK','XLF','XLV','XLE','XLY','XLI','XLP','XLB','XLU','XLRE','XLC');
-- → 0 rows

-- macro_series coverage
SELECT series_id, source, COUNT(*), MAX(as_of_date)
FROM macro_series GROUP BY series_id, source;
-- → DGS2: 251 rows, fred, last 2026-05-01
-- → DGS10: 258 rows, fred, last 2026-05-12
-- → VIXCLS: absent

-- FRED source running OK
SELECT started_at::DATE, status, records_inserted
FROM source_runs WHERE source_name='fred' ORDER BY started_at DESC LIMIT 5;
-- → ok / 70 records every run; 6 series × ~12 obs each
```

Generated by: `.claude/local_scripts/audit_equity_predictions_pipeline.py` and ad-hoc DuckDB queries on 2026-05-14.
