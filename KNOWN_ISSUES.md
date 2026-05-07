# Known Issues

Active bug tracker. One row per issue. Newest at the top.

When fixing an issue, don't delete the row — flip `Status: open` →
`Status: resolved (commit <sha>)` and leave a one-line note. The whole
point is to remember what bit us so it doesn't bite again.

---

## Open

### KI-003 — Promotion of trained models to `is_active=TRUE` is manual

**Status:** open. No tracked incidents.
**Symptom.** When `ml/crypto/fx train` writes a new `*_model_runs`
row, the row is *not* automatically activated. Predict continues to use
the previous active model until someone runs an UPDATE.
**Why this is a bug.** A weekly retrain produces a fresh model that
the scheduler never picks up. The promotion gates listed in the plan's
Session 6 monitoring stack are intended to handle this.
**Fix path.** Either (a) auto-promote inside `*_train_cmd` after
walk-forward validation passes thresholds, or (b) explicit `*_promote
<model_id>` CLI step in the retrain ExecStart chain. Decide in a
future session.

---

## Resolved (with regression-test pointers)

### KI-001 — `/review/` returned 502 instead of 404

**Resolved:** 2026-05-07 (pre-Session-2 follow-up).
**Symptom.** `https://mhde.duckdns.org/review/` returned HTTP 502.
**Root cause.** Session 0 disabled `mhde-review-server.service` and
removed the `location /review/` block + `upstream mhde_review` from
nginx. Requests to `/review/` then matched the catch-all `location /`
which proxies to the Streamlit unix socket; Streamlit / its relay
returned 502 for the unknown path.
**Fix.** Added `location /review/ { return 404; }` to
`/home/jpcg/homeboard/nginx/nginx.conf` inside the `mhde.duckdns.org`
server block. **nginx `-s reload` alone was insufficient**: the host
file is a single-file bind mount into the container, and atomic-rename
editors (which the Claude Code Edit tool uses) change the host
file's inode. Docker bind mounts on a single file point at the original
inode, so nginx kept reading the old config. Full container restart
(`docker compose restart nginx`) forced re-read of the new inode.
**Lesson for future host-file edits feeding bind-mounted single
files.** Either (a) `docker compose restart nginx` after every edit,
or (b) edit in-place via `cat > … << EOF` / `sed -i` (preserves inode).
The reload-only path silently serves stale config.
**Regression test (Session 5):** `curl https://mhde.duckdns.org/review/`
returns 404, not 502.

### KI-005 — `fx/ml/labels.py` IndexError on empty `fx_prices_hourly`

**Resolved:** 2026-05-07 (Session 3, found by `tests/fx/test_labels.py::test_compute_labels_empty_db`).
**Symptom.** `compute_labels(conn)` raised `IndexError: index -48 is out
of bounds for axis 0 with size 0` when the input table was empty. The
second loop did `range(n - 48, n - 24)` which produces a negative-bounded
range (e.g. `range(-48, -24)`) that iterates 24 times.
**Fix.** `for i in range(max(0, n - 48), max(0, n - 24)):` — clamps both
bounds to non-negative.
**Why it mattered.** The freshness guard skips empty-DB cases at the
pipeline level, but the underlying function still shouldn't crash on
its own — and the unit-test fixture exposed it immediately.
**Regression test:** `tests/fx/test_labels.py::test_compute_labels_empty_db`.

### KI-006 — `ml/features.py` ParserException when ML universe is empty

**Resolved:** 2026-05-07 (Session 3, found by `tests/equity/test_ml_features.py::test_compute_features_empty_universe`).
**Symptom.** `compute_features(conn)` raised
`Parser Error: syntax error at or near ")"` when the equity ML
universe (companies with `market_cap >= 10B`, sector set, not ETF,
active) was empty — `_load_fundamentals` built a `WHERE ticker IN ()`
which is invalid SQL.
**Fix.** Early return `0` from `compute_features` when the universe is
empty, before any downstream queries.
**Regression test:** `tests/equity/test_ml_features.py::test_compute_features_empty_universe`.

