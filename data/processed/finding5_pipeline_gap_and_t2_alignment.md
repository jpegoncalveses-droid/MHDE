# Finding 5 — Pipeline freshness gap (2026-05-15) + Step 6 T-2 alignment scope

**Investigation date:** 2026-05-15
**Mode:** read-only
**Trigger:** operator-observed gap — `ml_predictions.MAX(prediction_date)=2026-05-12`
on the morning of 2026-05-15; under T-2 cadence today's run should have produced
`prediction_date=2026-05-13`.

This report answers two coupled questions:

1. **Why didn't today's pipeline produce a 2026-05-13 prediction?**
2. **What does "T-2 alignment retrain" (resumption-queue Step 6) actually
   mean, and is it still needed after KI-149 (strict freshness) + Step 5
   (dashboard T-2 labelling)?**

The headline: today's pipeline did exactly what KI-149 told it to do —
**skipped honestly** rather than silently scoring on stale data. The
upstream gap is real, persistent (not a one-day blip), and caused by an
interaction between Polygon free-tier's current-day 403 and the
non-Polygon fallback ingestors. **Step 6 as currently framed in
`EQUITY_WORKSTREAM_PAUSED.md` is unnecessary** — model retraining
does not change calibration in any way that addresses the observed gap.
The remaining work is a freshness-check change (scan backward for latest
fully-covered date) so the pipeline emits predictions every weekday on
the latest clean trade_date, plus a small downstream contract surface.

---

## 1. Today's pipeline execution — what happened

**Unit:** `mhde-predict.service` (system-level, `/etc/systemd/system/mhde-predict.service`).
**ExecStart chain:**
```
/home/jpcg/MHDE/venv/bin/python main.py ml backfill-features
/home/jpcg/MHDE/venv/bin/python main.py ml predict
```
**Timer:** `mhde-predict.timer` — `OnCalendar=*-*-* 00:15:00 UTC`.
**Log:** `/home/jpcg/MHDE/data/logs/equity_predict.log` (the user-level
`mhde-predict.service` writing to `ml_predict.log` is stale since
2026-05-06 — the active service is the system-level unit).

**Today's fire — 2026-05-15 00:15 UTC.** Timer fired on schedule; service
ran to completion with exit 0. Outcome: **skipped by design**.

The relevant tail of `data/logs/equity_predict.log`:
```
WARNING DuckDB write lock held; retrying in 30s ... (PID 2555449)
INFO Computing features for 416 tickers
INFO   Loading reference data (SPY, sector ETFs, VIX, yield curve)...
INFO   Loading fundamentals...
INFO   Price features: 90/416 tickers (22826 rows)
...
INFO   Price features: 416/416 tickers (105517 rows)
INFO   Computing filing counts and beta (SQL updates)...
INFO Features complete: 105517 total rows
Features computed: 105,517 rows
2026-05-15 00:20:33,974 mhde.ml.pipeline  INFO     Starting ML prediction pipeline
2026-05-15 00:20:33,979 mhde.ml.pipeline  WARNING  DATA STALE — skipping equity
   prediction. Equity prices_daily latest=2026-05-14 (1 trading-day gap;
   threshold=2); partial coverage on 2026-05-14: 68 rows < expected ≥335
```

The `backfill-features` stage **did** compute features (105,517 rows
across all historical trade_dates). The `predict` stage entered
`run_prediction_pipeline()` and was rejected at the very first step by
`pipelines/freshness.py:check_equity_freshness()` — the KI-149
coverage-aware check.

`ml_prediction_pipeline.py:43-46`:
```python
freshness = check_equity_freshness(conn)
if not freshness.is_fresh:
    logger.warning("DATA STALE — skipping equity prediction. %s", freshness.message)
    return {"skipped": "stale_data", "freshness": freshness}
```

The pipeline returned `{"skipped": "stale_data"}`, completed cleanly, and
exited 0. **No traceback, no crash. The KI-149 fix is working
exactly as intended.**

---

