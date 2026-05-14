# Finding 3 — Root Cause: ML Pipeline Gap 2026-05-13 / 2026-05-14

_Investigation date: 2026-05-14. Read-only. systemd + log inspection._

Companion to the 2026-05-14 equity prediction quality audit. The
observed gap: no `ml_predictions` for `prediction_date=2026-05-13`, and
the May 14 predict run scored `2026-05-12` (one trading day behind the
ostensibly latest price date).

## TL;DR

**Polygon free-tier returns HTTP 403 for the current trading day's
grouped endpoint** (the API only serves data with a ≥2-day delay on
the free tier). On 2026-05-13 only **4 rows** of fallback-source data
landed in `prices_daily` (vs ~520 normal). `ml_features` for 2026-05-13
has **0 rows** because none of those 4 OTC tickers are in the ML
universe. The predict pipeline then **silently scored `2026-05-12`**
(T-2 from "now") because its scoring-date selector uses
`SELECT MAX(trade_date) FROM ml_features` with no completeness check,
and the freshness check uses `SELECT MAX(trade_date) FROM prices_daily`
which is satisfied by the 4 fallback rows.

This is not a transient failure — it has been recurring on every
weekday run since at least 2026-05-12.

## 1. Timer / service state

There is no `mhde-ml-predict.service`. The actual unit is
**`mhde-predict.service`** at `/etc/systemd/system/mhde-predict.service`
— its `ExecStart` chain is:

```
/home/jpcg/MHDE/venv/bin/python main.py ml backfill-features
/home/jpcg/MHDE/venv/bin/python main.py ml predict
```

Logs go to `data/logs/equity_predict.log`, not the journal.

| Unit | State | Last run | Verdict |
|---|---|---|---|
| `mhde-daily-analysis.timer` (user) | active, waiting | Wed 2026-05-13 23:15:21 UTC (~17h ago) | OK |
| `mhde-daily-analysis.service` (user) | inactive (dead), exit 0 | Wed 2026-05-13 23:15:21 → 2026-05-14 00:10 | "Succeeded" but Polygon failed inside |
| `mhde-predict.timer` | active, waiting | Thu 2026-05-14 00:15:21 UTC (~16h ago) | OK |
| `mhde-predict.service` | inactive (dead), exit 0 | Thu 2026-05-14 00:15:21 → 00:20:35 | "Succeeded" but scored stale date |
| `mhde-equity-pipeline-monitor.service` | **failed** (exit 1) at 01:00:09 UTC | Thu 2026-05-14 01:00:09 | **Monitor itself is broken — gap was never alerted** |
| `mhde-monitor-data-quality.service` | exit 0 but log shows DuckDB lock errors and `alert: could not open MHDE DB — bypassing throttle` | Thu 2026-05-14 02:00 | Effectively no-op'd |
| `mhde-monitor-pipeline.service` | exit 0 hourly, but same DuckDB-lock alert bypass loop in `monitor_pipeline.log` | Thu 2026-05-14 16:40 | Effectively no-op'd |

**Timer sequence per weekday (UTC):**

1. `mhde-daily-analysis.timer` → 23:15 Mon–Fri — runs `daily-radar`
   (includes ingestion)
2. `mhde-predict.timer` → 00:15 daily — runs `ml backfill-features`
   then `ml predict`
3. `mhde-crypto-predict.timer` → 00:30
4. `mhde-crypto-export-predictions.timer` → 00:40
5. `trading-engine-entry.timer` → 00:45 — **consumes the ML
   predictions**
6. `mhde-equity-pipeline-monitor.timer` → 01:00 (currently FAILED —
   produces no Telegram alert)

No race condition between ingest and predict (45 min margin). The
DuckDB lock contention seen in `equity_predict.log` (PID 2304618 /
2304619) is the in-process predict service competing with its own
`backfill-features` child — annoying but recovers via retry.

## 2. Smoking-gun evidence

### prices_daily coverage (the gap)

```
2026-05-13   rows=4         ← 4 tickers only (HTHIY, IFNNY, RSHGY, SUNB — fallback sources)
2026-05-12   rows=513       (normal)
2026-05-11   rows=518       (normal)
2026-05-08   rows=516       (normal)
2026-05-07   rows=518       (normal)
2026-05-06   rows=519
2026-05-05   rows=521
```

### ml_features coverage