### KI-007 — `ml/evaluate.py` ValueError on zero-fold walk-forward results

**Resolved:** 2026-05-07 (Session 3, found by `tests/equity/test_ml_evaluate.py::test_print_no_folds`).
**Symptom.** `print_walk_forward_results(results=[], ...)` raised
`ValueError: min() iterable argument is empty` because the success-criteria
block computed `min(lifts)` / `max(aucs)` over the empty list.
**Fix.** Guarded the success-criteria block with `if fold_results:` so
the report falls through cleanly when no folds completed.
**Regression test:** `tests/equity/test_ml_evaluate.py::test_print_no_folds`.

### KI-002 — Plan-vs-codebase drift in `HARDENING_PLAN.md`

**Resolved:** `f59baf9` 2026-05-07.
**Symptom.** `HARDENING_PLAN.md` Session 0 description listed several
directories (`features/`, `scoring/scorecard.py`, parts of `missed/`,
`daily_radar`) as legacy, when reachability analysis showed they're
all still wired into `mhde-daily-analysis.service`.
**Fix.** `f59baf9` rewrote the Session 0 section to match
codebase reality. See `DECISIONS.md` ADR-005.

### KI-004 — `models/saved/` was not gitignored

**Resolved:** 2026-05-07 (pre-Session-2 follow-up).
**Symptom.** Trained joblib artifacts under `models/saved/{,crypto/,fx/}`
were tracked in git. 3 equity .joblib files were committed; new
crypto/fx .joblib files were caught and de-staged in Session 0 but
would have reappeared on the next staging cycle.
**Fix.** Added the following patterns to `.gitignore` under a new
"Trained model artifacts" section:
```
models/saved/**/*.joblib
models/saved/**/*.pkl
models/saved/**/*.bin
models/saved/**/*.model
```
Removed the 3 tracked equity joblibs from the index with
`git rm --cached` (files preserved on disk). Verified with `git
ls-files models/saved/` — empty, all 9 model binaries on disk are
now ignored.
**Decision (no separate ADR needed).** Models will be rebuilt by the
weekly retrain timers. No external storage (S3 / git LFS) for now —
loss of artifacts at most costs one retrain cycle.
**Regression test (Session 5):** assert `git ls-files models/saved/`
is empty and that a `.joblib` file dropped under `models/saved/` is
ignored by git status.

The plan's Session 5 will turn each of the entries below into a test
that fails without the fix and passes with it.

The plan's Session 5 will turn each of these into a test that fails
without the fix and passes with it.

### KI-101 — Equity timer schedule was 21:00 (not the original 00:15)

**Resolved:** `dd63612` 2026-05-07 (`fix(infra): stagger retrain
timers and add DuckDB lock-retry`).
**Symptom.** Equity ML and crypto ML retrain timers used to fire at
identical times, causing DuckDB write contention.
**Fix.** Staggered the retrain timers (equity Sun 21:30, crypto Sun
23:00, FX Sat 22:00) plus added lock-retry in `storage/db.py:_connect_with_lock_retry`.
**Regression test (Session 5):** parse every `.timer` in `systemd/`
and assert no two `OnCalendar` lines fire within 30 minutes of each
other.

### KI-102 — Equity service did not include feature step

**Resolved:** earlier (pre-Session 0).
**Symptom.** `mhde-predict.service` only ran `ml predict`, but
`predict` requires features for the prediction date to exist.
**Fix.** Service now chains `ml backfill-features` → `ml predict` in
two `ExecStart=` lines.
**Regression test:** parse `mhde-predict.service` and assert both
ExecStart commands are present in order.

### KI-103 — Crypto outcome window did not match label window

**Resolved:** `886452b` 2026-05-07 (`fix(ml): align fill_outcomes()
windows with labels`).
**Symptom.** `crypto/ml/predict.py:fill_outcomes` walked a fixed
calendar window that didn't match the label horizon (1d/3d/5d/10d).
Historical accuracy reported in the predict log was incorrect.
**Fix.** `fill_outcomes` now takes the horizon string from the
prediction row and uses the corresponding label window.
**Regression test:** stub `crypto_prices_daily` with a known forward
move, write a prediction with horizon=10d, assert
`actual_max_return` equals the precomputed truth for the 10d window.