## 2. The failure-mode chain — why coverage is partial today

**Question for diagnosis:** why does `prices_daily.MAX(trade_date)`
report 2026-05-14 with only 68 rows when 2026-05-13 has 536 rows?

The answer is in `data/logs/daily_analysis_2026-05-14.log` (the 23:15 UTC
run on May 14, which prepared the data state that today's 00:15 UTC
predict run consumed):

| Ingestor | 2026-05-14 result | Notes |
|---|---|---|
| `polygon` | HTTP 403 on grouped, `in_universe=0`, raised `IngestionError` post-loop | Documented free-tier behavior; current-day endpoint blocked. Raise is the KI-149 fix. |
| `stooq` | `0 rows inserted for 0/520 tickers (failed=520)` | Stooq failed entirely; not a fallback source for May 14. |
| `yahoo_historical` | `13809 rows inserted for 58 tickers` (across all dates returned) | Exact-today match contributed **~55 universe rows for 2026-05-14**. |
| `reference_tickers` | `3264 rows inserted across 13 tickers` (across all dates) | Yahoo-backed SPY/VIX/XL\* sector ETFs — contributed **13 rows for 2026-05-14**. |
| `fred` | `94 observations inserted` (FRED rates) | DGS10/DGS2/VIXCLS up to 2026-05-13 only. |

Total 2026-05-14 rows in `prices_daily`: **68** (all from `yahoo` source).

Polygon's prior-day grouped query (2026-05-13) returned 518 universe
rows successfully — Polygon's free-tier unlocks dates the morning of T+1.
So **2026-05-13 has 536 rows total** (519 Polygon + 14 Yahoo + 3 Stooq),
which is full universe coverage.

The orchestrator does NOT abort on Polygon's `IngestionError`. It logs
the crash and the loop continues. Subsequent ingestors (Stooq, Yahoo
historical, ReferenceTickers, FRED) run and insert what they can. The
partial fallback rows for 2026-05-14 bump `MAX(trade_date)` to
2026-05-14 — high enough to trigger the KI-149 coverage check, low
enough (68 << 335) to fail it.

---

## 3. Upstream freshness state (verbatim, from DuckDB read-only audit)

Audit script: `.claude/local_scripts/finding5_freshness_audit.py`
(gitignored under `.claude/local_scripts/`). Output reproduced below.

### 3a. `prices_daily` row count per recent trade_date

| trade_date | rows | comment |
|---|---:|---|
| 2026-05-14 | **68** | Yahoo only (55 universe + 13 reference). Polygon 403'd. |
| 2026-05-13 | 536 | Polygon 519 + Yahoo 14 + Stooq 3 — full coverage. |
| 2026-05-12 | 684 | Full coverage. |
| 2026-05-11 | 689 | Full coverage. |
| 2026-05-08 | 687 | Full coverage. |
| 2026-05-07 | 586 | Full coverage. |
| 2026-05-06 | 587 | Full coverage. |
| 2026-05-05 | 588 | Full coverage. |
| 2026-05-04 | 579 | Full coverage. |
| 2026-05-01 | 689 | Full coverage. |

### 3b. KI-149 coverage math on the latest date

```
latest          : 2026-05-14
latest_count    : 68
mean_prior_30   : 669.8
expected_min    : 335   (0.5 × mean_prior_30; see pipelines/freshness.py:66)
fresh?          : False
```

### 3c. `prices_daily` source distribution, latest 3 dates

| trade_date | source | rows |
|---|---|---:|
| 2026-05-14 | yahoo | **68** |
| 2026-05-13 | polygon | 519 |
| 2026-05-13 | yahoo | 14 |
| 2026-05-13 | stooq | 3 |
| 2026-05-12 | polygon | 669 |
| 2026-05-12 | yahoo | 14 |
| 2026-05-12 | stooq | 1 |

Yahoo-only days are the smoking gun: Polygon is the dominant ingestor
when current-day-blocking does not apply.

### 3d. Top-10 mega-cap latest dates