```
2026-05-12   rows=311       ← latest full-coverage date
2026-05-11   rows=311
2026-05-08   rows=311
2026-05-07   rows=312
2026-05-06   rows=312
2026-05-05   rows=312
```

No row for 2026-05-13 — feature-builder produces nothing because none
of the 4 fallback tickers are in the ML universe (~311 tickers).

### ml_predictions coverage

```
2026-05-12   n=32   tickers=20   horizons=3   ← latest
2026-05-11   n=35   tickers=21   horizons=3
2026-05-08   n=63   tickers=29   horizons=3
2026-05-07   n=58   tickers=33   horizons=3
```

No predictions for 2026-05-13. The May 14 predict run scored
`2026-05-12` (one day stale relative to today). This was consumed by
`trading-engine-entry.timer` at 00:45.

### Predict log (`data/logs/equity_predict.log`)

```
2026-05-13 00:20:10  mhde.ml.pipeline   Freshness OK: Equity prices_daily latest=2026-05-12 (1 trading-day gap; threshold=2)
2026-05-13 00:20:10  mhde.ml.predict    Scoring universe for 2026-05-11    ← scored T-2

2026-05-14 00:20:35  mhde.ml.pipeline   Freshness OK: Equity prices_daily latest=2026-05-13 (1 trading-day gap; threshold=2)
2026-05-14 00:20:35  mhde.ml.predict    Scoring universe for 2026-05-12    ← scored T-2
```

The May 14 freshness line is **the silent skip**: it reports
`latest=2026-05-13` based on the 4 fallback rows, declares freshness
OK, then scores `2026-05-12` because that is the most recent date with
`ml_features`.

### Daily-analysis ingestion log (`data/logs/daily_analysis_2026-05-13.log`)

```
2026-05-13 23:56:26  mhde.ingestion.polygon   WARNING  Polygon grouped 2026-05-13: HTTP 403
2026-05-14 00:06:29  mhde.ingestion.polygon   Prices: 2048 inserted across 7 dates (2048 attempted, 1 failed)
2026-05-14 00:06:29  mhde.ingestion.polygon   2026-05-13: {'grouped_status': 403, 'in_universe': 0, 'fallback_inserted': 0}
2026-05-14 00:06:29  mhde.ingestion.polygon   2026-05-12: {'grouped_status': 200, 'in_universe': 504}
2026-05-14 00:06:29  mhde.ingestion.polygon   2026-05-11: {'grouped_status': 200, 'in_universe': 504}
2026-05-14 00:06:29  mhde.ingestion.polygon   2026-05-08: {'grouped_status': 200, 'in_universe': 504}
...
2026-05-14 00:09:32  mhde.ingestion.orchestrator   Ingestion complete: 8 succeeded, 1 failed, 2 skipped
```

The same pattern is visible in `daily_analysis_2026-05-12.log`
(Polygon 403 on 2026-05-12 ingested 23:17, then 200 on 2026-05-12
ingested at 23:27 from a different code path) — but the **current-day**
fetch is always 403. Even at T+1 the May 13 data is still 403; it only
unlocks at T+2 on Polygon's free tier.

## 3. Root cause

**Three layered defects** turn a known external-API limitation into a
silent stale-prediction:

### a) Polygon free-tier blocks current-day grouped endpoint (external)

`adapters/polygon.py` notes recent_daily_prices is "Last 5 days OHLCV
available on free tier" — but the `grouped` endpoint for date == today
(and often T-1) returns HTTP 403. Treated as `WARNING` and counted as
`fallback_attempted=0` for that date.

### b) Freshness check is row-count-blind — `pipelines/freshness.py:67`

```python
row = conn.execute("SELECT MAX(trade_date) FROM prices_daily").fetchone()
latest = row[0] if row else None
...
trading_gap = trading_days_between(latest + timedelta(days=1), today)
is_fresh = trading_gap <= max_trading_days     # threshold=2
```

With **4 rows** of fallback OTC data for 2026-05-13,
`MAX(trade_date) = 2026-05-13`, so `trading_gap = 1 ≤ 2`. Freshness
check **passes despite ~99% missing universe coverage for that date**.

### c) Scoring-date selector picks max ml_features with no cross-check — `ml/predict.py:93-95`

```python
if prediction_date is None:
    row = conn.execute("SELECT MAX(trade_date) FROM ml_features").fetchone()
    prediction_date = row[0]
```