### KI-104 — Equity outcome window used calendar days (not trading days)

**Resolved:** `886452b` 2026-05-07.
**Symptom.** Equity `fill_outcomes` walked calendar days in the
forward window, including weekends. Forward returns over a 5-day
window included 2 weekends ≈ 7 calendar days.
**Fix.** Equity now walks `prices_daily` rows ordered by trade_date
and takes the next N rows (trading days), matching how labels are
generated.
**Regression test:** sandbox prices_daily with known dates including
a weekend gap, predict at Friday, assert outcome reads Mon-Fri+1 (5
trading days) not Sat-Wed (5 calendar days).

### KI-105 — Dashboard had a module-level cached connection

**Resolved:** earlier.
**Symptom.** Streamlit pages used a module-level `duckdb.connect()`
that survived across reruns. After a writer rotated the file, the
read-only connection still pointed at the old file → stale reads.
**Fix.** Connection is now created inside each page's render function
(per-request).
**Regression test:** mock `duckdb.connect` and assert it's called
once per dashboard query, not once per process.

### KI-106 — User-level systemd units had `User=`/`Group=` lines (silent failure)

**Resolved:** 2026-05-06 (pre-Session 0; documented in
INFRASTRUCTURE.md gotchas).
**Symptom.** `mhde-daily-analysis.service` and
`mhde-daily-catalyst-queue.service` had `User=jpcg` and `Group=jpcg`
declarations. Because they're user-level units they already run as the
user; declaring `User=`/`Group=` triggers exit code 216/GROUP "Failed
to determine supplementary groups: Operation not permitted" on every
firing — silently, with no useful entry in the journal.
**Fix.** Strip those lines from any `~/.config/systemd/user/*.service`.
**Regression test:** scan every file under `~/.config/systemd/user/`
and assert it does not contain `User=` or `Group=`.

### KI-107 — FX predict used to score stale data

**Resolved:** by `7b46c50` 2026-05-07 (`checkpoint: pipeline freshness
guards, FX position-aware alerts, service chaining`).
**Symptom.** FX predict happily scored against old `fx_prices_hourly`
rows when Dukascopy was lagging, producing predictions over data 12+
hours old without warning.
**Fix.** `pipelines/fx_prediction_pipeline.py` calls
`check_fx_freshness` at the top and logs `DATA STALE` if the latest
bar is more than 2 hours old. Predict still runs (intentional — partial
data is still useful) but the warning surfaces in logs.
**Regression test:** stub `fx_prices_hourly` with latest=now-3h, run
the pipeline, assert log line contains "DATA STALE".

### KI-108 — Crypto predict assumed prices were already ingested

**Resolved:** earlier (service chaining).
**Symptom.** `mhde-crypto-predict.service` originally had a single
`ExecStart=… crypto predict`. If Binance ingestion failed or hadn't
run, predict would write predictions over yesterday's data.
**Fix.** Service now chains `backfill-prices → backfill-funding →
backfill-oi → backfill-labels → backfill-features → predict`.
**Regression test:** parse `mhde-crypto-predict.service` and assert
all six commands are present in order.

### KI-109 — Health-check timer not deployed

**Resolved:** earlier (deployment fix).
**Symptom.** `health/checks.py` existed but no systemd timer was
running it. Failures in any pipeline went undetected until JP looked
at the dashboard.
**Fix.** `mhde-health-check.service` + `.timer` deployed under
`~/.config/systemd/user/`.
**Regression test:** assert the unit + timer exist in the deployed
location and are enabled.

### KI-110 — FX bot sent alerts even when already in position

**Resolved:** `7b46c50` 2026-05-07 (FX position-aware alerts).
**Symptom.** Bot sent BUY_GBP alerts even when JP was already long
GBP/EUR. Same the other way.
**Fix.** `fx/bot/telegram_bot.py:send_signal_alert` reads
`fx_position` and suppresses signals matching the current position
direction. `fx/ml/signals.py` now routes Telegram through the bot
helper instead of sending directly.
**Regression test:** seed `fx_position` with `position='long'`,
generate a `BUY_GBP` signal, assert `telegram_sent=FALSE` after the
pipeline runs.