`AAPL`, `MSFT`, `GOOGL`, `AMZN`, `META`, `NVDA`, `AVGO`, `BRK.B`, `JPM` →
**2026-05-13**. `TSLA` → 2026-05-14 (caught by Yahoo's exact-today match).

### 3e. Reference tickers (`SPY`, `VIX`, `XL*`) freshness

All 13 → **2026-05-14**. ReferenceTickersIngestor (the
`feat-cross-asset-ingestion` work merged 2026-05-14) is doing its job:
SPY/VIX/sector ETFs are pulled directly from Yahoo, which delivers T-0.
Cross-asset features are not the limiting factor.

### 3f. `macro_series` freshness per series

| series_id | MAX(as_of_date) |
|---|---|
| `DGS10` | 2026-05-13 |
| `DGS2` | 2026-05-13 |
| `VIXCLS` | 2026-05-13 |
| `CPIAUCSL` | 2026-04-01 (monthly) |
| `FEDFUNDS` | 2026-04-01 (monthly) |
| `GDP` | 2026-01-01 (quarterly) |
| `PAYEMS` | 2026-04-01 (monthly) |
| `UNRATE` | 2026-04-01 (monthly) |
| CFTC series | 2026-05-05 (weekly) |

DGS10/DGS2/VIXCLS are all T-2 fresh; the FRED publication lag means
they will not advance to 2026-05-14 until ~the morning of 2026-05-15
(today). Not a blocker for a 2026-05-13 prediction_date.

### 3g. `ml_features` and `ml_predictions` state

| trade_date | `ml_features` rows | `ml_predictions` rows |
|---|---:|---:|
| 2026-05-14 | 5 | — |
| 2026-05-13 | **415** | — (would-be today's run) |
| 2026-05-12 | 415 | **32** ← last successful prediction_date |
| 2026-05-11 | 415 | 35 |
| 2026-05-08 | 415 | 63 |
| 2026-05-07 | 317 | 58 |

**Key observation:** 2026-05-13 has 415 fully-computed feature rows
(the `backfill-features` stage ran cleanly today and produced them).
Predictions could be scored on 2026-05-13 immediately — the only thing
blocking it is the freshness check looking at `MAX(prices_daily)` =
2026-05-14 instead of "latest fully-covered date" = 2026-05-13.

---

## 4. Diagnosis — one-day blip vs persistent issue

**This is persistent / systemic, not a blip.** Three coupled facts make
the same condition recur every weekday morning:

1. **Polygon free-tier 403 on current-day grouped is a permanent,
   documented behavior.** `adapters/polygon.py` records it; the
   2026-05-13 and 2026-05-14 daily-analysis logs both show identical
   `{'grouped_status': 403, 'in_universe': 0, 'current_day_blocked': True}`
   for the day they ran on. Free-tier date access unlocks at ~T+1.
2. **Fallback sources (Yahoo historical exact-today match,
   ReferenceTickersIngestor) succeed on the same date Polygon 403'd.**
   Yahoo has T-0 quote data; ReferenceTickersIngestor pulls 13 ETFs from
   Yahoo unconditionally. These will continue to insert ~60–80 rows
   into `prices_daily` for the current calendar date every evening at
   23:35 UTC.
3. **The KI-149 coverage check uses `MAX(trade_date)`, not
   "latest-fully-covered."** `pipelines/freshness.py:85` runs
   `SELECT MAX(trade_date) FROM prices_daily`, then checks coverage
   on that single date. If that date is partial, the entire freshness
   call returns `is_fresh=False` and the pipeline skips. There is no
   scan-backward to find the most recent date that satisfies the
   coverage threshold.

So the operational pattern is:

- **Tomorrow (Sat) and the weekend:** no Polygon ingestion runs because
  daily-analysis runs Mon-Fri (`mhde-daily-analysis.timer` is the
  weekday-only one driving the chain). The state persists: predictions
  still anchored at 2026-05-12, no new ones produced.
- **Monday morning (2026-05-18):** Polygon will have unlocked 2026-05-15
  by then, so daily-analysis at 23:15 UTC Sun (2026-05-17) won't run
  (weekend skip)... wait — daily-analysis timer fires Sun-Fri at 23:15
  UTC actually (`mhde-daily-analysis.timer: Fri 2026-05-15 23:15`), but
  for non-trading-day calendars the behavior depends on the daily_radar
  script. The next concretely-affecting run is Mon 23:15 ingesting
  Mon's prices.

The same conditions (Polygon current-day 403 + Yahoo fallback bumping
`MAX`) will recur on every weekday morning until either the freshness
selector changes or the fallback ingestion is suppressed for the
Polygon-blocked date.

**Yesterday (2026-05-14) was the last day a prediction was emitted**
(prediction_date=2026-05-12), because at the time it ran (00:20 UTC
2026-05-14), `MAX(prices_daily.trade_date)` was 2026-05-13 with full
coverage (Polygon had unlocked it overnight), and the coverage check
passed. Today's run had the same structural condition but with one less
day of Polygon catchup.

**The KI-149 fix's promise** ("the first morning that exposes failures
honestly instead of masking them") is partially met: the *masking* is
gone. But the strict-MAX semantics means the engine fails *entirely*
rather than degrading gracefully to T-2. The "T-2 honest" architectural
direction requires the second piece — predict-on-latest-clean-date —
which has not been built yet.

---

## 5. Step 6 — what "T-2 alignment retrain" actually means

The `docs/EQUITY_WORKSTREAM_PAUSED.md` framing of Step 6 is:

> Current models were trained on T-0 historical features (labels formed
> from prices following the T-0 feature snapshot). Applying a
> T-0-trained model to T-2 inputs creates a subtle distributional shift
> because the input distribution at T-2 differs slightly from T-0
> (missing two days of decay). Retrain on T-2 alignment to remove the
> shift.

**This framing is technically incorrect.** Reading the training and
feature code confirms:

### 5a. Labels (`ml/labels.py:27-91`)

Labels are computed by joining each (ticker, trade_date D) to its
forward-window adjusted closes at D+5, D+10, D+20 trading days. The
binary labels are functions of (close at D, max/min close over D+1
… D+H). They have no reference to "today's calendar date." They are
purely indexed by D.

### 5b. Features (`ml/features.py`)

Grep for `as_of\|today\|datetime.now\|current_date\|live_date\|inference`
in `ml/features.py` returns no hits other than the `as_of_date` column
on `macro_series` — which is just the historical observation date of
the macro print, not a "time of inference" reference. All features are
computed as deterministic functions of historical price/macro/fundamental
data with timestamps ≤ D.

### 5c. Train (`ml/train.py:67-82`)

Training joins `ml_features` to `ml_labels` on `(ticker, trade_date)`
and trains on `(X_at_D, y_for_D's_forward_window)`. The model never
sees a "today" feature; it has no notion of the calendar gap between
when D was selected as a prediction date and when the model is being
called.

### 5d. Implication for T-2 inference

Features computed at D=2026-05-13 today are identical, row for row,
to features computed at D=2026-05-13 yesterday or at D=2026-05-13 in
the training set (provided the underlying historical prices/macros
have not been revised — they haven't been). The feature
*distribution* is time-of-prediction-invariant.

A model trained on `(X_D, y_for_D)` pairs and applied to fresh `X_D`
features produces a calibrated probability for `y_for_D` — *regardless
of when* the inference is called. There is no "T-0 vs T-2 distributional
shift." The argument in `EQUITY_WORKSTREAM_PAUSED.md` ("missing two
days of decay") does not correspond to any actual computation in the
feature or label pipeline.

### 5e. What IS different under T-2 cadence

What changes under T-2 cadence is the *operator-facing semantic*, not
the model:

- A prediction issued at calendar time T about prediction_date=T-2
  forecasts forward returns from T-2's close. The window endpoint is
  T-2+H trading days; the **horizon-to-now** as the operator reads the
  prediction is H-2 trading days (3 trading days for a "5d" call,
  8 for a "10d" call, 18 for a "20d" call).
- The operator/dashboard must explicitly advertise this so live-trading
  decisions are not made on the assumption that "5d" means 5 calendar
  days from today.

**Step 5 (dashboard T-2 labelling — commit `6de1187`) closes this
operator-facing gap.** That commit adds the page-level caption,
per-date banner naming the T-0 / T-1 / **expected T-2** / stale cadence
branch, and the date in the predictions subheader. Examining the
commit confirms it covers the three surfaces where the gap mattered.

### 5f. Conclusion for Step 6

**Step 6 (T-2 alignment retrain) as currently described is unnecessary
and should be retired from the resumption queue.** No retrain
methodology change is needed; no new training-time alignment is needed.
The Sun 21:30 UTC `mhde-retrain.timer` will run its weekly walk-forward
retrain on its normal cadence and pick up cross-asset features
(DGS2/VIXCLS/sector ETFs) that became available 2026-05-14 — that's a
regular weekly retrain, not a "T-2 alignment" change.

What remains is the operational fix described next.

---

## 6. Recommended next dispatch

The diagnosis decomposes the work into two clearly separate
deliverables. Only one is needed to restore daily predictions:

### Primary — fix freshness selector to scan backward for latest fully-covered date

**File:** `pipelines/freshness.py:70-124` (`check_equity_freshness`).

**Change shape (high-level only, no code yet):** rather than reading
`MAX(trade_date)` and checking coverage on that single row, the function
should select the most recent trade_date whose row count is ≥
`_EQUITY_COVERAGE_RATIO × mean_prior_30`. The remainder of the trading-day
gap check then uses that "latest fully-covered" date as `latest`.

Downstream wiring:

- `ml_prediction_pipeline.py:43` already passes `freshness` through; the
  pipeline doesn't read `latest` explicitly today.
- `ml/predict.py:119-124` auto-picks `prediction_date` from
  `MAX(ml_features.trade_date)`. Since `ml_features` is computed for
  whatever dates `prices_daily` covered (and partial fallback for
  2026-05-14 produced 5 feature rows there), this would also need to
  select the latest fully-covered features row, or accept the
  freshness selector's chosen date as input.

This is a small, well-scoped change. It's the missing piece of
KI-149's "T-2 honest" architectural direction — the fix that allows
graceful T-2 degradation rather than total skip.

**Effect when shipped:**
- Today's run would have selected `prediction_date=2026-05-13` (415
  feature rows present), produced predictions, written them to
  `ml_predictions`, and the dashboard banner would have said
  "expected T-2".
- Tomorrow (Sat) would produce no new prediction because daily-analysis
  doesn't run on weekends; Monday morning would emit a fresh prediction
  for `prediction_date=2026-05-15` (Fri's data, T-2 from Monday).

**Could also be paired with** an alarm in `monitor pipeline-execution`
that fires only if the latest fully-covered date is older than T-3
trading days (i.e., even the graceful degradation has failed) — this
gives a useful alerting signal without burying the operator in
"normal T-2 stale" pages.

### Secondary — suppress fallback ingestion for Polygon-blocked current day (optional, lower priority)

**File:** `ingestion/orchestrator.py` (the ingestor loop).

If `polygon` raised `IngestionError` with `current_day_blocked=True` for
date `D`, downstream universe-symbol ingestors (Yahoo historical's
exact-today match) could skip date `D` to keep `prices_daily.MAX` clean
at the latest fully-Polygon-covered date.

This is a defense-in-depth alternative to the freshness selector change.
Doing only this (without the freshness change) restores normal behavior
under MAX-only semantics, but is less robust to future fallback
expansion. Probably skip in favor of the primary fix.

### What NOT to do

- **Don't retrain the model "for T-2 alignment."** No model change is
  needed; the framing in `EQUITY_WORKSTREAM_PAUSED.md` step 6 is based
  on a misunderstanding of the training pipeline (see §5 above).
- **Don't pay for paid-tier Polygon.** The architectural decision in the
  pause doc is committed: free-tier + T-2 honest. The freshness
  selector fix delivers it.
- **Don't manually delete the 68 partial 2026-05-14 rows.** That would
  unblock today's pipeline once but the same condition recurs Monday
  evening. Fix the selector, not the symptom.

### Out of scope — separately worth tracking

- `EQUITY_WORKSTREAM_PAUSED.md` step 6 should be **rewritten** (or
  retired) to reflect that the "alignment" concern is dashboard-facing
  semantics (already shipped in Step 5), not training-time alignment.
  Could be done as part of the freshness-selector branch's pause-doc
  updates.
- Update `KNOWN_ISSUES.md` to file a new KI for the
  freshness-selector gap — or extend KI-149 with a "follow-up" note,
  since KI-149's "Resolution paths" §1 acknowledged the
  `MAX`-vs-coverage trade-off but didn't explicitly call out the
  scan-backward variant.

---

## 7. Summary table for the operator

| Question | Answer |
|---|---|
| Did the pipeline fire today? | Yes, 00:15 UTC on schedule, exit 0. |
| Did it crash or error? | No. Skipped by design (KI-149 freshness check). |
| Why did it skip? | `prices_daily.MAX(trade_date)=2026-05-14` has only 68 rows; KI-149 requires ≥335 (50% of 30-day mean). |
| Is 2026-05-13 fully covered? | Yes: 536 prices_daily rows + 415 ml_features rows. Ready to score. |
| Why was 2026-05-14 partial? | Polygon free-tier 403'd current-day; Yahoo + ReferenceTickers fallback inserted 68 rows; not enough for coverage. |
| Is this a one-day blip? | No. Polygon free-tier 403 on current day is documented permanent behavior; fallback insertion behavior is structural. Same condition will recur every weekday. |
| Is the T-2 retrain (Step 6) needed? | **No.** The model is time-invariant; T-0 vs T-2 inference produces the same calibrated probabilities. The framing in `EQUITY_WORKSTREAM_PAUSED.md` is incorrect. |
| Did Step 5 (dashboard T-2 labelling) resolve the semantic concern? | Yes — that was the right place for it. |
| What's the next dispatch? | Change `pipelines/freshness.py:check_equity_freshness` to scan backward for latest fully-covered `trade_date`. Small, well-scoped, completes KI-149's "T-2 honest" direction. No retrain. |
| Is anything broken in operations beyond this? | No. The KI-149 + KI-150 work last session left monitoring honest; cross-asset ingestion is healthy; FRED is on its usual 1-day lag. |

---

## 8. References

- `pipelines/freshness.py:70-161` — current MAX-only coverage check.
- `pipelines/ml_prediction_pipeline.py:39-47` — pipeline skip path.
- `ml/predict.py:30-47, 103-145` — `StaleFeaturesError` second line of defense.
- `ml/labels.py:27-91` — label is pure function of `(ticker, D, forward window)`.
- `ml/features.py` — no "today" reference; features purely historical.
- `ml/train.py:28-82` — walk-forward; trains on `(features_at_D, label_at_D)` pairs.
- `data/logs/equity_predict.log` — today's `WARNING DATA STALE` line.
- `data/logs/daily_analysis_2026-05-14.log` — Polygon 403 + per-ingestor fallback contributions.
- `docs/EQUITY_WORKSTREAM_PAUSED.md` §5 (Step 5) — dashboard T-2 labelling (done, commit `6de1187`).
- `docs/EQUITY_WORKSTREAM_PAUSED.md` §6 (Step 6) — the section this report recommends retiring/rewriting.
- `KNOWN_ISSUES.md` KI-149 — the silent-T-2-skip closure that this finding builds on.
- `.claude/local_scripts/finding5_freshness_audit.py` — the read-only DuckDB audit script that produced §3.