Because `ml_features` for 2026-05-13 has 0 rows, this returns
`2026-05-12`. The pipeline does **not** compare against
`MAX(trade_date)` in `prices_daily` and does **not** raise/warn when
there's a divergence. The `INFO Scoring universe for 2026-05-12` log
line is the only signal, buried in a log file that nothing else reads.

### d) The monitor that would have caught this is broken

- `mhde-equity-pipeline-monitor.service` exits 1 every day since at
  least 2026-05-14 01:00 (no journal entries visible without sudo, but
  service is in `failed` state).
- `mhde-monitor-data-quality.service` and
  `mhde-monitor-pipeline.service` both exit 0 but their logs show
  `alert: could not open MHDE DB — bypassing throttle` in a loop —
  they can't read DuckDB because of write-lock contention, so they
  emit no alert.

## 4. Why this is a "silent skip-on-no-input"

`ml/predict.py` has no explicit skip branch, but the effect is the
same:

- `_load_features_for_date()` only fails when `ml_features` has zero
  rows for the chosen `prediction_date` — but the selector chose
  `2026-05-12` which has 311 rows, so it succeeds.
- The "no features" guard at `ml/predict.py:108-110` only fires if you
  *explicitly* pass `prediction_date=2026-05-13`. Default behavior
  silently steps back.
- The predict pipeline then **rewrites** predictions for `2026-05-12`
  (idempotent UPSERT at `ml/predict.py:202`), creating the appearance
  of "another successful run today" — even though the trade date
  hasn't advanced.

## 5. Recommended fix

Smallest change that closes the gap, in order of priority:

1. **Tighten freshness in `pipelines/freshness.py:67`** to require
   minimum universe coverage for the latest date:
   ```python
   row = conn.execute("""
       SELECT trade_date, COUNT(*)
       FROM prices_daily
       GROUP BY trade_date
       HAVING COUNT(*) >= (SELECT 0.5 * COUNT(DISTINCT ticker) FROM ml_features WHERE trade_date >= CURRENT_DATE - INTERVAL 30 DAY)
       ORDER BY trade_date DESC LIMIT 1
   """).fetchone()
   ```
   With this, 4-row "today" coverage no longer satisfies freshness —
   the pipeline will return `{"skipped": "stale_data"}` and the
   operator can be alerted before `trading-engine-entry` consumes a
   stale prediction.

2. **Cross-check predict scoring-date against prices_daily in
   `ml/predict.py:93-95`**: if `MAX(ml_features.trade_date)` <
   `MAX(prices_daily.trade_date)`, log `WARNING` (or refuse to score,
   behind a flag) so the regression is visible in
   `equity_predict.log` and alertable.

3. **Fix `mhde-equity-pipeline-monitor.service`** — it has been
   failing every day and is the unit that would have surfaced this on
   Telegram. Investigate `main.py monitor equity-pipeline` exit-1
   cause.

4. **Polygon adapter**: if `grouped_status=403` *and* `in_universe=0`,
   the orchestrator should treat that as a hard failure for the
   affected date (currently logged as `WARNING` and rolled into a
   generic "1 failed" summary). Optionally, fail over to a paid-tier
   endpoint or accept a documented T-2 prediction cadence — but right
   now the pipeline pretends the data is fresh.

5. **Operator action**: decide whether the equity engine should accept
   Polygon's free-tier ≥2-day lag (i.e., predictions are always T-2)
   or budget for a paid tier to get T-1 / T-0 grouped data. The
   current state advertises "daily" predictions but ships T-2.

## Files / lines referenced

- `/etc/systemd/system/mhde-predict.service` (ExecStart chain:
  backfill-features → predict)
- `/home/jpcg/.config/systemd/user/mhde-daily-analysis.{timer,service}`
  (user unit, Mon–Fri 23:15 UTC)
- `pipelines/freshness.py:67` — `MAX(trade_date)`-only freshness check
- `ml/predict.py:93-95` — silent scoring-date fallback
- `adapters/polygon.py` — handles 403 as warning
- `data/logs/equity_predict.log:631` (May 13 run) and `:731` (May 14
  run) — the smoking-gun "Scoring universe for…" lines
- `data/logs/daily_analysis_2026-05-13.log` — the Polygon 403 on the
  current-day grouped fetch
- Investigation script (left in repo at the time of investigation):
  `.claude/local_scripts/check_ml_pipeline_gap.py` (read-only,
  idempotent)