### KI-111 — DuckDB lock errors crashed hourly services

**Resolved:** `dd63612` 2026-05-07.
**Symptom.** When a long-running writer (daily-analysis) held the
lock, the next FX hourly firing crashed immediately with `Could not
set lock`. systemd marked the unit as failed.
**Fix.** `storage/db.py:_connect_with_lock_retry` retries with 30s →
60s → 120s back-off when the lock error is detected.
**Regression test:** mock `duckdb.connect` to raise
`IOException("Could not set lock")` twice then succeed; assert the
helper retries and returns the third connection.

### KI-112 — Repo systemd units drifted from deployed copies

**Resolved:** ongoing diligence; partially detected by Session 0
inventory.
**Symptom.** Files in `MHDE/systemd/` and the deployed copies under
`/etc/systemd/system/` (system) and `~/.config/systemd/user/` could
diverge silently.
**Fix path.** Each deploy procedure (see `OPERATIONS.md`) copies the
repo file. A monitoring check in Session 6 will diff the two
locations and alert on drift.
**Regression test (Session 6 monitoring):** for every unit in `systemd/`,
diff against `/etc/systemd/system/$unit` (or user equivalent) and
alert on any difference.

### KI-113 — Dashboard outcome rendering inconsistent across engines

**Resolved:** `47d9766` ("correct candidate price anchoring") +
`1130a1c` ("hide post-event signal dates"). Multiple commits.
**Symptom.** ML / crypto / FX tabs each rendered outcomes slightly
differently (anchoring date, sort order, signal-vs-event date
display).
**Fix.** Consolidated under shared `dashboard/services/queries.py`
and components.
**Regression test:** snapshot test against a known DB state for each
of the 3 engines.

### KI-114 — Sector-ETF ingest INSERT missed `id` field

**Resolved:** `785808e`.
**Symptom.** `data ingest-sector-etfs` failed because the INSERT into
`prices_daily` didn't supply the `id` PK.
**Fix.** Added missing `id` field, plus exposed the data CLI command.

### KI-115 — `priority_refresh_queue` shadowed `os` module

**Resolved:** `77ebddd`.
**Symptom.** `data priority-refresh-queue` raised `UnboundLocalError`
because a local variable named `os` shadowed the import.
**Fix.** `import os as _os` at the top of the function body.

### KI-116 — Outcomes module re-exported a function from a now-legacy file

**Resolved:** `a3ba5d1` 2026-05-07 (Session 0).
**Symptom.** After moving `outcomes/labels.py` to `legacy/`, the
import-resolution smoke caught `outcomes/__init__.py` re-exporting
`compute_forward_returns` from the moved file.
**Fix.** Removed the re-export line. No active caller existed.
**Regression test:** assert no `__init__.py` under the active tree
re-exports a name from a `legacy/` module path.

### KI-117 — `models/saved/` was nearly swept into `legacy/`

**Resolved:** during Session 0 (caught and undone in same session,
`a3ba5d1`).
**Symptom.** `git mv models/ legacy/models/` swept the trained-artifact
subdirectory `models/saved/` along with the legacy code.
`ml/train.py:26`, `crypto/config.py:26`, `fx/config.py:31`, and
`health/ml_checks.py:17` all hardcode the `"models/saved"` string;
moving the directory would have broken all four.
**Fix.** `git mv legacy/models/saved models/saved` before any
pipeline missed the artifacts.
**Regression test (Session 5):** assert the path `models/saved/`
exists at the active location and matches the reads in
`ml/train.py:26`, `crypto/config.py:26`, `fx/config.py:31`,
`health/ml_checks.py:17`.

---

## Issue ID convention

- `KI-0XX` — open issues
- `KI-1XX` — resolved issues kept for historical context
- New issues get the next available `KI-0XX`. When closing, do NOT
  renumber — leave the open ID and add a `Resolved:` line.
