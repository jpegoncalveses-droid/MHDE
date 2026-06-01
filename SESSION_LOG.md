# Session Log

Append-only record of what each `HARDENING_PLAN.md` session actually
accomplished, what changed, and what's pending. Most recent entries
are at the top.

---

## 2026-06-01 — Intraday faithful replay of Phase 1B predictions (pipeline + first pass)

**Branch:** `feat-intraday-replay-eval`, **based off `exp/trailstop-sweep`**
(not master) so the inherited `HardFloorOverlay` is reused rather than
duplicated. `exp/trailstop-sweep` was pushed to origin first to preserve
lineage. Worked in a **separate worktree** (`../mhde-intraday`) so the
production checkout at `/home/jpcg/MHDE` stayed on master and the live
predict/export timers were never switched onto a feature branch. MHDE-only;
no engine/INTERFACE/spec/hash changes; no production-DB writes.

**Goal.** Replay each daily walk-forward 10d prediction against **1-minute**
klines under the live engine's exit stack to get a per-probability-bin read
on realized intraday performance (vs the daily-bar Phase 1B backtest).

### Key decisions / assumptions (flagged)

- **Reused, not reimplemented:** `policies.py::TrailingStopOnly`
  (trail=0.30, activation=0.01), `costs.py`, and the base branch's
  `HardFloorOverlay` (−0.05). No second floor was added.
- **Intraday arm-aware floor extension.** `HardFloorOverlay` gained an
  additive `intraday_arm_aware` flag (default `False` → daily floor-first
  behaviour unchanged, existing 8 tests green). When `True`: while the
  inner trail is UNARMED the −5% floor is the stop; once ARMED the
  give-back trail (which sits ≥ entry, above the floor) is checked first
  and wins. `TrailingStopOnly` gained an additive `is_armed` property.
  Within-bar arm+floor ties resolve **adverse-first** (down-move before
  up-move → floor fires). The −5% floor models the engine
  `HARD_FLOOR_EXIT_PCT` (operator-confirmed); it is NOT part of Phase 1B.
- **Entry anchor (KI-141, confirmed).** `crypto_ml_predictions.prediction_date`
  is the features-as-of T-1 day (`MAX(trade_date) FROM crypto_ml_features`),
  and all walk-forward rows have `predicted_at` NULL — there is no run-time
  stamp. The live engine enters at 00:45 UTC on `export_date =
  prediction_date + 1` (`write_daily_predictions.py`, engine INTERFACE.md
  §3.1). The **default `DeployedEntry` therefore anchors at
  prediction_date + 1 day @ 00:45 UTC**, fill = open of that 1-minute bar.
  This `day_offset` is a parameter, so an alternative anchor is a config
  change, not a rebuild.
- **Pluggable entry interface.** `EntryRule` ABC →
  `DeployedEntry` (baseline, default) + `FixedOffsetEntry(hours)` (wired to
  prove pluggability, **not swept** here). Conditional/intraday entry rules
  and fixed-hour sweeps are a config-change follow-on with OOS discipline.
- **Separate research DB.** 1-minute klines live in
  `data/research/intraday.duckdb` (`crypto_klines_intraday`,
  PK `(symbol, interval, open_time)`), created on demand —
  **NOT** in `ALL_SCHEMAS`, never in `mhde.duckdb`. The replay opens both
  DBs **read-only** (two read-only connections; equivalent isolation to the
  spec's ATTACH).

### What was added (all MHDE)

- `crypto/execution/backtest/policies.py` — additive `is_armed` +
  `intraday_arm_aware` (no behavioural change to existing callers).
- `crypto/ingestion/binance_client.py` — additive `fetch_klines` +
  `_parse_intraday_kline` (existing daily/funding/OI methods untouched).
- `crypto/execution/backtest/intraday_klines.py` — research-DB schema +
  idempotent UPSERT + `backfill_intraday` driver (injected client;
  skip+log on per-symbol failure; gap tally).
- `crypto/execution/backtest/intraday_replay.py` — entry rules, 1-minute
  exit walk (`simulate_intraday_trade`), net-return, per-bin aggregation,
  the DB-backed `run_intraday_replay` driver, and `render_report`.
- `main.py` — `crypto backfill-intraday` (live-window guard 22:00-23:30 /
  00:25-00:50 UTC, `--force` to bypass) and `crypto intraday-replay`.
- Tests: 50 new/covered (floor reuse + intraday-mode, trail parity, first
  touch, entry-rule selection, fee accounting, within-bar tie, backfill
  idempotency + pagination + skip, driver end-to-end). Green. The two
  repo-wide failures (`test_systemd_units::...23_00_utc`,
  `test_schema_promotion_status::test_migration_backfills_pre_existing_db`)
  **pre-exist on the base commit** and are unrelated.
- `.gitignore` — `data/research/` + `data/reports/`.

### First pass (scope + window)

- **Window:** last 90 days of the walk-forward OOS set —
  **prediction_date 2026-02-06 → 2026-05-07** (4,362 predictions, 48
  symbols, 91 dates). Bounded validation BEFORE any full-window backfill.
- Pipeline validated end-to-end on real Binance data (BTCUSDT smoke: 18,721
  1-minute bars, 0 gaps; replay produced a trailing exit). Full 90d × 48
  symbol backfill into the research DB + the per-bin report:
  `data/reports/intraday_replay_<date>.md` (gitignored).

**Pending.** PR is DRAFT pending merge-reviewer + explicit JP approval —
do NOT merge. `exp/parabolic-filter-ab` + `stash@{2}` (harness rule-gating
WIP) remain untouched and still overlap `harness.py` for a future
reconcile. Entry-time / condition sweeps are out of scope (interface only).

---

## 2026-05-19 — Phase 1b: phantom `polling_interval_seconds` removed from MHDE spec emission

**Branch:** `chore/spec-remove-phantom-polling-mhde` (local, not pushed).

**Context.** The spec field `runtime.polling_interval_seconds` written
into `data/exports/active_spec.json` was non-binding on the consumer
side: actual polling cadence in crypto-trading-engine is driven by an
in-handler loop in `engine/cli/handlers/monitor.py` (not by the spec).
The field's presence in the spec was misleading future edits, and the
consumer's `_MIN_POLLING_SECONDS = 30` validator floor existed only to
gate this dead field.

**Phase 1a (already shipped on the engine side, prerequisite for this
work).** crypto-trading-engine PR #7 removed the field from the
engine's spec loader, schema, validator, types, and tests. The loader
now tolerates the field being present or absent (transitional). Phase
1a was deployed in production and verified: monitor cycles ran cleanly
with the new code, `load_spec` succeeded against MHDE's then-current
spec (which still carried the field).

**Phase 1b (this commit).** Stop emitting the field from MHDE so the
spec no longer carries it.

### What was changed

- `crypto/exports/spec_config.py` — removed
  `"polling_interval_seconds": 60` from the `RUNTIME` dict.
- `tests/crypto/exports/test_spec_config.py::test_runtime_values` —
  replaced the `>= 30` assertion with an absence check
  (`assert "polling_interval_seconds" not in rt`).
- `data/exports/active_spec.json` — regenerated locally via
  `venv/bin/python main.py crypto export-spec` for verification only.
  The file is gitignored (`.gitignore:121` excludes `data/exports/`)
  so the regeneration is NOT in this commit. The `runtime` block in
  the freshly built file now has 3 fields (entry_time_utc,
  monitoring_window_hours, reconciliation_time_utc). New `spec_hash`
  produced: `sha256:122dc4205647dc9308542f4800bcd81b6311f35081a1dc842c63a97c2326fc0e`
  (was `sha256:0e3a9f4105195b6857276e528dedd9e0f0d23d4be732aa7dca8bb7760f4afa05`).

### Files intentionally NOT changed

- `docs/superpowers/plans/2026-05-10-mhde-engine-export-contract.md`
  and `docs/superpowers/specs/2026-05-10-mhde-engine-export-contract-design.md`
  still reference the field. These are dated planning artifacts from
  the original 2026-05-10 implementation; modifying them retroactively
  would falsify the historical record. The field's removal is recorded
  here in the session log instead.

### Verification

- `venv/bin/python main.py crypto export-spec` — succeeded; wrote
  active_spec.json with new hash, log line confirms write.
- `grep polling_interval data/exports/active_spec.json` — 0 matches.
- `venv/bin/python -m pytest tests/crypto/exports/ -v` — 55 passed,
  1 skipped. The skip is the pre-existing cross-repo hash parity test
  waiting on a fixture from crypto-trading-engine; unrelated to this
  change.

### Cross-repo state after this commit

The two-repo phantom-config cleanup is complete:

- crypto-trading-engine (Phase 1a, PR #7) — field absent from loader /
  schema / validator / types / tests. Loader tolerates presence or
  absence. Deployed.
- MHDE (Phase 1b, this commit) — field absent from `spec_config.RUNTIME`
  and from emitted `active_spec.json`. The engine's transitional
  "tolerate either" loader will now consistently see "absent."

A related engine-side follow-up exists as engine PR #8
(docs/SESSION_LOG resolution) but is independent of this work.

### Pending

- Push this branch and open PR on operator approval.
- **Post-merge deploy step.** Because `data/exports/active_spec.json`
  is gitignored, the production VPS will keep emitting the old spec
  (with the phantom field) until `venv/bin/python main.py crypto
  export-spec` is run there after pulling. Engine PR #7 is already
  tolerant of the field, so there's no ordering hazard, but the
  cross-repo "consistently absent" state isn't reached until the prod
  regen runs.
- After the MHDE PR lands and the new spec is deployed, the engine
  side can drop its transitional "tolerate field" tolerance in a
  future cleanup (out of scope here).

### Out-of-scope flag (not acted on in this PR)

The operator noted that some MHDE docs may carry stale "universe
static, frozen at 50, no dynamic management" wording. A casual scan
during this work surfaced nothing matching that exact claim in the
files I touched (spec_config.py UNIVERSE source label
`binance_usdtm_perp_top_50` is the source-of-truth label string, not
a static-size assertion). The most recent SESSION_LOG entry
(2026-05-18) already pins the current dynamic state at 51 active / 8
inactive / 2 pending with daily rank/build timers firing. Any deeper
audit is deferred.

---

## 2026-05-18 — Universe state pin + rank/build timer-gap widened

**Branch:** `fix/universe-timer-gap` (pushed; do NOT auto-deploy).

**Where universe stands today** (audited read-only against
`/home/jpcg/MHDE/data/mhde.duckdb` + journalctl):

- Hysteresis builder deployed. Both daily timers active and healthy:
  - `mhde-crypto-rank-universe-daily.timer` — currently fires 23:00 UTC
    (this commit moves it to **22:00 UTC**; not yet deployed).
  - `mhde-crypto-build-universe-daily.timer` — fires 23:30 UTC (unchanged).
- Last successful build run 2026-05-17 23:30:01–23:30:33 UTC:
  **9 adds, 8 removes, 3 pendings, 95 no-ops**.
  - ADDs: AAVEUSDT, APEUSDT, HIGHUSDT, KATUSDT, ORDIUSDT, PIEVERSEUSDT,
    SIRENUSDT, WLDUSDT, ZBTUSDT
  - REMOVEs: FARTCOINUSDT, GIGGLEUSDT, MUSDT, NAORISUSDT, PRLUSDT,
    TSTUSDT, WLFIUSDT, ZEREBROUSDT
- **Active universe size: 51** (50 was the pre-rebuild count; net +1 from
  the rebuild). 51 is a *hysteresis overshoot* — the 7-day consecutive
  in/out rule lets the active set drift slightly above the cutoff. Audit
  confirmed downstream (MHDE predict + crypto-trading-engine top-N
  selection) scales with `len(active_universe)`; no code on the live path
  caps at 50. `UNIVERSE_SIZE=50` in `crypto/config.py` is a vestigial
  import used only by off-the-path backtest validation + local audit
  scripts.
- Pending list (blocked by the 60-day listing floor, all with 7d
  consecutive top-50):
  - **BASEDUSDT** — eligible 2026-05-29 (48d listed)
  - **BILLUSDT** — eligible 2026-07-06 (10d listed)
  - **CHIPUSDT** — eligible 2026-06-15 (31d listed)

**What this commit changes.** Moves rank from 23:00 → 22:00 UTC. With
the observed 3 min 34 s rank runtime, the prior 26-min gap before build
left no margin for a runtime spike. The new schedule:

```
rank end  ~22:03:34  →  build start 23:30:00  =  ~86 min headroom
build end ~23:30:32  →  predict     00:30:00  =  ~60 min
```

`systemd-analyze verify` clean on both unit files. Build's timer comment
updated to describe the new gap; OnCalendar value unchanged.

**Supersedes the entry at line 129 ("Crypto universe state pinned (no
fix applied)").** Option A from that entry was shipped 2026-05-17
evening; this entry pins the post-deploy state and the timer-gap fix.
Line 129 stays in the log as the historical waypoint but is no longer
operationally accurate.

**Not deployed.** No `sudo cp`/`daemon-reload`/`restart` performed. To
activate, the operator runs the standard:

```
sudo cp systemd/mhde-crypto-rank-universe-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart mhde-crypto-rank-universe-daily.timer
```

(Build's `.timer` file changed only its comment block — no functional
change there, so the daemon-reload alone picks up the new description.
Skip the build-universe restart unless you also want to refresh its
`Active since` timestamp.)

---

## 2026-05-17 (evening) — Crypto universe: hysteresis-based daily continuous-update pipeline shipped

**Branch:** `feat-universe-hysteresis-continuous` (pushed; **STOPPED for
operator review/merge** — push-only per branch-handoff pattern, operator
opens the PR). Single-repo (MHDE), no engine repo change. Logically
stacked on top of `chore/session-log-universe-state` (commit d91c9ae,
already pushed) which pins the pre-fix state; both branches merge cleanly
to master (chore-branch first to avoid a SESSION_LOG conflict).

**Trigger.** Earlier today's read-only investigation (see the
chore-branch SESSION_LOG entry) pinned: `crypto_universe` was frozen at
50 coins added 2026-05-05; no systemd timer fed `crypto build-universe`;
the hysteresis work that addresses this lived only in `stash@{0}` (never
committed to any branch). Operator chose **Option A** — ship the
hysteresis from the stash AND wire continuous daily-update timers so
each 00:30 UTC predict consumes a freshly-evaluated universe.

**What landed (commit fc51219).**

*Schema (idempotent — orphan tables already in prod DB matched the
shape exactly per `.claude/local_scripts/audit_orphan_universe_tables.py`):*
- `crypto_universe_ranking_buffer (symbol, ranking_date,
  avg_daily_volume_30d, rank_by_volume, in_top_50)`
- `crypto_universe_pending (symbol, days_listed, eligible_after_date,
  consecutive_top_50, last_checked_at)`

*CLI:*
- `crypto rank-universe-daily` — writes top-100 USDT-perp by 30d avg
  quote volume into the buffer for today (UTC). Idempotent on
  `(symbol, ranking_date)`. Does NOT touch `crypto_universe`.
- `crypto backfill-universe-rankings --start-date YYYY-MM-DD` — same
  but per-date in a range, using point-in-time 30d windows.
- `crypto build-universe [--dry-run]` — rewritten as hysteresis-based:
  ADD on 7-consecutive `in_top_50=TRUE` + ≥60d listed; REMOVE on
  7-consecutive `in_top_50=FALSE`; PENDING when ADD-eligible but listed
  <60d. Refreshes `rank_by_volume` + `avg_daily_volume_30d` for active
  symbols from the most recent buffer date.

*Cadence (diverges from the originally-stashed weekly-Sunday design;
operator chose daily so each rotation feeds the next predict):*
- 23:00 UTC daily — `mhde-crypto-rank-universe-daily` (rank, ~3 min)
- 23:30 UTC daily — `mhde-crypto-build-universe-daily` (hysteresis, ~s)
- 00:30 UTC daily — `mhde-crypto-predict` (existing — consumes result)

The 7-consecutive hysteresis rule means daily invocation does NOT churn
membership on non-transition days; rotation only happens when a coin
stabilises in or out of top-50 for a full week.

*Systemd:* `mhde-crypto-rank-universe-daily.{service,timer}` and
`mhde-crypto-build-universe-daily.{service,timer}` installed in
`/etc/systemd/system/`, enabled, started. `systemctl list-timers`
confirms next fires:
- `mhde-crypto-rank-universe-daily.timer  → Sun 2026-05-17 23:00 UTC`
- `mhde-crypto-build-universe-daily.timer → Sun 2026-05-17 23:30 UTC`

**Tests.** 44 new tests across hysteresis branches (ADD-success, ADD-
blocked-by-streak, ADD-blocked-by-listing-floor→PENDING, REMOVE,
REMOVE-blocked-by-transition, insufficient-history, dry-run, rank-
refresh), ranker idempotency, backfill correctness + per-date failure
isolation, systemd unit syntax + invariants. 59 universe-correction
tests pass; broader crypto suite has 439 passing + 1 pre-existing fail
(`test_schema_promotion_status::test_migration_backfills_pre_existing_db`,
documented as **KI-155** in `KNOWN_ISSUES.md`, verified to also fail on
master, unrelated to this work).

**Tonight's first rebuild — dry-run preview (2026-05-17 20:53 UTC).**

```
[DRY-RUN] build-universe: 9 adds, 9 removes, 3 pendings, 97 no-ops
                          (latest buffer date 2026-05-16)
ADDs    : AAVE, APE, HIGH, KAT, ORDI, PIEVERSE, SIREN, WLD, ZBT
REMOVEs : 1000LUNC, FARTCOIN, GIGGLE, M, NAORIS, PRL, TST, WLFI, ZEREBRO
PENDINGs: BASED (eligible 2026-05-29), BILL (2026-07-06),
          CHIP (2026-06-15)
```

Net: universe stays at 50 coins after the rotation. Hysteresis behaved
as designed — meaningful turnover after 12 days frozen, no flicker.

**Active spec unchanged.** `data/exports/active_spec.json` continues to
declare `universe.source = "binance_usdtm_perp_top_50"`; the source
label remains accurate when membership rotates. Phase-1B-winner params
(`horizon_days=10, exit_policy=D, selection_mode=top_n, selection_n=6,
trail_pct=0.3, activation_pct=0.01`) unchanged.

**Stash + safety net.** The originating `stash@{0}` was preserved
unchanged for the entire workstream; the operator-side patch backup at
`/home/jpcg/wip-universe-hysteresis.patch` (752 lines, 30683 bytes)
remains in place. The stash itself is dropped at the end of this
session now that the install is verified and the work is committed.

**Pending.**
- Monitor `journalctl -u mhde-crypto-rank-universe-daily -f` and
  `journalctl -u mhde-crypto-build-universe-daily -f` tonight at 23:00
  / 23:30 UTC for the first live fires.
- Tomorrow 00:30 UTC: predict consumes the rotated universe for the
  first time. Tomorrow 06:30 UTC: trading-engine entries fire against
  the new set.
- Open the PRs for `chore/session-log-universe-state` (already pushed,
  d91c9ae) and `feat-universe-hysteresis-continuous` (this branch);
  merge chore-branch first to avoid a SESSION_LOG conflict.
- Step 8 still owed (per stashed `systemd/README-universe-correction.md`):
  write ADR for the methodology fix; add universe section to
  `docs/PATH_TO_LIVE_PLAN.md`.

**Related files.**
- `crypto/ingestion/rank_universe.py`,
  `crypto/ingestion/universe_builder.py`
- `crypto/schema.py` (additions for ranking_buffer + pending)
- `systemd/README-universe-correction.md`,
  `systemd/mhde-crypto-rank-universe-daily.{service,timer}`,
  `systemd/mhde-crypto-build-universe-daily.{service,timer}`
- `tests/crypto/test_universe_hysteresis.py`,
  `tests/crypto/test_rank_universe_daily.py`,
  `tests/crypto/test_backfill_universe_rankings.py`,
  `tests/crypto/test_systemd_units.py`
- `KNOWN_ISSUES.md` — KI-155 added (pre-existing test fail, unrelated)

---

## 2026-05-17 — Crypto universe state pinned (no fix applied)

Investigation outcome: the crypto universe in `mhde.duckdb` is static at 50 coins
from 2026-05-05. Nothing scheduled or otherwise runs `crypto build-universe`.

Active code state:
- `crypto/ingestion/universe_builder.py` = simple volume-rank version (no hysteresis)
- `main.py` `crypto build-universe` CLI = wired to simple builder
- `crypto/schema.py` `ALL_SCHEMAS` = does NOT include ranking_buffer or pending tables

Live DB has two orphan tables (not in active schema, not written by active code,
not read by any production path):
- `crypto_universe_ranking_buffer` (1400 rows, 2026-05-03 → 2026-05-16)
- `crypto_universe_pending` (0 rows)

These came from running stashed WIP against the live DB during development.

Stashed work: `stash@{0}` "feat-universe-correction WIP before sentiment week 1"
- 7 files, +559/-85 lines
- Adds hysteresis (7-day consecutive rule, 60-day listing floor)
- Adds the two schema tables to `ALL_SCHEMAS`
- Adds `fetch_30d_avg_quote_volume_at` to `binance_client`
- Backed up to `~/wip-universe-hysteresis.patch` on 2026-05-17

Branch `feat-universe-correction` exists (local + origin) but HEAD == master.
No commits on the branch. Easy to overlook.

Systemd timers for universe management: do NOT exist anywhere (not in repo,
not in `/etc/systemd/system/`, not in any commit on any branch). The earlier
recollection of "authored but not installed" was incorrect.

Open KI: KI-155 (referenced in stashed `KNOWN_ISSUES.md` changes).

Decision deferred: Option A (ship hysteresis from stash) vs Option B (minimal:
wire simple builder weekly) vs Option C (manual curation, status quo).
Recommended A, scheduled for a dedicated focused session before any next
Phase 1B re-run.

---

## 2026-05-14 — Cross-asset ingestion restored (Finding 1): SPY/VIX/sector ETFs + DGS2/VIXCLS now in nightly chain

**Branch:** `feat-cross-asset-ingestion` (pushed; **STOPPED for
operator review/merge** — push-only per branch-handoff pattern,
operator opens the PR). Single-repo (MHDE), no engine repo change,
no schema change.

**Trigger.** `data/processed/finding1_cross_asset_ingestion_root_cause.md`
documented that SPY, VIX, all sector ETFs and FRED `DGS2` had no
producer-side counterpart in the scheduled ingestion chain: the
universe-driven path (Polygon/Stooq/YahooHistorical) iterates
`companies.ticker`, and SPY/VIX/XL* are not in `companies`; the
FRED `_SERIES` constant never included `DGS2`/`VIXCLS`. Six features
(`return_vs_spy_5d/20d`, `return_vs_sector_5d/20d`, `beta_60d`,
`vix_change_5d`) were 100% NULL on every live prediction since the
2026-05-10 retrain.

**Decision.** Option A from the investigation: new
`ReferenceTickersIngestor` with a hardcoded constant (bypasses
universe lookup), wired into `orchestrator._ALL_INGESTORS`; FRED
`_SERIES` extended with `DGS2` + `VIXCLS`; cross-asset freshness
check added to `health/ml_checks.py`. No `companies` row pollution
(SPY isn't a tradeable engine candidate), no schema change.

**What shipped.**
- `ingestion/ingest_reference_tickers.py` (NEW) —
  `ReferenceTickersIngestor` with `REFERENCE_TICKERS = ('SPY',
  'VIX', 'XLK', 'XLF', 'XLV', 'XLE', 'XLY', 'XLI', 'XLP', 'XLB',
  'XLU', 'XLRE')`. Reuses `_parse_yf_response` from
  `ingest_yahoo_historical.py`. Writes to `prices_daily` with
  `source='yahoo'`, ON CONFLICT DO NOTHING.
- `ingestion/orchestrator.py` — `ReferenceTickersIngestor` added
  to `_ALL_INGESTORS` between `YahooHistoricalIngestor` and
  `FREDIngestor`. Dry-run confirms it appears as `[active]`.
- `ingestion/ingest_fred.py` — `_SERIES` extended with
  `'DGS2': '2-Year Treasury Yield'` and
  `'VIXCLS': 'VIX Close'`.
- `health/ml_checks.py` — `check_cross_asset_freshness()` asserts
  each reference ticker has a `prices_daily.trade_date` within
  T-3; returns `{check_name, status, severity, message}` matching
  the existing check signature. Threshold: 3 days.
- `tests/equity/test_reference_tickers_ingestor.py` (NEW) — 5
  TDD-RED-first tests: constant contents, orchestrator
  registration, full-write coverage, universe-arg bypass,
  `source='yahoo'` recording.
- `tests/equity/test_fred_ingestor_series.py` (NEW) — 3 tests:
  `DGS2` present, `VIXCLS` present, existing-series preserved.
- `tests/equity/test_health_ml_checks.py` (extended) — 4 new
  tests: empty-DB fail, all-fresh pass, stale-SPY fail with
  ticker named, missing-ticker fail with ticker named.

**Verification.**
- 12 new tests pass; 25 adjacent existing tests still pass (37
  total in the regression sweep:
  `test_reference_tickers_ingestor.py + test_fred_ingestor_series.py
  + test_health_ml_checks.py + test_yahoo_historical.py +
  test_orchestrator_universe_sort.py`).
- `venv/bin/python main.py ingest --dry-run` lists 12 sources
  including `reference_tickers [active]` between
  `yahoo_historical [experimental]` and `fred [active]`.

**Pending (out of scope, not blocked by this branch).**
- One-time backfill `2026-05-05 → today`: local-only script at
  `.claude/local_scripts/backfill_cross_asset_2026-05-05.py` (path
  is gitignored). Operator runs after this branch merges; it
  invokes `ReferenceTickersIngestor.ingest()` with a 1y window so
  ON CONFLICT preserves any rows from the prior manual bootstrap.
- KI-149 — `ml/predict.py` silently scores T-2 when ml_features
  for the latest prices_daily date is incomplete (separate
  branch).
- KI-150 — `mhde-equity-pipeline-monitor.service` has been
  failing since 2026-05-14; once fixed, the new
  `check_cross_asset_freshness` becomes the alert path
  (separate branch).
- Model retrain (will pick up DGS2/VIXCLS once `macro_series` is
  warm and `ml_features` is rebuilt against fresh cross-asset
  data).

---

## 2026-05-14 — Equity universe-tier sort fix: ORDER BY DESC restores 99 displaced ML-universe tickers (ADR-031 / KI-143)

**Branch:** `fix-equity-orchestrator-tier-sort` (committed +
pushed; **STOPPED for operator review/merge** — push-only per
branch-handoff pattern, operator opens the PR). Single-repo
(MHDE), no engine repo change, no schema change.

**Trigger.** Read-only investigation A from the prior session traced
the ml_features 411 → 312 drop on 2026-05-04 to the orchestrator's
universe sort + dev-mode cap. Operator confirmed Option A (one-character
SQL fix) + asked me to flag scope; I identified the same SELECT
duplicated in `pipelines/daily_radar.py:77` (the call site that
actually reaches production — it passes its result to
`ingestion.orchestrator.run_all` as `tickers_override`, short-
circuiting the orchestrator's own SELECT). Operator chose Option 1
of the scope question (fix both files; track helper-extraction as a
follow-up KI). Branch carries the production-effective fix.

**Decision.** Add `DESC` to the ORDER BY in both files:
```sql
SELECT ticker FROM companies WHERE is_active = true
ORDER BY universe_tier DESC, ticker
```
`'primary'` > `'extended'` alphabetically, so DESC puts the 504
primary-tier tickers in positions 0-503 of the sorted list. The
520-slot `max_symbols` cap now fills as 504 primary + first 16
extended, exactly matching the production intent. ADR-031 records
the rationale and four alternatives considered (two-pass selection,
remove cap entirely → KI-145, shared helper → KI-144, no-op).

**What shipped.**
- `ingestion/orchestrator.py` — line 75 ORDER BY clause now
  `universe_tier DESC, ticker`. Inline block comment cites ADR-031
  + KI-143 + KI-144 (helper follow-up) so a future reader doesn't
  remove `DESC` without re-reading the rationale.
- `pipelines/daily_radar.py` — line 77 identical change. Inline
  comment cross-references the orchestrator ADR rationale and notes
  this is the call site that reaches production.
- `tests/equity/test_orchestrator_universe_sort.py` (NEW) — 5
  pinned regressions:
  1. `test_primary_tier_fills_cap_first` — behavioural pin: 500
     primary + 174 extended seeded; first 520 slots after the
     fixed sort contain all 500 primary + 20 extended.
  2. `test_full_universe_returned_when_below_cap` — edge case:
     small universe (4 tickers) returns full set regardless of
     cap.
  3. `test_inactive_tickers_excluded` — sanity pin: the
     `is_active = true` filter remains intact.
  4. `test_orchestrator_uses_primary_first_sort` — duplication-pin
     A: reads orchestrator source, asserts the fixed ORDER BY
     clause is present and the buggy form is not.
  5. `test_daily_radar_uses_primary_first_sort` — duplication-pin
     B: same on daily_radar source. Both fail-loud if either file
     drifts.
- `DECISIONS.md` — ADR-031: context (latent-buggy from inception,
  triggered by extended-tier population on 2026-05-01/02),
  decision, three alternatives considered with rationale per
  rejection (two-pass selection, remove cap, shared helper),
  trade-off accepted (158 extended-tier tickers no longer ingested
  but they don't flow through to ml_features anyway per KI-122),
  files of record, reversibility.
- `KNOWN_ISSUES.md` — KI-143 added (opened + resolved 2026-05-14,
  full entry in "Recently resolved"), KI-144 opened (shared helper
  extraction follow-up), KI-145 opened (`max_symbols` cap removal
  follow-up). KI-122 amended to note the displacement-side-effect
  is now mitigated. Header observation count updated 11 → 13.

**Verification (L5).**
- RED: `.venv/bin/python -m pytest
  tests/equity/test_orchestrator_universe_sort.py` before fix → 2
  of 5 tests fail (the duplication-pins) with the expected
  "must contain 'ORDER BY universe_tier DESC, ticker'" messages.
  The 3 behavioural tests pass under both code paths because they
  exercise SQL semantics (identical pre/post on the test seed)
  and the cap intent (only differs at scale, caught by the
  duplication-pins).
- GREEN (post-fix): same command → **5 passed**.
- Full equity suite (`.venv/bin/python -m pytest tests/equity/
  --ignore=tests/equity/test_ml_predict.py -q`) → **775 passed,
  2 failed**. The 2 failures
  (`test_smoke_test_fails_without_active_models`,
  `test_smoke_test_flags_missing_joblib`) are pre-existing
  `joblib`-import failures in `monitoring/smoke_test.py`,
  unrelated to this branch (confirmed identically failing on
  master in the prior session via `git stash` baseline).
- Live verification against current `data/mhde.duckdb` via
  `.claude/local_scripts/diag_post_fix_universe.py` (gitignored,
  re-runnable):
  ```
  Companies:  primary 504, extended 174
  ML universe size: 416, Cap = 520
  Pre-fix sort:  312 ML-universe tickers in cap
  Post-fix sort: 416 ML-universe tickers in cap
  Recovered:     104  e.g. ['ODFL', 'OKE', 'ON', 'ORCL', 'ORLY', 'OTIS', 'OXY', 'PANW']
  Newly excluded: 0
  Tier composition: primary 504 + extended 16 = 520
  ```
  Full ML universe recovered. Zero regressions.

**Files of record.** `ingestion/orchestrator.py` (line 75 + block
comment), `pipelines/daily_radar.py` (line 77 + block comment),
`tests/equity/test_orchestrator_universe_sort.py` (new, 5 tests),
`DECISIONS.md` (ADR-031), `KNOWN_ISSUES.md` (KI-143 opened+resolved,
KI-144 + KI-145 opened, KI-122 amended), this log. No CLAUDE.md
change. No CHANGELOG.md in repo. Diagnostic script
`.claude/local_scripts/diag_post_fix_universe.py` is gitignored.

**Pending operator action.**
1. Review the branch + ADR-031 (especially the alternatives
   considered).
2. Merge `fix-equity-orchestrator-tier-sort`. No deploy step
   needed — the next `mhde-daily-analysis.service` fire (23:15 UTC)
   picks up the new code automatically.
3. Watch the 23:15 UTC daily-radar log: ingestion phase should
   process all 504 primary tickers (vs ~346 pre-fix). The next
   00:15 UTC `mhde-predict.service` should compute features for
   ~411 ML-universe tickers (vs 311 pre-fix; the remaining 5 gaps
   are the IPO ingestion holes — UBER, TXT, PTC, TYL, PSKY —
   tracked separately, not addressed here). The 01:00 UTC equity
   pipeline-monitor should flip 🟢 on the features step.
4. Historical T-2 prediction rows for 2026-05-04 → 2026-05-14
   are not retroactively rewritten. Coverage advances cleanly
   from the next predict run forward.

**Open follow-ups carried.**
- **KI-144 (new)** — extract the duplicated universe SELECT to a
  shared helper. The duplication is what let the bug slip through
  code review; eliminating it makes future drift impossible.
- **KI-145 (new)** — remove `max_symbols=520` cap entirely. After
  the 2026-05-09 grouped-daily switch (commit `473b92a`) it
  rate-limits nothing and is structurally vestigial. Removing it
  would moot the KI-143 displacement class permanently and
  auto-extend ML coverage to any future S&P additions.
- **KI-122 (amended)** — extended-tier reconciliation leak: the
  *displacement* side-effect is now neutralized by ADR-031, but
  the underlying reconciliation gap (174 stale extended-tier rows
  marked `is_active=true`) remains.

---

## 2026-05-14 — Equity Stooq freshness fix: today-exact match restores T-1 features after the Polygon grouped-daily switch (ADR-030 / KI-142)

**Branch:** `fix-equity-stooq-freshness` (committed + pushed;
**STOPPED for operator review/merge** — push-only per
branch-handoff pattern, operator opens the PR). Single-repo (MHDE),
no engine repo change, no schema change.

**Trigger.** Per-pipeline equity monitor (ADR-026, deployed
2026-05-12) flagged `Feature pipeline (ml_features)` 🔴 two days
running:
```
🔴 Equity Pipeline 2026-05-13 01:00 UTC
🔴 Feature pipeline (ml_features) — MAX(trade_date)=2026-05-11 (311 rows) — expected features for 2026-05-12
🔴 Equity Pipeline 2026-05-14 01:00 UTC
🔴 Feature pipeline (ml_features) — MAX(trade_date)=2026-05-12 (311 rows) — expected features for 2026-05-13
```
Operator triage handed me a read-only investigation; this branch is
the follow-on fix.

**Investigation findings (read-only phase).**
- `mhde-predict.service` (`/etc/systemd/system/`, system-level) was
  running on schedule (00:15 UTC daily, exit 0, both 05-13 and 05-14)
  with the chained `ml backfill-features → ml predict` ExecStart pair
  intact and the timer enabled. No service regression.
- The cadence regression was real and pinpointable. `equity_predict.log`
  showed `Scoring universe for {trade_date}` matching `prices_daily latest`
  (T-1) every day from system inception through 2026-05-12, then
  silently slipping to T-2 starting 2026-05-13 (`prices_daily latest=
  2026-05-12, Scoring universe for 2026-05-11` and the analogous shift
  the next morning).
- Root cause traced upstream to `ingestion/ingest_stooq.py:_tickers_needing_prices`
  and its `trade_date >= today - 2 days` predicate. The predicate had
  worked because the pre-`473b92a` per-ticker Polygon path was rate-
  limited and routinely left every universe ticker without a T-1 row
  by the time Stooq ran; Stooq's broad sweep then patched today over
  the gap. Commit `473b92a` (2026-05-09) switched to the grouped-daily
  endpoint, which serves T-1 reliably at 23:15 UTC (and 403s on T-0
  because Polygon hasn't published the same UTC day yet). The new
  steady state — every universe ticker has a T-1 row, none has a T-0
  — silently satisfied the 2-day predicate, Stooq's missing-list went
  to ~3 ADRs/night, and `prices_daily` for T-0 stayed at ~4 macro
  rows. `ml backfill-features` therefore couldn't write a feature
  row for T-0; `ml predict` fell back to `MAX(trade_date)` = T-1 in
  `ml_features`; the prediction surface lagged a day; the new monitor
  caught it.
- Stooq production-log evidence across three nights:
  `Stooq: 517 rows for 517/520 tickers` (2026-05-11 23:15, pre-grouped),
  `Stooq: 2 rows for 2/3 tickers` (2026-05-12 23:15, post-grouped),
  `Stooq: 6 rows for 6/6 tickers` (2026-05-13 23:15).
- Confirmed live DB state via read-only diagnostic
  `.claude/local_scripts/diag_equity_pipeline_state.py`:
  `prices_daily 2026-05-13: 4 tickers (HTHIY, IFNNY, RSHGY from stooq;
  SUNB from yahoo); ml_features MAX(trade_date) = 2026-05-12 (311
  rows); 0 universe tickers with prices for 2026-05-13`.

**Decision.** Tighten Stooq's freshness predicate to `trade_date =
today` exactly. Aligns the freshness check with what Stooq's `/q/l/`
endpoint actually returns (today's quote), eliminates the false-
positive "fresh" reading that yesterday's polygon row produced under
the 2-day window, and restores the pre-2026-05-09 universe-sweep
behaviour that gave T-0 prices. ADR-030 records the rationale,
alternatives considered (per-ticker polygon same-day pass —
incompatible with free-tier rate limits; daily-radar reschedule to
~10:00 UTC — operationally invasive; pipeline-monitor expectation
relaxation — hides the underlying data regression), and trade-offs.

**What shipped.**
- `ingestion/ingest_stooq.py` —
  `_tickers_needing_prices(conn, tickers)` rewritten to query
  `WHERE trade_date = ?` with today's UTC date as the parameter.
  `_FRESHNESS_DAYS = 2` constant deleted; `timedelta` import dropped
  (no other use in the file). Inline body comment cites ADR-030 +
  KI-142 so a future reader does not "widen this back to 2 days"
  without re-reading the rationale.
- `tests/equity/test_ingest_stooq.py` — 4 new pinned regressions:
  1. `test_tickers_needing_prices_returns_universe_when_only_yesterday_in_db`
     — RED-pin, unit-level: universe with only T-1 rows must come
     back as needing today's price.
  2. `test_tickers_needing_prices_skips_when_today_already_present`
     — forward-pin: ticker with today's row is fresh.
  3. `test_ingest_fetches_today_when_universe_has_only_yesterday`
     — RED-pin, end-to-end: HTTP call is made and row inserted with
     `trade_date = today, source = 'stooq'`.
  4. `test_polygon_t1_does_not_short_circuit_stooq_t0`
     — orchestration-shape integration test, the gap the original
     test suite missed: 5-ticker universe with T-1 polygon rows,
     asserts Stooq writes T-0 for all 5.
  Existing `test_polygon_prices_not_overwritten_by_stooq` re-seeded
  to use today's date so the PK-overwrite guard remains meaningful
  under the new contract.
- `DECISIONS.md` — ADR-030 entry: context (why the 2-day window
  worked pre-grouped; why it broke post-grouped), decision, four
  alternatives considered with rationale for each rejection, trade-off
  accepted (Stooq HTTP load returns to pre-2026-05-09 levels),
  files of record, reversibility.
- `KNOWN_ISSUES.md` — KI-142 added (opened + resolved 2026-05-14).
  Header sentence + observation count unchanged (KI-142 closed in the
  same session, like KI-138).

**Verification (L5).**
- RED: `.venv/bin/python -m pytest tests/equity/test_ingest_stooq.py -v`
  before fix → 3 of 4 new tests fail with the expected messages
  quoted in their assertion strings (`assert set() == {'AAPL','MSFT',
  'NVDA'}`, `assert 0 == 1` for the HTTP call count, `assert 0 == 5`
  for the orchestration shape). Forward-pin (test 2) green under
  both code paths, as designed.
- GREEN (post-fix): same command → **15 passed**.
- Full equity suite (`.venv/bin/python -m pytest tests/equity/
  --ignore=tests/equity/test_ml_predict.py -q`) → **770 passed, 2
  failed**. Both failures (`test_smoke_test_fails_without_active_models`,
  `test_smoke_test_flags_missing_joblib`) confirmed identically failing
  on master via `git stash` baseline; both fail on `import joblib` in
  `monitoring/smoke_test.py:34` — pre-existing `.venv` environment
  issue, unrelated to this branch. `test_ml_predict.py` skipped
  for the same `joblib` reason.
- Regression suite (`.venv/bin/python -m pytest tests/regression/
  -q`) → **30 passed, 3 failed**: `test_no_module_level_connection`
  (KI-105), `test_active_model_paths_resolve` (joblib-related),
  `test_repo_vs_deployed_unit_parity` (KI-112) — all pre-existing on
  master.
- `venv/bin/python -m py_compile` on the touched ingestion file →
  clean.

**Files of record.** `ingestion/ingest_stooq.py`,
`tests/equity/test_ingest_stooq.py`, `DECISIONS.md` (ADR-030),
`KNOWN_ISSUES.md` (KI-142 opened + resolved), this log. No
CLAUDE.md change. No CHANGELOG.md in repo (matches prior session
pattern). Diagnostic script
`.claude/local_scripts/diag_equity_pipeline_state.py` is gitignored
and re-runnable for the operator.

**Pending operator action.**
1. Review the branch + the ADR-030 rationale (especially the four
   alternatives considered).
2. Merge `fix-equity-stooq-freshness`. No deploy step needed beyond
   the merge — the next `mhde-daily-analysis.service` fire (23:15 UTC)
   picks up the new code automatically.
3. Watch the 23:15 UTC daily-radar log: the Stooq line should jump
   from `~6 rows / 6/6 tickers` back to `500+ rows / 500+/500+
   tickers`. The next 00:15 UTC `mhde-predict.service` run should log
   `Scoring universe for {T-1}` matching `prices_daily latest` (i.e.
   today's expected target). The 01:00 UTC equity pipeline-monitor
   fire should flip 🟢 on the Feature pipeline step.
4. Historical T-2 prediction rows (2026-05-13, 2026-05-14) are not
   retroactively rewritten — the prediction surface advances cleanly
   from the next predict run forward.

**Open follow-ups.** None tracked here. The orchestration-shape gap
that let the original regression slip through is now pinned by
`test_polygon_t1_does_not_short_circuit_stooq_t0`. ADR-030 explicitly
notes that the per-ingestor test suites had no test exercising the
post-Polygon, pre-Stooq DB state where the failure mode lived;
adding one is the durable fix, not an open follow-up.

---

## 2026-05-14 — Pipeline_execution monitor false-positive fix: crypto recency budget 27h → 51h (ADR-029 / KI-141)

**Branch:** `fix-pipeline-execution-crypto-threshold` (committed +
pushed; **STOPPED for operator review/merge** — push-only per
branch-handoff pattern, operator opens the PR). Single-repo (MHDE),
no engine repo change, no schema change.

**Trigger.** `monitoring/pipeline_execution.py` was alerting the
crypto leg through ~21 hours of every UTC day even when the daily
00:30 UTC scoring fired on time. Root cause: the monitor's recency
check compared `now - midnight(MAX(prediction_date))` against a
`27h` budget commented "24h cycle + 3h grace". But
`crypto/ml/predict.py:score_universe` writes
`prediction_date = MAX(trade_date) FROM crypto_ml_features` — the
last completed features day, i.e. T-1 calendar. So the age right
after a healthy fire is already ~24h 30m, and the gap before the
next fire pushes it up to ~48h 30m. The 27h budget assumed
prediction_date incremented to *today*, which it never does.

**Decision.** Option A from the task brief: raise the crypto
threshold to `timedelta(days=2, hours=3)` (51h = 48h cycle + 3h
grace), mirroring equity's ADR-015 pattern (which already absorbs
the same T-1 semantic plus the Fri→Mon weekend roll). Option B
(switch the monitor to a run-time column) was rejected for this
PR because `crypto_ml_predictions` carries no reliably-populated
run-time timestamp — adding one is a writer-touching multi-file
change tracked separately as KI-141.

**What shipped.**
- `monitoring/pipeline_execution.py` — `RECENCY_BUDGET['crypto']`
  changed from `timedelta(hours=27)` to `timedelta(days=2, hours=3)`.
  Inline comment rewritten to spell out the T-1 cause and cite
  ADR-029 + KI-141 so a future reader does not "tighten this back
  down" without re-reading the rationale.
- `tests/regression/test_pipeline_execution_crypto_t1.py` (NEW) —
  three TDD-pinned regressions:
  1. `test_crypto_ok_at_normal_afternoon` — on-time fire yesterday,
     now = 14:00 UTC today, age ≈ 38h → must pass. (The case the
     old 27h budget got wrong.)
  2. `test_crypto_ok_just_before_next_fire` — on-time fire
     yesterday, now = 00:29 UTC tomorrow, age ≈ 48h 29m → must
     pass. (Tightest pre-fire moment in the cycle.)
  3. `test_crypto_fails_when_two_consecutive_fires_missed` —
     latest prediction_date 3 days ago, now 04:00 UTC, age ≈ 52h
     → must fail with `threshold` mentioned in the reason.
- `DECISIONS.md` — ADR-029: context (T-1 semantic), decision,
  alternatives considered (run-time column / on-read offset /
  operator-configurable), trade-off accepted (single-day-miss
  insensitivity), files of record, reversibility.
- `KNOWN_ISSUES.md` — KI-141 added (run-time column follow-up),
  count updated 10 → 11, header sentence amended.

**Verification (L5).**
- RED: `venv/bin/python -m pytest tests/regression/test_pipeline_execution_crypto_t1.py -v` (before fix) → 2 failed, 1 passed; failure reasons quoted "age N old, threshold 1 day, 3:00:00" — exactly the bug.
- GREEN (post-fix): same command → **3 passed**.
- Full pipeline_execution suite:
  `venv/bin/python -m pytest tests/regression/test_pipeline_execution_*.py tests/equity/test_monitoring.py` → **44 passed**.
- Pre-existing failures outside this fix (`test_dashboard_structure.py::test_no_module_level_connection` KI-105, `test_systemd_units.py::test_repo_vs_deployed_unit_parity` KI-112) confirmed present on master via `git stash` baseline — unrelated to this branch.

**Pending operator action.**
- Open the PR, merge, restart `mhde-pipeline-execution.service` (or
  let the next hourly fire re-import). No DB migration, no engine
  change. The drift monitor and dashboard are unaffected.

**Open follow-up (KI-141).** Add `created_at TIMESTAMP DEFAULT
CURRENT_TIMESTAMP` to `crypto_ml_predictions` (and bundle the same
for equity `ml_predictions` under ADR-015 if the operator agrees),
then switch the monitor's recency check to `MAX(created_at)` with a
24h + grace budget. Trade-off doc lives in KI-141; ADR-029
explicitly defers this so the false-positive fix could ship as a
one-line change.

---

## 2026-05-14 — Post-parabolic filter v2: add short-window momentum rule `return_5d < -0.30` (ADR-028)

**Branch:** `feat-postparabolic-add-ret5-filter` (committed + pushed;
**STOPPED for operator review/merge** — push-only per branch-handoff
pattern, operator opens the PR). Extends ADR-021's `should_exclude`
with an OR-combined second rule. Single-repo (MHDE), no engine repo
change, no JSON-interface change.

**Trigger.** Two confirmed live failure patterns surfaced after ADR-021
went live:
- **SWARMSUSDT 2026-05-14** entered at the 2026-05-13 prediction (dd90
  −50.0%, ret60 +147%, ret5 −36.8%, 9/10 down days). Rule A did not fire
  (`ret60 = +1.47` is below the `+2.0` baseline gate). Position
  immediately drew down to −22% within 24h.
- **4USDT 2026-05-12** at −11.8% unrealized — same dd-depth family but
  different shape (deep dd + 60d downtrend + recent bounce).
Prior research session ran a paired backtest of three short-momentum
candidate variants (ret5 < -0.20; down_days ≥ 7; their union), all of
which destroyed Sharpe by ~4 points. A tighter Variant D
(`ret5 < -0.30`) was Sharpe-positive and operator-approved.

**What shipped.**
- `crypto/config.py` — `POSTPARABOLIC_RET5_THRESHOLD = -0.30` (+ updated
  block comment describing the two-rule OR-gate).
- `crypto/ml/postparabolic_filter.py` — `should_exclude(dd90, ret60, ret5=None)`
  signature with OR-combined logic; three stable reason tokens
  (`REASON_POST_PARABOLIC`, `REASON_SHORT_MOMENTUM`, `REASON_BOTH`); the
  legacy `REASON` constant kept as a back-compat alias. Each rule
  fail-opens on its own missing input.
- `crypto/exports/write_daily_predictions.py:build_predictions` — reads
  `return_5d` off the raw feature row, passes it to `should_exclude`,
  logs the value (with NULL/NaN safely coerced to `-999.0` in the
  printf), persists into `crypto_signal_exclusions.ret5` (coerces NaN/None
  to SQL NULL via a small inline `_to_db` helper). UPSERT now includes
  the new column.
- `crypto/schema.py` — `crypto_signal_exclusions` schema gains a
  `ret5 DOUBLE` column; idempotent `ALTER TABLE … ADD COLUMN IF NOT
  EXISTS` migration applied inside `create_all_tables` so existing live
  rows pick the column up (NULL until next UPSERT). Header comment
  updated to cite ADR-028.
- `crypto/ml/POSTPARABOLIC_FILTER_SPEC.md` — new v2 section at the top
  documenting Rule B, backtest evidence, pattern characterization, live
  verification, audit-trail change, and the explicit non-coverage of the
  4USDT-class. v1 content preserved verbatim below it.
- `DECISIONS.md` — ADR-028 entry: context (two live incidents), decision
  (Rule B threshold + OR-combined), threshold rationale (the full
  variant grid: A/B/C reject; D accept; E/DE reject), schema-change
  rationale, 4USDT-class explicit non-coverage, expected live impact,
  files of record, reversibility (one-line config change).
- `KNOWN_ISSUES.md` — KI-137 mitigation paragraph rewritten to cover v1
  + v2; header counts updated to 10 open; new KI-140 for the 4USDT-class
  pattern (characterized, not addressable by current filter shape,
  deferred to a separate workstream — proposes 3 directions: tighter
  entry-conditional time-stop, probability haircut, direction-aware
  label).
- Tests (TDD, RED → GREEN):
  `tests/crypto/test_postparabolic_filter.py` — 13 new cases covering
  ret5-only firing, exact-edge strictness (=`-0.30` does not fire),
  just-below excludes, missing/NaN ret5 fails open (per-input),
  default-value preserves legacy callers, combined reason token,
  reason-token stability/distinctness, plus two **live-incident pins**
  (SWARMSUSDT 2026-05-13 excluded; 4USDT 2026-05-11 not excluded).
  `tests/crypto/exports/test_write_daily_predictions.py` — 4 new
  integration cases (short-momentum-only drops symbol; audit row
  records ret5 + short_momentum reason; both rules → combined reason;
  NULL ret5 fails open without affecting Rule A evaluation).

**Verification (L5).**
- `venv/bin/python -m pytest tests/crypto/test_postparabolic_filter.py
  tests/crypto/exports/test_write_daily_predictions.py -v` → **48 passed**
  (24 in `test_postparabolic_filter.py`, 24 in
  `test_write_daily_predictions.py`).
- `venv/bin/python -m pytest tests/crypto/ tests/regression/ -q` → **459
  passed, 1 skipped, 3 pre-existing failures unrelated to this work**
  (`test_no_module_level_connection` — KI-105 dashboard module-level
  conn, `test_repo_vs_deployed_unit_parity` — KI-112 deployed-unit drift,
  `test_migration_backfills_pre_existing_db` — pre-existing on master,
  confirmed identically failing on the branch base after a fresh
  `git stash` re-run).
- **Live dry-run** (`.claude/local_scripts/dryrun_postparabolic_extension.py`,
  gitignored, read-only connection) against `crypto_ml_features`
  MAX(trade_date) = 2026-05-13 on 48 active coins: 4 would-be exclusions
  — SKYAIUSDT + ZEREBROUSDT by Rule A unchanged, **DOGSUSDT + SWARMSUSDT
  newly by Rule B**. SWARMSUSDT 2026-05-13 (the live incident) caught;
  4USDT 2026-05-11 (the separate-workstream incident) correctly not
  caught.
- `venv/bin/python -m py_compile` on all touched files → clean.
- Pre-commit hook (py_compile + smoke pytest + lint) → expected OK on
  commit (see Pending below).

**Files of record.** `crypto/config.py`, `crypto/ml/postparabolic_filter.py`,
`crypto/exports/write_daily_predictions.py`, `crypto/schema.py`,
`crypto/ml/POSTPARABOLIC_FILTER_SPEC.md`, `DECISIONS.md` (ADR-028),
`KNOWN_ISSUES.md` (KI-140 + KI-137 mitigation update),
`tests/crypto/test_postparabolic_filter.py`,
`tests/crypto/exports/test_write_daily_predictions.py`, this log. No
CLAUDE.md change, no CHANGELOG.md in repo.

**Pending operator action.**
1. Review the branch + the ADR-028 evidence table.
2. Merge `feat-postparabolic-add-ret5-filter`. No deploy step needed —
   the change takes effect on the next scheduled 00:40 UTC export run
   automatically. The `ret5` column is created by
   `create_all_tables`, which the application calls on every fresh
   connection / fresh DB; the idempotent ALTER picks up the column on
   the existing live `crypto_signal_exclusions` table the first time
   any code path that goes through `create_all_tables` opens the live
   DB. The first post-merge export will write fully-populated rows; any
   pre-existing rows keep `ret5 = NULL` until re-UPSERTed on a future
   day's exclusion of the same `(export_date, symbol, model_id)`.
3. Watch the 00:50 monitor on day 1 — expect identical pipeline status
   (filter widening doesn't change pipeline shape). On 2026-05-13 data
   it would add 2 exclusions (DOGSUSDT, SWARMSUSDT) on top of the 2
   pre-existing Rule A exclusions.

**Open follow-ups carried.**
- **KI-140 (new)** — 4USDT-class pattern; the deep-dd + 60d-downtrend +
  recent-bounce signature is not addressable by the current
  `should_exclude` shape (Variant E backtested at −1.96 Sharpe and
  rejected). Three hypothesized directions documented; none scoped.
- **KI-137 (continued)** — the model-level root cause (volatility-loving
  label) remains the real fix; this ADR is one more guard rail.

---

## 2026-05-12 — Move the crypto daily chain (export / engine entry / pipeline monitor) up to 00:40–00:50 UTC, right after predict (ADR-027)

**Branch:** `fix-pipeline-timers-early-fire` (committed; **STOPPED — branch
ready for operator review/merge/deploy**). MHDE side: two `.timer` OnCalendar
changes + docs. The matching engine-side change (`trading-engine-entry.timer`
06:30 → 00:45) is on the crypto-trading-engine branch `fix-entry-timer-early-fire`.
No code change, no migration, no schema change.

**Trigger.** `mhde-crypto-predict.service` fires at 00:30 and reliably finishes
in ~2 min (observed 2:05–2:40/day this week, +30s on a write-lock retry), but
the downstream steps were left at their pre-00:30-era times: predictions export
06:15, engine `entry` 06:30, crypto pipeline monitor 06:40. ⇒ a built-in ~6h
gap between fresh predictions and the engine acting on them, and a 6h-late
surfacing of any overnight predict breakage. Operator asked to collapse it.

**Change (MHDE).** `systemd/mhde-crypto-export-predictions.timer`
`06:15:00 → 00:40:00`; `systemd/mhde-crypto-pipeline-monitor.timer`
`06:40:00 → 00:50:00`. Both keep `Persistent=true`; the export `.service` keeps
`After=mhde-crypto-predict.service`. Buffers ≈ 2× the worst observed duration of
the preceding step: predict ~2–3 min → export at +10 min; export ~30–60s →
engine entry at +5 min; engine entry ~1–30s → monitor at +5 min. Target chain:
00:30 predict → 00:40 export → 00:45 engine entry → 00:50 monitor. Rationale,
duration evidence and the deploy-ordering constraint recorded in ADR-027.

**Not changed.** `active_spec.json` / `crypto/exports/spec_config.py` still
carry `runtime.entry_time_utc: "06:30"` — loaded into the engine's
`RuntimeConfig` but never read by engine code (documentation only); editing it
changes the spec hash and forces a coordinated spec-reload + re-ack on the
engine, for no functional gain. Stale-metadata note left in ADR-027; correct it
in a future routine spec bump. The predict timer (00:30) is unchanged.

**Verification (L4).**
- `systemd-analyze verify systemd/mhde-crypto-export-predictions.timer
  systemd/mhde-crypto-pipeline-monitor.timer` → clean;
  `systemd-analyze calendar '*-*-* 00:40:00'` / `'*-*-* 00:50:00'` → normalize
  as expected (system TZ is UTC).
- `venv/bin/python -m pytest tests/monitoring tests/regression
  tests/test_session2_infra_smoke.py -q` → all green **except** the two
  pre-existing failures unrelated to this work
  (`test_dashboard_structure.py::test_no_module_level_connection` —
  `dashboard/app.py:931`; `test_systemd_units.py::test_repo_vs_deployed_unit_parity`
  — now also lists `mhde-crypto-export-predictions.timer` as repo-vs-deployed
  drift, which is **expected** until the operator deploys the new timer; the
  monitor timer isn't deployed yet so it's still skipped by that test).
- pre-commit hook (py_compile + smoke pytest + lint) → OK on commit.

**Files of record.** `systemd/mhde-crypto-export-predictions.timer`,
`systemd/mhde-crypto-pipeline-monitor.timer`, `DECISIONS.md` (ADR-027),
`OPERATIONS.md`, `ARCHITECTURE.md`, `docs/PIPELINE_MONITORING.md`, this log.

**Pending operator action (deploy — ORDER MATTERS).**
1. **MHDE export timer first.** Merge this branch.
   `sudo cp systemd/mhde-crypto-export-predictions.timer /etc/systemd/system/` →
   `sudo systemctl daemon-reload` →
   `sudo systemctl restart mhde-crypto-export-predictions.timer` →
   `systemctl list-timers mhde-crypto-export-predictions.timer` (confirm next
   elapse = tomorrow 00:40). Optionally fire once now and confirm
   `data/exports/predictions_latest.json` has today's `export_date`.
2. **Engine entry timer second** (only after step 1 is live). Merge the
   crypto-trading-engine `fix-entry-timer-early-fire` branch.
   `sudo cp systemd/trading-engine-entry.timer /etc/systemd/system/` →
   `sudo systemctl daemon-reload` →
   `sudo systemctl restart trading-engine-entry.timer` →
   `systemctl list-timers trading-engine-entry.timer` (confirm 00:45).
   *Critical:* doing this before step 1 means the 00:45 entry reads
   yesterday's stale export and skips entry that day.
3. **Monitor timer third.** `sudo cp systemd/mhde-crypto-pipeline-monitor.timer
   /etc/systemd/system/` → `daemon-reload` → `restart` (only if the
   pipeline-monitor units have already been deployed at all — see the earlier
   ADR-026 session entry; if not yet deployed, just include the new time when
   you do).
4. Watch the next morning's 00:50 Telegram message (should be all-green) and
   `journalctl -u trading-engine-entry.service -n 50` (entry ran ~00:45 on a
   fresh file).

## 2026-05-12 — Pipeline monitor: one Telegram message per pipeline, outcome-based step checks (ADR-026)

**Branch:** `feat-pipeline-monitoring` (committed; **STOPPED — branch ready
for operator review/merge/deploy**). MHDE-side only: new
`monitoring/pipeline_monitor/` package + tests + 4 systemd unit pairs + 4 CLI
commands + docs. No engine repo change, no migration, no DATABASE_SCHEMA
change (every check reads existing tables; no new tables).

**Trigger.** Operator-approved design after KI-138: a regression where every
script exited 0, the crypto predictions export froze, the engine rejected the
stale file, and no positions were placed — invisible for ~24h to the existing
health-check and `pipeline-execution` monitors. Need a second observability
layer that verifies *today's* run produced the right outputs all the way
through to engine entry, by **reading the DB/files** rather than trusting exit
codes.

**What was built.**
- `monitoring/pipeline_monitor/core.py` — `Status` enum (GREEN/RED/SKIPPED),
  `StepResult` / `PipelineResult` dataclasses, `evaluate_steps()` (linear
  cascade — first RED ⇒ remaining steps SKIPPED ⚪, SKIPPED never makes the
  pipeline RED, exceptions ⇒ RED), `render_telegram_message()`
  (`🟢/🔴 <Pipeline> Pipeline <date> <HH:MM UTC>` header — 🔴 iff any step
  red — then one `🟢/🔴/⚪ <step> — <detail>` line per step).
- `checks/crypto.py` — 9 outcome checks: OHLCV ingestion (`MAX(trade_date)` in
  `crypto_prices_daily` ≥ today-1 under cap-at-today-1/ADR-022), data-quality
  guard (no `systemic_corruption` row in `crypto_data_quality_reports` for the
  last 2 days), funding/OI ingestion (`crypto_funding_rates.funding_time` &
  `crypto_open_interest.trade_date` ≥ today-1), feature pipeline
  (`crypto_ml_features` rows for `MAX(trade_date)`), model predictions
  (`crypto_ml_predictions` active-model rows for `prediction_date`), outcome
  tagging (no matured active-model prediction, forward window closed ≥ 2 days
  ago, still has `actual_hit` NULL), export predictions
  (`data/exports/predictions_latest.json` resolves to a file with
  `export_date == today` and ≥ 1 prediction — this is the KI-138 catch),
  engine ingest (engine `engine_runs` has a successful `entry` phase today,
  read-only), engine entry/positions (≥ 1 `positions` row with
  `entry_date == today`, or 0 and the book is already at `max_concurrent` from
  `active_spec.json`).
- `checks/equity.py` — 4 checks against the equity tables (ingestion /
  features / predictions for the most recent closed market day via
  `pipelines.market_calendar.expected_equity_prediction_date`; dashboard-data
  refresh = `data/processed/prediction_vs_actual_rows.csv` mtime within 4
  days).
- `checks/fx.py` — 2 checks: bar ingestion (reuses
  `pipelines.freshness.check_fx_freshness`, forex-closed-window aware/KI-128;
  fed a naive-UTC `now`), signal generation (`MAX(datetime_utc)` in
  `fx_signals` ≥ `MAX(datetime_utc)` in `fx_prices_hourly`).
- `daily_runner.py` — `run_pipeline(pipeline, …)` opens the MHDE DuckDB
  read-only (and, for crypto, the engine DuckDB read-only via
  `CRYPTO_ENGINE_DB_PATH`; failure ⇒ `engine_conn=None`, the engine steps go
  red but don't block the message), runs `evaluate_steps`, returns a
  `PipelineResult`. `main(pipeline)` sends one Telegram message every run
  (green heartbeat or red), exit 0/1. Dependencies/paths are injectable for
  tests.
- `continuous_runner.py` — `run_continuous()` runs 3 independent checks (no
  cascade): FX hourly-bar freshness, engine `monitor`-timer liveness (RED if
  no success in > 15 min), engine `entry`-timer ran today (RED only after
  08:00 UTC). `main()` is **silent when all green**, sends one message listing
  every check when any is red. The engine `reconcile` timer is **not** checked
  (`CHECK_ENGINE_RECONCILE = False` — disabled pending RECONCILE-001).
- `monitoring/alert.py` — new `send_text(text)` (logs payload at INFO,
  respects `MONITORING_DRY_RUN`, bottoms out in `fx.bot.telegram_bot.send_message`).
- `main.py` — 4 new commands under the `monitor` group:
  `crypto-pipeline` / `equity-pipeline` / `fx-pipeline` / `continuous`.
- 4 systemd unit pairs (Type=oneshot, `User=jpcg`, system-level pattern,
  `TimeoutStartSec=120`, log to `data/logs/pipeline_monitor_*.log`):
  `mhde-crypto-pipeline-monitor` (06:40 UTC daily, After
  `mhde-crypto-export-predictions.service`, has `CRYPTO_ENGINE_DB_PATH`),
  `mhde-equity-pipeline-monitor` (01:00 UTC daily, After `mhde-predict.service`),
  `mhde-fx-pipeline-monitor` (12:10 UTC daily),
  `mhde-continuous-monitor` (`*:0/30`, has `CRYPTO_ENGINE_DB_PATH`).
  Not deployed — operator deploys (copy → `daemon-reload` → `enable --now`).

**Adapted to engine-DB reality (vs. the original spec wording).** The engine
DB has no `entry_complete` event type and no machine-readable "why 0 positions"
field, so crypto step 9 counts `positions` rows with `entry_date == today` and
softens 0 → green only when the book is at `max_concurrent`.
`crypto_data_quality_reports` has no `is_systemic` column, so step 2 keys on
the `systemic_corruption` check-name row. `mhde-predict.timer` actually fires
00:15 UTC (ARCHITECTURE.md's "21:00" is stale), so the equity monitor is at
01:00. All recorded in KI-139 / ADR-026.

**Verification (L5).**
- `venv/bin/python -m pytest tests/monitoring/ -q` → **114 passed** (90 new
  pipeline-monitor tests across `test_core.py` 13, `test_checks_crypto.py` 38,
  `test_checks_equity.py` 16, `test_checks_fx.py` 9, `test_daily_runner.py` 8,
  `test_continuous_runner.py` 6; + the 24 pre-existing `paper-trading-drift`).
- `venv/bin/python -m pytest tests/monitoring tests/pipelines tests/regression
  tests/test_session2_infra_smoke.py -q` → 181 passed, **2 pre-existing
  failures unrelated to this work** (`test_dashboard_structure.py::test_no_module_level_connection`
  — `dashboard/app.py:931`; `test_systemd_units.py::test_repo_vs_deployed_unit_parity`
  — `mhde-crypto-predict.service` deployed-copy drift; both fail identically on
  the branch base `51903b5`). The two regression tests that *did* relate
  (`test_no_untracked_systemd_units`, `test_no_production_py_imports_untracked_module`)
  pass once the new files are `git add`-ed.
- `venv/bin/python -m py_compile` on all new/modified `.py` → clean.
- `systemd-analyze verify` on all 8 unit files → clean.
- `venv/bin/python main.py monitor --help` → the 4 new commands listed.
- Demo (`.claude/local_scripts/demo_pipeline_monitor.py`, gitignored): against
  the **live** DB all three daily pipelines render all-green and the continuous
  monitor is green; simulating the KI-138 shape (stale `predictions_latest.json`
  → `export_date='2026-05-10'`) ⇒ the crypto pipeline header goes 🔴, step 7
  "Export predictions" is 🔴 ("…export_date='2026-05-10' — expected 2026-05-12;
  predictions export is stale (engine will reject it)"), and steps 8–9 cascade
  to ⚪. The regression *would* have been caught the same morning.

**Files of record.** `monitoring/pipeline_monitor/{__init__.py,core.py,
daily_runner.py,continuous_runner.py,checks/{__init__.py,crypto.py,equity.py,fx.py}}`,
`tests/monitoring/pipeline_monitor/*`, `monitoring/alert.py`, `main.py`,
`systemd/mhde-{crypto,equity,fx}-pipeline-monitor.{service,timer}`,
`systemd/mhde-continuous-monitor.{service,timer}`, `docs/PIPELINE_MONITORING.md`,
`DECISIONS.md` (ADR-026), `KNOWN_ISSUES.md` (KI-139), `ARCHITECTURE.md`,
`OPERATIONS.md`, this log.

**Pending operator action (post-merge).**
1. Merge the branch.
2. Deploy the 4 unit pairs at system level (`/etc/systemd/system/`), parallel
   to the existing monitor units: copy files → `systemctl daemon-reload` →
   `systemctl enable --now mhde-{crypto,equity,fx}-pipeline-monitor.timer
   mhde-continuous-monitor.timer`. The crypto + continuous units need
   `CRYPTO_ENGINE_DB_PATH` (already in the unit files).
3. Add the 4 new timers to the `config-drift` monitor's expectation set if it
   enumerates units explicitly.
4. Sanity-fire once with `MONITORING_DRY_RUN=true` to see the payloads in the
   journal before letting the real Telegram sends go live.

**Pending engineering follow-up.** KI-139 limitations — no auto-remediation,
no dashboard view, coarse equity dashboard-mtime check, "0 positions" → red
with a note (no precise reason from the engine DB), engine `reconcile` timer
not checked (flip `CHECK_ENGINE_RECONCILE` when RECONCILE-001 re-enables it).


## 2026-05-12 — KI-138: cap-at-today-1 ingestion broke the prediction-export staleness gate (option A)

**Branch:** `fix-export-preflight-cap-at-today-1` (committed; **STOPPED —
branch ready for operator merge**). MHDE-side only: `crypto/exports/write_daily_predictions.py`
+ its tests + docs. No engine repo change, no systemd change, no migration.

**Trigger.** Since commit `8f9d707` ("stop freezing partial-day OHLCV
candles", ADR-022) landed on 2026-05-11, `crypto export-predictions` aborted
every day with `ExportPreflightError("features stale: MAX(trade_date)=…-1,
expected …")`. The ingestion fix caps OHLCV at `today - 1` (only fully-closed
UTC days), so `MAX(trade_date)` in `crypto_ml_features` is structurally
`today-1`, but `_check_freshness` required it to equal `today`. Result:
`data/exports/predictions_latest.json` froze at the last pre-`8f9d707` file,
the engine rejected it on `export_date != today_utc` (INTERFACE.md §3.2), and
no positions were placed.

**Fix (option A).** In `crypto/exports/write_daily_predictions.py`:
- `_check_freshness(conn, export_date)` now accepts `MAX(trade_date) ==
  export_date` **or** `== export_date - 1`, returns the validated
  features-as-of date, and still raises for anything older (≥2 days = genuine
  staleness).
- `build_predictions` loads features for that returned features-as-of date
  (not blindly for `export_date`); the empty-symbols error message names both
  dates.
- JSON `export_date` is unambiguously `today` UTC (the trading date —
  INTERFACE.md §3.1 / engine §3.2 validation).
- New informational JSON field `features_as_of_date` (= the `MAX(trade_date)`
  used for inference; `export_date - 1` on a normal cap-at-today-1 run).
  Engine loader unchanged; doesn't read it.
- `crypto_signal_exclusions.export_date` and `predicted_at` still use the
  trading date — unchanged. Module / function docstrings updated.

**What was NOT done (deliberate).** Option B — aligning
`crypto_ml_predictions.prediction_date` (written by `score_universe`,
= `MAX(trade_date)` = `today-1`, semantically the features/entry date) with
the export's trading date — is left as a deferred follow-up (schema change;
touches predict, outcome-fill, dashboard, tests). The exporter does its own
inference and never reads `crypto_ml_predictions`, so there's no operational
conflict. Flagged in KI-138 + ADR-025.

**Verification (L5).**
- `venv/bin/python -m pytest tests/crypto/exports/test_write_daily_predictions.py`
  → 20 passed (3 new: `test_preflight_accepts_features_one_day_old`,
  `test_export_date_is_today_utc_and_features_as_of_is_yesterday`,
  `test_features_as_of_date_equals_max_trade_date_when_same_day`; 1 renamed/
  retargeted: `test_preflight_fails_when_features_two_days_stale`;
  `test_write_does_not_touch_files_on_preflight_failure` updated for the new
  tolerance).
- `venv/bin/python -m pytest tests/crypto/` → 415 passed, 1 skipped.
- `venv/bin/python -m py_compile crypto/exports/write_daily_predictions.py main.py` → clean.

**Files of record.** `crypto/exports/write_daily_predictions.py`,
`tests/crypto/exports/test_write_daily_predictions.py`, `KNOWN_ISSUES.md`
(KI-138, intro), `DECISIONS.md` (ADR-025), this log.

**Pending operator action (post-merge).**
1. Merge the branch.
2. One-off regenerate today's file: `venv/bin/python main.py crypto export-predictions`
   (confirm `predictions_latest.json` now has `export_date` = today,
   `features_as_of_date` = today-1, non-empty `predictions`).
3. Trigger a manual entry-timer fire on the engine side; confirm it accepts
   the fresh file and opens positions.

**Pending doc follow-up.** Update `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md`
§3 to document the new optional `features_as_of_date` field (coordinated, but
non-breaking — the daily predictions file isn't hash-canonicalised).

**Pending engineering follow-up.** Option B (see KI-138 / ADR-025).

## 2026-05-11 — KI-136: paper-trading dashboard shows real exit_price / realized_pnl

**Branch:** `fix-paper-closed-trades-exit-price-ki136` (committed + pushed;
**STOPPED for operator review** — diff only, no PR). No engine / monitor /
predict / spec change; dashboard read-side + docs only.

**Trigger.** The dashboard's "Recent closed positions" table (and its CSV
download) showed `"uncomputable (KI-136)"` for *every* closed row even though
the crypto-trading-engine `positions` table now carries real `exit_price` /
`realized_pnl_usd` (engine-side EXIT-PRICE-001 + reconcile-side backfill, both
merged on the engine repo; the 2026-05-11 manual flatten of SKYAIUSDT 576a025d
/ TAGUSDT f25a428a / ZEREBROUSDT cb092940 populated −129.41 / −30.43 / −46.26
USD respectively). Root cause: `dashboard/services/queries.py:get_paper_closed_trades`
hardcoded `"exit_price": _UNCOMPUTABLE` / `"realized_pnl": _UNCOMPUTABLE` for
all rows and never SELECTed the two columns — pure display lag behind the
engine schema change, no caching involved.

**What changed:**
- `dashboard/services/queries.py` — `get_paper_closed_trades` now SELECTs
  `exit_price, realized_pnl_usd` and renders them: `exit_price` verbatim,
  `realized_pnl` `round(_, 2)` (USD → cents). Each falls back to
  `"uncomputable (KI-136)"` **only** when its own column is NULL — handled
  independently (a reconcile backfill can recover the SELL price but leave P&L
  NULL if `entry_price` was NULL). Added a docstring + a comment on the
  `_UNCOMPUTABLE` constant spelling out when it shows.
- `dashboard/app.py` — "Recent closed positions" caption rewritten: defines the
  two columns + the `(exit−entry)·qty` gross / FUNDING-001 framing, and that
  `"uncomputable (KI-136)"` now means only "no exit fill recorded"
  (pre-EXIT-PRICE-001 closes not yet backfilled, orphan auto-closes).
- Docs: `KNOWN_ISSUES.md` KI-136 — added an "Update (2026-05-11)" block
  (engine persists exit price now; dashboard reads it; Check C of the drift
  monitor auto-activates off the SELL `orders.price` join, optional future
  simplification to read `positions.realized_pnl_usd` directly; remaining
  KI-136 scope = P&L-band/DD/monthly arms, still blocked on `daily_pnl`).
  `OPERATIONS.md` + `ARCHITECTURE.md` Paper-Trading-tab descriptions updated.

**Tests (TDD).** `tests/dashboard/test_paper_trading_queries.py`: `_engine_db`
fixture schema + `_pos` helper gained `exit_price` / `realized_pnl_usd`;
`test_closed_trades_exit_price_uncomputable` replaced by four cases — populated
columns shown (exit verbatim, P&L rounded to −129.41); both NULL → placeholder;
exit price known but P&L NULL → independent fallback; orphan `engine_only_position`
auto-close → still placeholder + `close_reason` names it. Watched the two new
"populated" cases fail first (`ValueError: could not convert 'uncomputable (KI-136)'`),
then go green. `tests/dashboard` 66 passed; `tests/dashboard tests/monitoring`
90 passed. Pre-commit: OK.

**Verified against the live engine DB** (`get_paper_closed_trades(limit=30)`):
SKYAIUSDT 2026-05-11 16:44 → exit_price 0.38288 / realized_pnl −129.41; TAGUSDT
16:45 → 0.0013717 / −30.43; ZEREBROUSDT 16:45 → 0.04613 / −46.26; SKYAIUSDT
2026-05-10 12:24 orphan → "uncomputable (KI-136)" (NULL entry/exit, reason
`engine_only_position`). The older 2026-05-10 closes still read "uncomputable"
— correct: those are pre-fix closes the reconcile backfill hasn't healed yet
(engine `trading-engine-reconcile.timer` still disabled on the VPS; see the
engine repo's redeploy + `systemctl enable --now` loose end).

**Pre-existing dirty state set aside.** Before branching, `git stash -u` parked
the operator's uncommitted `SESSION_LOG.md` Phase-1B-re-run entry + the untracked
`.claude/local_scripts/*` diagnostic scripts (`stash@{0}` on `master`,
message `pre-dashboard-fix WIP …`). Left in place per operator instruction —
restore with `git stash pop` after this branch lands. **This means the
Phase-1B-re-run session entry is *not* in `SESSION_LOG.md` on this branch.**

**Pending (operator):** review + merge; `git stash pop` on master to restore the
parked work; (engine repo, separate) VPS redeploy + reconcile timer re-enable so
the older closes get backfilled. Optional follow-up: switch
`monitoring/paper_trading_drift.py` Check C to read `positions.realized_pnl_usd`
directly instead of reconstructing from the `orders` join.

---

## 2026-05-11 — Phase 1B sensitivity grid: post-repair re-run

**Branch:** `master` (no code change). **DB:** overwrote 82 rows in
`crypto_backtest_runs` / their `crypto_backtest_trades` / `crypto_backtest_summary`
(force-re-run). Read-only on prediction tables. No engine-config / `active_spec.json`
change.

**Trigger.** The original Phase 1B base+sensitivity grid (2026-05-08/09) ran on
pre-repair OHLCV; the 2026-05-07 partial-candle bug + the ~6-day hold-window
repair shifted the canonical 10d run modestly (Sharpe 6.32→6.25, cumRet
51.2→52.2%) per the post-parabolic-toggle session entry below — which flagged the
full grid for a post-repair re-run.

**What was done.** `.claude/local_scripts/phase1b_rerun.py`: snapshot every
`crypto_backtest_runs` + `_summary` row to `.../phase1b_original_snapshot.json`,
rebuild a `GridConfig` per stored run, re-run all **82** Phase 1B configs via
`runner.run_grid(force=True, skip_existing=False)`, `apply_postparabolic_filter=False`.
Excluded the 4 non-1B runs (2 knockout `model_id_like=kowf` paired backtests,
2 post-parabolic-filter-ON runs — all 2026-05-11, already post-repair). 82/82
completed, 0 failed; `n_predictions_seen` held at 16,679 for every config (walk-fold
OOS set unchanged). Two of the 82 (`backtest_10d_D_top_n_a02e15a0` = deployed
winner, `backtest_5d_D_top_n_5aff7b45` = 5d sibling) were already post-repair
(re-run 2026-05-11) so they reproduced byte-for-byte. Comparison + ranking +
portfolio re-check scripts: `phase1b_rerun_analyze.py`, `phase1b_rerun_portfolio.py`;
full side-by-side in `phase1b_rerun_result.json`.

**Result.** Uniform tiny shift, same direction the canonical run already showed:
ΔcumRet median +0.47 pp, Δhit_rate +~0.3 pp, ΔSharpe ≈ flat (median +0.02; the
realistic Policy-D top_n configs moved −0.03 to −0.10), ΔmaxDD ≈ 0, Δn_trades 0…+4
(the only non-zero ΔmaxDD values are sum-of-fractions artifacts on the net-losing
Policy-B configs). **Top-3 per horizon: set and order unchanged** — 10d
`{d884e9f2, c06dded9, 2378c511}`, 5d `{db11de9b, 9d5b95c5, b65bbd09}`; nothing
entered or left. Deployed winner `backtest_10d_D_top_n_a02e15a0` (Policy D, top_n=6,
trail_pct=0.30, activation_pct=0.01, 10d): sum-of-fractions Sharpe 6.32(pre)/6.25(post),
maxDD −17.0% unchanged, cumRet 51.2→52.2%; **portfolio metrics** (`simulate_portfolio`,
$1000/6/80%/1×) post-repair Sharpe 5.108, maxDD −23.73% (identical to documented),
PF 4.014, end-equity $34,259, annRet ~+3043% — **still PASSES all four Phase 1B
gates** with the same ~1.3 pp drawdown margin. Base point `e08cf9da` still FAILS the
25% DD gate exactly as before. The iterated multi-axis configs (`d884e9f2`,
`c06dded9`) still out-Sharpe the winner by the same margins they always did — and
are still outside the agreed single-axis sensitivity contract; the repair doesn't
change that calculus (open item: KI-125 follow-up / `PHASE1B_HANDOFF.md`).

**Verdict: WINNER UNCHANGED — no action required.** Policy D / top_n=6 /
trail_pct=0.30 / activation_pct=0.01 / 10d (`PHASE1B_WINNER_RUN_ID =
backtest_10d_D_top_n_a02e15a0`) remains optimal on corrected data. No engine
config change, no `active_spec.json` change, no migration.

**Pending (optional, operator):** `PATH_TO_LIVE_PLAN.md`'s "Phase 1B selected
winner" block still quotes pre-repair numbers (sum-of-fractions Sharpe 6.32→6.25;
portfolio equity $32,121.89→$34,259; PF 3.811→4.014; annRet "+2854%"→~+3043%).
A `crypto export-spec` re-run would refresh the metrics embedded in
`data/exports/active_spec.json` (no parameter field changes — spec-hash logic
unaffected). Neither done here (out of task scope).

---

## 2026-05-11 — Knockout label phase 2 — training + validation + paired backtest

**Branch:** `feat-crypto-knockout-training` (committed + pushed; **STOPPED for
operator review — verdict is HOLD, do not promote**; diff only, no PR).

**Trigger.** Phase 2 of the knockout label (ADR-023): train knockout models,
validate per the spec §5 criteria, paired backtest, promotion recommendation.

**What shipped (no predict.py / write_daily_predictions.py / validation_gate.py /
engine / dashboard change):**
- `crypto/ml/train.py` — `train_walk_forward(..., label_kind="legacy", auto_promote=True)`:
  `label_kind="knockout"` → trains on `label_Nd_knockout`, `model_id` prefixed
  `crypto_{horizon}_knockout_…`, bundle records `label_kind`/`knockout_tp`/`knockout_sl`;
  `auto_promote=False` → INSERT row as `is_active=false, promotion_status='pending'`
  and **skip the validation gate entirely** (no `validate_promotion` call, no flip).
  `_persist_and_gate` gains `label_kind` / `auto_promote` params (defaults preserve
  legacy behaviour). `crypto train --label-kind {legacy,knockout}` CLI option;
  `knockout` loops 5d+10d, no auto-promote.
- `crypto/schema.py` — `crypto_ml_model_runs.label_kind VARCHAR DEFAULT 'legacy'`
  (CREATE + idempotent `ADD COLUMN IF NOT EXISTS`).
- `crypto/execution/backtest/harness.py` — `model_id_like` param on
  `load_oos_predictions` / `count_predictions_below_funding_floor` / `run_backtest`
  (default `'crypto_%_walkfold_%'`; folded into `make_run_id` only when non-default
  so existing run_ids/parameters JSON are unchanged) — lets a paired A/B backtest
  replay an alternative walk-forward set.
- Tests: `tests/crypto/test_knockout_training.py` (5: `--label-kind` wiring +
  default; knockout `train_walk_forward` persists correctly — model_id encodes
  `knockout`, `label_kind='knockout'`, `is_active=false`, gate not called, bundle
  has the knockout params; legacy run unchanged; `_persist_and_gate(auto_promote=False)`;
  harness `model_id_like` filter). Full crypto suite: 412 passed, 1 skipped.
  Full suite: 1458 passed, 1 skipped, 2 pre-existing failures (KI-105 dashboard;
  KI-112 systemd-deploy parity). Pre-commit: OK. TDD throughout.

**Trained (is_active=false, promotion_status=pending, label_kind=knockout):**
`crypto_5d_knockout_53b91781` (precision_at_threshold 0.428, AUC 0.572, base
0.230, lift 1.94×) and `crypto_10d_knockout_6c7754c2` (0.394 / 0.553 / 0.275 /
1.47×), 18/18 walk-forward folds each, finals in `models/saved/crypto/`. OOS
walk-forward probabilities persisted to `crypto_ml_predictions` as
`crypto_{hz}_kowf_{YYYY_MM}` (20,133 rows/horizon, 2024-12 → 2026-05).

**Validation (spec §5):**
- C1 walk-forward precision ≥ 0.40: 5d 0.428 PASS; 10d 0.394 FAIL.
- C2 calibration bucket check: **FAIL both** — lower buckets ~ok, upper buckets
  wildly over-confident (10d [0.8,0.9): predicts 0.84, realizes 0.30). Per-fold
  Platt on a 20% within-fold split doesn't generalise for this harder label.
- C3 paired backtest (Phase-1B-winner config, post-parabolic filter OFF): **FAIL
  both, mixed.** 5d Run A (legacy) Sharpe 6.10 / maxDD −9.1% / cumRet 47.5% vs
  Run B (knockout) **4.39** / −7.9% / 51.0% (one 7.6× outlier tanks the Sharpe).
  10d A 6.25 / −17.0% / 52.2% vs B **7.36** / **−10.7%** / **44.2%** — Sharpe &
  maxDD *improve*, cumRet ~−15% (outside ±10%). Trade counts: knockout runs more
  (5d 1165 vs 1054; 10d 1093 vs 932).
- Bonus: knockout top-6 picks trip the post-parabolic filter ~half as often
  (5d 1.1% vs 2.2%; 10d 1.0% vs 2.4%) — the label *is* organically avoiding the
  SKYAI profile.

**Verdict: HOLD — do not promote either model.** Direction right (avoids the
post-parabolic profile; 10d risk metrics improve) but execution not there:
calibration broken, AUC ~0.55 (barely above random — the legacy edge was partly
"volatility", which the knockout label removes by design). Phase-3 retry needed:
(a) fix calibration (isotonic / larger held-out cal set); (b) reconsider TP/SL
(wider band — +15%/−7% — has a higher learnable `tp` rate per the spec scan);
(c) add directional features; (d) re-run the paired backtest. The post-parabolic
filter (ADR-021) stays as the working symptom-guard meanwhile. Promotion SQL is
printed in the report for reference but **NOT executed**; `is_active` unchanged
on all models. Docs: ADR-024, this entry. Analysis script:
`.claude/local_scripts/knockout_phase2_run.py`.

---

## 2026-05-11 — Knockout (triple-barrier) crypto label — phase 1 (label + backfill)

**Branch:** `feat-crypto-knockout-label` (committed + pushed; **STOPPED for
operator review before phase 2 (training)** — diff only, no PR).

**Trigger.** Root-cause fix for the SKYAI false-positive class — the legacy
`label_Nd_10pct` (close-based, +10% "tagged at any point") is direction-agnostic
and volatility-rewarding. Spec `crypto/ml/KNOCKOUT_LABEL_SPEC.md` written +
operator-approved in the prior session (TP=+0.10, SL=−0.05, horizons 5d/10d,
neither→loss, same-bar→SL-first, keep both labels / additive schema).

**What shipped (phase 1 — no train.py / predict.py / model / dashboard / validation_gate change):**
- `crypto/config.py` — `KNOCKOUT_TP = 0.10`, `KNOCKOUT_SL = -0.05` (+ docstring).
- `crypto/ml/knockout_label.py` (new) — pure `knockout_classify(forward_highs,
  forward_lows, entry_close, tp, sl, horizon, sl_first=True) -> (outcome, resolve_day)`;
  `outcome ∈ {'tp','sl','neither'}`, `resolve_day` 1-indexed (None for 'neither');
  same-bar both-touch → 'sl' when `sl_first`; NaN/None bars treated as "no touch";
  non-positive `entry_close` / empty window → `('neither', None)`. No DB I/O.
- `crypto/schema.py` — six new `crypto_ml_labels` columns (`label_{5d,10d}_knockout
  BOOLEAN`, `knockout_outcome_{5d,10d} VARCHAR`, `knockout_resolve_day_{5d,10d}
  INTEGER`) in the CREATE + idempotent `_CRYPTO_ML_LABELS_MIGRATIONS`
  (`ADD COLUMN IF NOT EXISTS`) run by `create_all_tables`.
- `crypto/ml/labels.py` — `compute_labels` `INSERT` changed to an explicit
  column list (so the new columns don't break it) + a new `_compute_knockout_labels`
  forward-walk pass (loads `crypto_prices_daily` per symbol, walks bar by bar,
  bulk-`UPDATE`s the six columns from a registered DataFrame; the close-based
  INSERT can't express first-touch). `label_Nd_knockout = (outcome == 'tp')`.

**Backfill** (`crypto backfill-labels`, full window, 27,483 rows; sub-second):
`label_5d_knockout` base rate **23.1%** (`tp` 23.1% / `sl` 60.0% / `neither` 16.9%);
`label_10d_knockout` base rate **27.8%** (`tp` 27.8% / `sl` 65.5% / `neither` 6.7%)
— vs the legacy `label_10d_10pct` 35.8% (the gap is the purged volatility-loving
false-wins). Median resolve day: `tp` ≈ 2, `sl` ≈ 1–2. Consistency checks pass
(`resolve_day` NULL ⇔ outcome='neither'; `label` ⇔ outcome=='tp'). Legacy
columns untouched (0 NULLs). 5 random spot-checks recompute identically from raw
OHLCV. **SKYAI case confirmed:** entry at the 2026-05-10 close (0.54185) → the
2026-05-11 bar (H 0.55680 / L 0.38260) pierces the −5% barrier (0.51476) on day 1
before the high reaches the +10% barrier (0.59604) → `outcome='sl'`, `resolve_day=1`,
both horizons. (No `crypto_ml_labels` row for SKYAI 2026-05-10 — no forward bar in
the DB yet; verified via the Binance 05-11 bar.)

**Tests.** `tests/crypto/test_knockout_label.py` — 20 (TP-first / SL-first /
later-bar / neither / exact-edge TP+SL inclusive / same-bar→sl / same-bar→tp option /
gap-up-through-TP / gap-down-through-SL / partial window ± touch / empty window /
non-positive entry / NaN bar skipped / horizon caps lookahead / negative-SL barrier
semantics / config-constant signs / the SKYAI day-1-loss case). `tests/crypto/test_labels.py`
— 6 new integration tests (TP win, SL loss, same-bar SL-first, neither→loss, 5d/10d
horizon difference, legacy columns untouched). Full crypto suite: 437 passed, 1
skipped. Pre-commit: OK. TDD throughout.

**Docs.** ADR-023 (DECISIONS.md), `crypto_ml_labels` knockout-columns section in
DATABASE_SCHEMA.md, this entry. **Phase 2 (separate task):** train a knockout
model (`train.py --label-kind`), `fill_outcomes` knockout-aware `actual_hit`,
`validation_gate` same-label comparison, dashboard caption, then the side-by-side
validation against the spec §5 promotion criteria.

**Note** — `tests/regression/test_systemd_units.py::test_repo_vs_deployed_unit_parity`
fails on `master` (and therefore on this branch) — it's the still-outstanding
deploy step from the data-quality-guard merge (the updated `mhde-crypto-predict.service`
hasn't been copied to `/etc/systemd/system/` + `daemon-reload`ed). Not introduced
by this branch; flagged so it isn't mistaken for a regression here.

---

## 2026-05-11 — Data-quality guard / volume-cliff detector

**Branch:** `feat-crypto-data-quality-guard` (committed + pushed;
**STOPPED for operator review/merge** — diff only, no PR).

**Trigger.** The safeguard that would have caught the 2026-05-07
partial-candle bug immediately — existing monitors only check row
*presence*, not *plausibility*. Noted as a follow-up in the OHLCV-fix
session entry below.

**Investigation (90-day post-repair clean-data scan, 4 367 symbol-days):**
clean 1st-percentile volume ratio ≈ 0.22, so 0.10 is safely below
organic quiet days; grid-swept volume × range thresholds (0.05/0.10/0.20
× 0.10/0.20/0.30) — **zero systemic false positives at every combo**
(clean-day max ≈ 10 % of the universe flagged; systemic threshold 30 %).
Reconstructed the 05-07…05-11 corruption (real Binance volumes + a
~2.5 %-volume partial-candle simulation vs the clean 20-day baseline):
flags ≈64–96 % of the universe **every day** at **every** combo →
systemic fires on day one. Chosen pair: **volume/trade cliff 0.10,
range collapse 0.20** (per-symbol WARN rate ≈ 0.55 %); 0.05/0.10 noted
as a near-silent conservative alternative.

**What shipped (no ingestion / model / feature change):**
- `crypto/config.py` — `OHLCV_PLAUSIBILITY_WINDOW_DAYS = 20`,
  `VOLUME_CLIFF_RATIO = 0.10`, `RANGE_COLLAPSE_RATIO = 0.20`,
  `TRADE_COUNT_CLIFF_RATIO = 0.10`, `SYSTEMIC_FLAG_RATIO = 0.30`,
  `SYSTEMIC_MIN_SYMBOLS = 10`.
- `pipelines/data_quality_guard.py` (new) — pure
  `check_ohlcv_plausibility(conn, target_date) -> QualityReport`
  (per-symbol `volume_cliff` / `range_collapse` / `trade_count_cliff`
  vs the trailing-20-day median; systemic iff ≥ 10 evaluable symbols and
  > 30 % flagged; warmup symbols fail open; empty date → clean `ok`),
  plus `persist_report(conn, report)` (UPSERT to the new table; clean
  reports write nothing). No DB writes / alerts / exits in the pure path.
- `crypto/schema.py` — new `crypto_data_quality_reports` table
  `(date, symbol, check_name, expected, observed, flagged, severity,
  created_at)`, PK `(date, symbol, check_name)`, in `ALL_SCHEMAS`.
- `main.py` — new `crypto check-data-quality [--date]` command: runs the
  check, persists, sends a Telegram alert (CRITICAL if systemic, WARN if
  per-symbol-only), and **exits non-zero on a systemic flag** unless
  `MHDE_DATA_QUALITY_GUARD_OVERRIDE` is set.
- `systemd/mhde-crypto-predict.service` — new `ExecStart=` for
  `check-data-quality` inserted right after `backfill-prices`; with
  `Type=oneshot` a systemic anomaly aborts the unit and every step below
  (funding/oi/labels/features/predict). **Deployed unit needs
  `systemctl daemon-reload` after merge.**

**Demonstrated catch:** integration test
`test_simulated_partial_candle_corruption_triggers_systemic` (50 symbols,
synthetic ~2.5 %-volume partial day → 50/50 flagged, systemic) + the
investigation reconstruction above. Live smoke: `crypto check-data-quality`
on the current (clean) DB → `evaluated=50 flagged=0 severity=ok rows_written=0`,
exit 0.

**Tests.** `tests/crypto/test_data_quality_guard.py` — 16 (per-symbol
volume / range / trade-count flags fire on synthetic bad data; do NOT
fire on noisy-but-normal data or just-above-threshold; systemic fires
when ratio exceeded; does NOT fire on an isolated single-symbol issue or
when too few symbols are evaluable; warmup symbols skipped; empty
universe / no-row-on-date handled gracefully; `severity` mapping;
`persist_report` writes flagged + systemic rows, idempotent UPSERT,
writes nothing for a clean report; the simulated-corruption integration
test). Full crypto suite: 381 passed, 1 skipped. Pre-commit: OK. TDD
throughout.

**Docs.** ADR-022 (DECISIONS.md), `crypto_data_quality_reports` in
DATABASE_SCHEMA.md, this entry. **Scoped out (phase 2):** no dashboard
view of `crypto_data_quality_reports`.

---

## 2026-05-11 — Backtest the post-parabolic filter (toggle + paired runs)

**Branch:** `feat-backtest-postparabolic-toggle` (committed + pushed;
**STOPPED for operator review/merge** — diff only, no PR). Separate from
`feat-crypto-postparabolic-filter` (the already-merged filter itself).

**Trigger.** Validate the post-parabolic exclusion filter (KI-137 /
ADR-021) at portfolio level before relying on it in production.

**What shipped (harness toggle only — no production-pipeline change):**
`crypto/execution/backtest/harness.py` —
- new `load_dd90_ret60_at_entry(conn, keys)` loader (mirrors
  `load_atr_at_entry`; reads `drawdown_from_90d_high` / `return_60d` from
  `crypto_ml_features`; NULL → absent key → fail-open).
- `make_run_id(...)` gains `apply_postparabolic_filter: bool = False`,
  folded into the hash **only when True** (baseline run_ids unchanged; a
  filter-on run gets a distinct id so paired A/B runs coexist).
- `RunState` gains `apply_postparabolic_filter` + `n_excluded_by_postparabolic`.
- `_run_lifecycle`: when the flag is set, drop post-parabolic candidates
  from `preds` (via `should_exclude`) *before* `_apply_selection` —
  exactly as the live export does (selection re-ranks the survivors).
  `n_predictions_seen` still reflects the pre-filter universe.
- `run_backtest(...)` gains the `apply_postparabolic_filter` kwarg
  (threaded to `make_run_id` / `RunState` / logged; stamped into the
  `parameters` JSON only when True so a filter-off run is byte-identical
  to the pre-toggle baseline).
Tests: `tests/crypto/test_backtest_postparabolic_toggle.py` — 4 (run_id
distinctness when on; `load_dd90_ret60_at_entry`; filter-off keeps a
post-parabolic candidate; filter-on drops it and `n_excluded_by_postparabolic`
increments while `n_predictions_seen` is unchanged). Full crypto suite:
365 passed, 1 skipped. Pre-commit: OK.

**Paired backtests run** (write to `crypto_backtest_*` only): Phase-1B-winner
config — Policy D, top_n n=6, trail_pct=0.3 — for 10d and 5d, filter
OFF vs ON, over the full funding-floored window (2025-04-05 → 2026-05-07).
Note Run A 10d re-runs the canonical `backtest_10d_D_top_n_a02e15a0`
run_id (`force=True`); its metrics shifted slightly (Sharpe 6.32→6.25,
cumRet 51.2%→52.2%) vs the pre-repair stored row because the recent
OHLCV repair corrected ~6 days of hold-window prices — the new numbers
are post-repair-correct; flag for the operator if the pre-repair row
needs preserving / the full Phase 1B grid wants a post-repair re-run.

**Result.** Filter fires on ~105 / ~16.7k walkfold predictions per
horizon (~0.6%), clustered in frothy months (Aug/Oct/Nov 2025, Jan 2026)
and a handful of pump-and-crash coins (MUSDT, FHEUSDT, ZEC, PENGU, DASH;
SKYAI itself only 2). Portfolio impact ≈ zero: 10d Sharpe 6.25→6.36,
maxDD −17.01→−16.98%, cumRet 52.23→52.48% (marginal improvement); 5d
Sharpe 6.10→5.82, maxDD −9.13→−9.29%, cumRet 47.54→51.46%, PF 2.56→2.75
(one big 3.4× winner survives in B and lifts mean/cumRet while denting
Sharpe — a wash). Trade *count* barely moves (selection backfills the
freed slot — 10d 932→941, 5d 1054→1053). The excluded set has ~2× the
*label* max-drawdown (10d −16.6% vs −8.7%) **and** a higher *label* hit
rate (10d 56.6% vs 35.2%) — i.e. by hit-rate the filter removes
"winners", but Policy D's trailing stop already truncates the drawdown
those trades would have caused, so the realized-P&L effect nets to zero.

**Verdict: KEEP the current thresholds (−0.20 / +2.0).** It costs
nothing at the portfolio level (within noise on both horizons) and
remains a cheap tail-risk / operator-trust guard against the SKYAI class
of trade. Widening to −0.15/+1.5 (spec-time scan: ~1.9% excluded, still
the high-DD tail) is an optional later move if more aggressiveness is
wanted; no evidence it's needed. No code change recommended beyond the
already-merged filter; the toggle stays in the harness for future
re-validation.

---

## 2026-05-11 — Crypto post-parabolic exclusion filter

**Branch:** `feat-crypto-postparabolic-filter` (committed + pushed to
origin; **STOPPED for operator review/merge** — diff only, no PR).

**Trigger.** Confirmed structural bias from the SKYAI diagnostic chain:
the crypto model re-emits buy signals on coins immediately after a
parabolic crash (SKYAI: calibrated prob 0.72–0.88 across the crash
window, *on clean data*). Spec `crypto/ml/POSTPARABOLIC_FILTER_SPEC.md`
written + approved in the prior session. Step 1 was investigation; this
is Step 2 (implementation).

**What shipped (option (b) — filter at the prediction-export step):**
- `crypto/config.py` — `POSTPARABOLIC_DD90_THRESHOLD = -0.20`,
  `POSTPARABOLIC_RET60_THRESHOLD = 2.0` (+ docstring).
- `crypto/ml/postparabolic_filter.py` (new) — pure
  `should_exclude(dd90, ret60) -> (bool, reason|None)`; excludes iff
  **both** `dd90 < -0.20` and `ret60 > 2.0` (strict); fail-open on
  None/NaN (logs DEBUG). No DB I/O, no imports from exports/dashboard.
- `crypto/schema.py` — new `crypto_signal_exclusions` table
  `(export_date, symbol, model_id, raw_probability, dd90, ret60, reason,
  created_at)`, PK `(export_date, symbol, model_id)`, added to
  `ALL_SCHEMAS`.
- `crypto/exports/write_daily_predictions.py::build_predictions` —
  after Platt calibration, before ranking: reads `dd90`/`ret60` from the
  *raw* feature row (not the median-filled `X`, so warmup symbols fail
  open), drops excluded coins, UPSERTs each into
  `crypto_signal_exclusions`, `logger.warning`s each one; re-ranks the
  survivors consecutively 1..N; an all-excluded day yields an empty
  `predictions` list with a WARNING (no crash — the engine then skips
  entry + alerts per INTERFACE.md §3.2 / §5.3). The
  `predictions_*.json` schema is unchanged (excluded coins simply absent
  + ranks renumbered). `crypto_ml_predictions` writes (`score_universe`)
  are **untouched** — the raw signal is preserved.

**Explicitly NOT done (phase-2 / out of scope):** no dashboard expander,
no `excluded_postparabolic` field in the predictions JSON, no
crypto-trading-engine changes, no change to `crypto_ml_predictions`.

**Threshold rationale (60-day historical scan, in
POSTPARABOLIC_FILTER_SPEC.md §3):** −0.20/+2.0 fires on 0.8% of
predictions (21 symbol-dates / 6 coins), isolating the high-drawdown
tail (excluded avg max-DD −25% vs retained −4%); on the daily top-6 it
never removed more than 2 picks on any day. Hard exclusion (not a
probability haircut) — it's a risk gate, not a probability adjustment.
Same thresholds for 5d and 10d; the engine consumes 10d only today.

**Tests.** `tests/crypto/test_postparabolic_filter.py` — 11 unit tests
(both conditions / only dd90 / only ret60 / exact-edge strictness ×2 /
just-inside / missing dd90 / missing ret60 / NaN inputs / negative
ret60 / stable reason token). `tests/crypto/exports/test_write_daily_predictions.py`
— 7 new integration tests (excluded symbol dropped; exclusion row
written with right values; UPSERT idempotent on re-run; re-rank
consecutive after exclusion; all-excluded → empty list, no crash;
missing-feature → not excluded). Full suite: 392 passed, 1 skipped, **1
pre-existing unrelated failure** (`tests/regression/test_dashboard_structure.py::test_no_module_level_connection`
— KI-105, `dashboard/app.py:931`, also fails on master). Pre-commit
(`scripts/pre-commit.sh`): OK. TDD throughout (tests written first;
RED on `ImportError` for the constants → behavior RED → GREEN).

**Docs.** ADR-021 (DECISIONS.md), KI-137 (KNOWN_ISSUES.md — opened,
*mitigated* by the filter; root-cause model-label fix is the open
follow-up), `crypto_signal_exclusions` row in DATABASE_SCHEMA.md, this
entry.

---

## 2026-05-11 — Crypto OHLCV ingestion fix: partial-day candles

**Branch:** `fix-crypto-ohlcv-partial-candle-ingestion` (committed + pushed
to origin; **STOPPED for operator review/merge** — diff only, no PR opened).

**Trigger.** Diagnostic chain (SKYAIUSDT "model keeps buying during the
crash"): the model was being fed garbage prices. Root cause = ingestion,
not the model. The daily `mhde-crypto-predict` timer fires 00:30 UTC;
`crypto/ingestion/backfill_ohlcv.py` fetched klines through `date.today()`
(the in-progress UTC day → ~30-min partial candle), inserted with
`ON CONFLICT DO NOTHING`, and `fetch_start = max(trade_date)+1` never
revisited. So from 2026-05-05/07 *every* symbol in `crypto_prices_daily`
had partial-day OHLCV (verified against the Binance futures public API:
SKYAIUSDT real 05-11 close $0.42, DB had $0.546; 5 other symbols affected
identically; perp `status=TRADING`, not delisted).

**What shipped (defense in depth, all three on `backfill_ohlcv`):**
1. `end_date` defaults to `date.today() - INGESTION_LAG_DAYS` (=1) — only
   fully-closed UTC days are requested.
2. `INSERT … ON CONFLICT (symbol, trade_date) DO UPDATE SET` all OHLCV
   columns — re-writes self-correct.
3. per-symbol `fetch_start = max(trade_date) - (REFETCH_WINDOW_DAYS-1)`
   (=3-day trailing window) — idempotent + self-healing with the UPSERT.
New constants `INGESTION_LAG_DAYS` / `REFETCH_WINDOW_DAYS` in
`crypto/config.py`.

**Other ingestion paths audited (item 4):** `backfill_funding.py` — was
`DO NOTHING` (no `max+1`/`today()` parts; funding events are final on
publish) → switched to `DO UPDATE`, kept `end_date=today()` so same-day
settled funding is still ingested. `backfill_oi.py` — was `DO NOTHING`;
already re-fetches a rolling 30-day window (`limit=30`) but the newest
point is the in-progress day, so it froze the same way → switched to
`DO UPDATE`. `universe_builder.py` — no bug (DELETE + reinsert from 24hr
tickers, already `DO UPDATE`); unchanged.

**Tests.** `tests/crypto/test_backfill_ohlcv.py` — 6 new (the 4 specified:
never requests today's UTC date; trailing re-fetch window spans
`REFETCH_WINDOW_DAYS`; UPSERT overwrites on re-run; synthetic frozen
partial candle self-heals — plus 2 audited-path: funding & OI UPSERT
overwrite). Full suite: 1391 passed, 1 skipped, **1 pre-existing
unrelated failure** (`tests/regression/test_dashboard_structure.py::
test_no_module_level_connection` — KI-105, `dashboard/app.py:931`, also
fails on master). Pre-commit (`scripts/pre-commit.sh`): OK. Files:
`crypto/config.py` (+14), `crypto/ingestion/backfill_ohlcv.py` (+30/-4),
`backfill_funding.py` (+7/-1), `backfill_oi.py` (+7/-1),
`tests/crypto/test_backfill_ohlcv.py` (+183).

**Explicitly NOT done (out of scope):** repairing the existing bad rows in
`crypto_prices_daily` (separate operator task — `DELETE … WHERE trade_date
>= '2026-05-05'` then `crypto backfill-prices`, then re-run labels →
features → predict for that window; today's SKYAI paper entry was on bad
data, real price ~$0.42). Nothing downstream of ingestion (features,
labels, models, predictions, dashboard, systemd units) was touched.
Also worth a follow-up: add a data-quality check that flags implausible
day-over-day volume/range collapses (would have caught this on 05-07).

---

## 2026-05-11 — Gap 3: paper-trading dashboard tab

**Branch:** `gap3-paper-trading-dashboard-tab` (committed; **STOPPED for
operator review before push** — then PR via `gh`, operator merges via
GitHub UI).

**Trigger.** Three-gap observability plan, Gap 3 — the last of the three.
The operator has no in-product view of paper-trading state; the engine
DuckDB lives in a separate repo.

**Design doc.** `docs/superpowers/specs/2026-05-11-paper-trading-dashboard-tab-design.md`.
Approved by operator before implementation.

**What shipped.**
- `dashboard/services/queries.py` — new read-only engine-DB query/transform
  functions (engine DB path from `CRYPTO_ENGINE_DB_PATH`, default
  `/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`, ADR-020):
  `_connect_engine` / `engine_db_path`; `get_paper_open_positions` (live
  states only; `calc_stop` = `peak − trail_pct·(peak − entry)` once the
  trailing stop activates, else `"— (not activated)"`; NULL prices → `"—"`);
  `get_paper_closed_trades` (newest-first, limited; `exit_price` /
  `realized_pnl` → `"uncomputable (KI-136)"`; `close_reason` best-effort
  from `events`); `get_paper_failed_entries`; `get_paper_engine_runs_summary`.
  All transform logic is in pure functions for testability.
- `dashboard/app.py` — `st.tabs(...)` gains `"Paper Trading"`; new
  `with tab_paper:` block: a 🟢/🟡/🔴 drift banner from a
  `@st.cache_data(ttl=60)` wrapper around `monitoring.paper_trading_drift.run()`
  (read-only, no Telegram); engine summary metrics; open-positions table;
  recent-closed table; rejected-entries expander. If the engine DB can't be
  opened the tab shows a single warning and the other tabs are unaffected.
  `trail_pct`/`activation_pct` read from `active_spec.json` (Phase-1B-D
  defaults 0.30 / 0.01 if absent).
- `.claude/local_scripts/test_dashboard_queries.py` — extended to exercise
  the four `get_paper_*` functions against the real engine DB (skips if the
  engine DB file isn't present).
- Docs: `OPERATIONS.md` (Paper Trading tab note — `CRYPTO_ENGINE_DB_PATH`,
  the "uncomputable" cells, smoke command); `ARCHITECTURE.md` (dashboard
  section). No new ADR (ADR-020 already covers the read-only engine-DB
  read); no new KI (the tab surfaces KI-136 / PRICE-SNAPSHOTS-001
  in-product).

**Tests.** `tests/dashboard/test_paper_trading_queries.py` — 11 unit tests,
all passing (synthetic engine DuckDB; cover live-state filtering, calc-stop
activated / not-activated / NULL-price, closed-trade ordering+limit+uncomputable,
`close_reason` from a `reconcile_action` event, failed-entries filter, the
engine-runs summary incl. empty case, `CRYPTO_ENGINE_DB_PATH` env path). Full
`tests/dashboard/` suite: 63 passed. `dashboard/app.py` py_compiles. Extended
dashboard-query smoke against the live engine DB: all paper-trading queries OK
(7 open positions, 15 closed, 6 rejected — `binance_rejection`).

**Pending operator action.** Review the branch → on approval I push + open PR
via `gh` → I then squash-merge + `git checkout master && git pull && git log -3`
→ operator restarts `mhde-streamlit` (no new systemd unit; ensure the unit's
`Environment=` carries `CRYPTO_ENGINE_DB_PATH` if the engine repo isn't at the
default path). After Gap 3: the engine-data-recording follow-up
(exit-price persistence / PRICE-SNAPSHOTS-001 / RECONCILE-001 — KI-136).

---

## 2026-05-11 — Gap 2: paper-trading drift monitor (liveness + hit-rate)

**Branch:** `gap2-paper-trading-drift-monitor` (committed; **STOPPED for
operator review before merge** — same handoff as Gap 1: PR opened via
`gh`, operator merges via GitHub UI).

**Trigger.** Three-gap observability plan
(`~/.claude/plans/operator-needs-three-interconnected-zazzy-brooks.md`),
Gap 2 — reworked in-session: the original plan's Gap 2 was a daily P&L /
win-rate / label-hit monitor; the operator narrowed it to liveness +
hit-rate only, because the engine's `daily_pnl` table is empty (its
reconcile timer is disabled pending engine-side RECONCILE-001) so the
P&L/DD/monthly arms would have been inert. Those arms deferred to KI-136
("Gap 2.5").

**Design doc.** `docs/superpowers/specs/2026-05-11-paper-trading-drift-monitor-design.md`
(commit `b5fa530`). Approved by operator before implementation.

**What shipped.**
- `monitoring/paper_trading_drift.py` — `run(engine_conn, mhde_conn, now)`
  + `main()`. Opens the engine DuckDB **read-only** via
  `CRYPTO_ENGINE_DB_PATH` (default
  `/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`) and
  MHDE's `crypto_ml_labels`. Four checks → one `MonitorResult` (worst
  severity wins) via `monitoring.alert.send_alert`:
  - **A. engine liveness** — newest `engine_runs[phase=monitor,success]`
    age > 5 min → warn / > 20 min → critical; after the 08:30 UTC cutoff,
    no successful `phase=entry` run today → warn. (`reconcile` arm gated
    off by `CHECK_RECONCILE=False` while the engine's reconcile timer is
    disabled.)
  - **B. stuck positions** — `entry_pending`/`exit_pending` older than
    10 min → warn / 30 min → critical (relaxed from the originally-spec'd
    5 min to match the 15-min monitor cadence).
  - **C. closed-trade win rate** (rolling 14d by exit timestamp;
    post-cost `net = (sell_vwap - entry_price)·qty - 0.0009·notional`):
    outside `[0.74, 0.99]` → warn, < 0.60 → critical. Excludes the
    RECONCILE-001 phantom `exit_filled`-with-NULL-`entry_price` rows.
    **Live-data finding:** the engine records market exits with
    `orders.price = NULL` and no price in the exit `order_filled` event,
    so there's no readable exit price — Check C ships but currently
    reports "uncomputable (KI-136)" and counts those trades under
    `closed_trade_no_exit_price`. It activates with no code change once
    the engine persists a readable realized exit P&L. (Also: the 14
    closed trades in the engine DB right now are all manual
    `manual_close_leverage_fix` closes, not strategy exits.)
  - **D. label hit rate** — closed positions joined to
    `crypto_ml_labels.label_10d_10pct`, windowed by *label settlement*
    (entry+10d ∈ last 14d): outside `[0.32, 0.62]` → warn, outside
    `[0.20, 0.75]` → critical.
  - C and D are **sample-gated**: < 20 qualifying trades → status stays
    OK, body notes "insufficient sample (N/20)".
- `main.py` — `monitor paper-trading-drift` subcommand.
- `systemd/mhde-monitor-paper-trading-drift.{service,timer}` —
  `OnCalendar=*:0/15`, `User=jpcg`,
  `Environment=CRYPTO_ENGINE_DB_PATH=…`, logs to
  `data/logs/monitor_paper_trading_drift.log`.
- Docs: `DECISIONS.md` ADR-020 (monitoring may read the engine DuckDB
  read-only — scoped exception to INTERFACE.md's no-DB-access rule, with
  the constraints that keep it from being real coupling); `KNOWN_ISSUES.md`
  KI-136 (deferred P&L/DD/monthly arms); `OPERATIONS.md` (monitor catalog
  row + interpretation runbook + deploy step); `ARCHITECTURE.md` (monitor
  table row).

**Tests.** `tests/monitoring/test_paper_trading_drift.py` — 23 unit
tests, all passing. Build synthetic engine + MHDE DuckDBs; cover the four
checks at each severity, the sample gate, phantom-position exclusion,
out-of-window exclusion, label-unsettled exclusion, the
`CRYPTO_ENGINE_DB_PATH` env-var path, severity aggregation, and `main()`
exit codes.

**Commits on branch (2 so far).**
1. `b5fa530` docs: Gap 2 design spec
2. `34dffcf` feat(monitoring): paper-trading drift monitor + CLI + systemd
(this docs commit follows.)

**Verification.** `pytest tests/monitoring/test_paper_trading_drift.py`
→ 23 passed. Live dry-run against the real engine DB:
`MONITORING_DRY_RUN=true venv/bin/python main.py monitor
paper-trading-drift` — see the session for the observed result. (Pre-existing,
unrelated: `tests/equity/test_monitoring.py` has 2 failures from `joblib`
not being installed in `.venv` — not touched by this branch.)

**Pending operator action.** Review the branch; merge the PR via GitHub
UI; deploy the new timer on the VPS (`OPERATIONS.md` § Deploying the
monitors — the `enable --now` list now includes
`mhde-monitor-paper-trading-drift.timer`); optionally add a one-line
back-reference to ADR-020 in the engine repo's `docs/INTERFACE.md` §1.
Then: Gap 3 (`gap3-paper-trading-dashboard-tab`).

---

## 2026-05-10 — Gap 1: crypto retrain validation gate (single-arm hit rate)

**Branch:** `gap1-model-retrain-validation-gate` (committed; pending
operator review).

**Trigger.** Three-gap observability plan
(`/home/jpcg/.claude/plans/operator-needs-three-interconnected-zazzy-brooks.md`).
Gap 1 closes the auto-promote risk: `crypto/ml/train.py` was
unconditionally flipping `is_active=true` on every retrain. Phase E
paper trading would have consumed today's two new models tomorrow.

**Final design (full journey in ADR-019).** Single-arm gate on label
hit rate (`precision_at_threshold` stored at training-time CV),
threshold new >= 0.9 * old. Originally specified as a two-arm gate
(hit rate + walkfold trade Sharpe). The Sharpe arm was dropped after
Task 1.3 spec review found that walkfold predictions are tagged with
per-fold model_ids (never the production model_id), making per-model
Sharpe queries non-functional. `crypto/ml/sharpe_sim.py` remains as
a utility module.

**Commits on branch (5).**
1. `2a666cd` feat(crypto/ml): add promotion_status column to model_runs
2. `70563ed` refactor(crypto/ml): extract walkfold trade Sharpe sim from local_scripts
3. `7eca751` feat(crypto/ml): retrain validation gate (initial two-arm)
4. `222345d` refactor(crypto/ml): drop Sharpe arm from validation gate
5. `b584e2a` feat(crypto/ml): gate is_active promotion on validation result

**Tests.** 4 schema migration tests, 6 sharpe_sim tests, 4 validation
gate unit tests, 4 retrain-promotion-gating integration-style tests.
Full crypto suite: 338 passed, 1 skipped.

**Pending operator action.** Review the branch and merge. Then run a
real `crypto retrain` and record the gate's `duration_sec` JSON log
field in a follow-up SESSION_LOG entry. Per the original plan: if
real-world duration exceeds 30 min for any horizon, propose async
refactor; currently the gate is a single SELECT so duration should be
sub-second — surprise duration would indicate something else has
slowed.

**Docs updated.** DECISIONS.md (ADR-019), OPERATIONS.md (Retrain
validation gate section), KNOWN_ISSUES.md (KI-135 resolved), this
entry.

---

## 2026-05-10 — KI-130 dashboard date-selector DuckDB DISTINCT+TopN bug

**Branch:** `dashboard-distinct-limit-bug` (committed; pushed; NOT
merged — pending operator approval).

**Trigger.** Investigation of three operator-reported findings: (1)
walk-fold predictions "stopped" May 8, (2) dashboard surfaced only
May 9 + May 10 crypto predictions despite 10+ days in the DB, (3)
no monitor caught either. Findings (1) and (3) turned out to be
expected behaviour — walk-fold is a one-shot Phase 1A backfill (not
a daily pipeline) and `monitoring/pipeline_execution.py` correctly
filters on `is_active=true` so walk-fold rows are intentionally
excluded. Only Finding (2) was a real bug.

**Root cause (Finding 2).** Both prediction-tab date dropdowns ran
`SELECT DISTINCT prediction_date FROM <table> ORDER BY
prediction_date DESC LIMIT 30`. Against the production DuckDB file
this returned 2 rows instead of 30. Bisected to a DuckDB 1.5.2
TopN-with-DISTINCT planner fusion that triggers data-volume
dependently: same query with `LIMIT 100` returns 100 rows; same
logical query with `GROUP BY` returns 30 rows. Bug does NOT
reproduce in fresh in-memory or file DBs even at 40k rows; only
manifests on the production DB's specific block layout.

**Fix.** New helper `get_distinct_prediction_dates(conn, table,
date_col, limit)` in `dashboard/services/queries.py` uses
`GROUP BY` + `ORDER BY` + `LIMIT` to avoid the broken planner path.
Both call sites in `dashboard/app.py` (equity tab at line 117,
crypto tab at line 387) switched to the helper. FX tab uses a
different shape (`MAX(datetime_utc)` and `WHERE datetime_utc = ?`)
so is unaffected; `dashboard/components/filters.py:23` is unaffected
because its GROUP-BY-shaped query selects two columns rather than
DISTINCT on a single sort key.

**Tests added (5).**
`tests/dashboard/test_distinct_date_selector_regression.py`:
- 4 behavioural contract tests verifying the helper returns every
  distinct date under `limit`, returns the most-recent N when the
  table exceeds `limit`, and collapses multi-row dates correctly.
- 1 source-level anti-pattern test that intercepts the SQL the
  helper actually executes (via a `_Capture` shim conn) and asserts
  it contains `GROUP BY` and not `DISTINCT`. The behaviour tests
  cannot reliably catch a regression to the broken pattern (bug
  doesn't reproduce in synthetic test data), so the source guard is
  the durable backstop. Confirmed it fires when the SQL is reverted
  to the buggy shape.

**Verification.** Local smoke script
`.claude/local_scripts/smoke_distinct_dates.py` (gitignored under
the `smoke_*` prefix) runs the helper against the production DB:
crypto returns 30 dates (previously 2 — fix confirmed), FX returns
30 datetimes, equity returns 6 (the production table genuinely has
only 6 distinct prediction dates — separate data fact, no
regression). Full test suite green (1263 passed, 1 skipped, 0
failed; 3m06s).

**Docs.** KNOWN_ISSUES.md: KI-130 added under "Recently resolved"
with the full repro detail and fix description; KI-131 added under
"Open" as a low-priority side-observation (crypto 5d production
model wrote 23 rows on 2026-05-09 vs ~30 expected — below the
50% monitor threshold so didn't fire; hypotheses listed for future
triage). A new "Walk-fold semantics — FAQ" callout at the top of
KNOWN_ISSUES.md surfaces that walk-fold is a one-shot Phase 1A
backfill (per `crypto/ml/backfill_walkforward.py:35` and KI-119),
not a daily pipeline — so future operators / chats won't repeat the
"walk-fold stopped writing" misread that prompted this session.

**Commits on branch:**
- `feat(dashboard): get_distinct_prediction_dates helper — DuckDB
  DISTINCT+TopN bug workaround (KI-130)`
- `test(dashboard): regression + anti-pattern tests for KI-130`
- `docs(known_issues): KI-130 resolved, KI-131 open, walk-fold FAQ`

**Pending operator action.** Review and merge `dashboard-distinct-
limit-bug`. No deployment beyond `git pull` on the VPS dashboard
host — Streamlit auto-reloads. Branch is pushed; not yet merged.

---

## 2026-05-10 — KI-128 weekday-aware recency for health_check + pipeline_execution

**Branch:** `ki128-weekday-aware-recency` (committed; not yet merged — pending operator approval).

**Problem.** `pipelines/health_check.py::_check_equity` failed Sun/Mon mornings (the literal `now - 1d` returned Sat or Sun, neither has equity data); `_check_fx`, `monitoring/pipeline_execution.py` (FX leg), and `pipelines/freshness.py::check_fx_freshness` failed through the entire forex weekend close (Fri 22:00 UTC → Sun 22:00 UTC). Result: predictable Telegram false alerts every weekend.

**Fix.** Added `pipelines/market_calendar.py` as a single source of truth for market-clock decisions. Four pure helpers:
- `trading_days_between(start, end)` — moved from `freshness.py`.
- `expected_equity_prediction_date(now)` — most recent Mon-Fri strictly before `now.date()`.
- `is_forex_closed(now)` — True iff Fri 22:00 UTC ≤ now < Sun 22:00 UTC.
- `fx_close_floor(now)` — Fri **21:00** UTC of the active closure (the last bar timestamp expected before close, since MHDE's `fx_prices_hourly.datetime_utc` stamps bars at hour-start).

Three callers gate their existing recency logic on these helpers. Equity / crypto branches in `pipeline_execution` are unchanged (75h / 27h budgets per ADR-015 already cover their domains). Holidays remain operator-acknowledged per ADR-015's precedent.

**Tests added (~30):** `tests/pipelines/test_market_calendar.py` (21), `tests/pipelines/test_health_check_weekend.py` (11 — 6 equity + 5 fx), `tests/regression/test_pipeline_execution_weekend.py` (4), plus 3 forex-closed cases appended to `tests/equity/test_pipeline_freshness.py`. The cross_artifact `_seed_minimal_health_data` helper was updated to be weekday-correct so `tests/equity/test_monitoring.py` stays green on any CI day.

**Notable design correction during execution.** The original spec had `fx_close_floor` returning Fri 22:00 UTC (the close moment). Task 4's tests caught a semantic error: with `latest >= floor` and floor=22:00, a healthy system shows stale because the bar covering 21:00–22:00 trading has `datetime_utc=21:00:00`. Fixed in commit `0a42f40` by returning Fri 21:00 UTC and renaming the constant `_LAST_FX_BAR_HOUR_UTC = 21` (kept `_FOREX_CLOSE_HOUR_UTC = 22` for `is_forex_closed`).

**Docs.** ADR-018 captures the decision and the bar-timestamp rationale (commit `44f8f59`). KI-128 → "Recently resolved" in `KNOWN_ISSUES.md`.

**Commits on branch (11):**
1. `0f15529` docs(specs): KI-128 weekday-aware recency design
2. `10d237a` docs(plans): KI-128 weekday-aware recency TDD plan
3. `ff1a7f5` feat(market_calendar): extract trading_days_between to shared module
4. `60cea99` feat(market_calendar): add expected_equity_prediction_date
5. `c6758c5` feat(market_calendar): add is_forex_closed and fx_close_floor
6. `bcff031` fix(freshness): forex-closed window aware FX freshness check (KI-128)
7. `0a42f40` fix(market_calendar): fx_close_floor returns last bar timestamp pre-close
8. `059d02e` fix(health_check): weekday-aware equity recency (KI-128)
9. `2cdd60c` fix(health_check): forex-closed window aware FX check (KI-128)
10. `3840a36` fix(monitor): forex-closed window aware FX recency (KI-128)
11. `44f8f59` docs(decisions,known_issues): ADR-018 + KI-128 -> resolved

**Verification (L5).** 107 tests passed, 3 failed (all pre-existing `test_smoke_test_*` / `test_active_model_paths_resolve` — missing `joblib`, unrelated to this branch). Health check CLI ran against production DB: PASSED, forex-closed branch active (`latest bar 2026-05-09 20:00:00 UTC (forex-closed; floor=2026-05-08 21:00:00)`).

**Pending operator action.** Review the branch and approve merge. Branch is pushed; not yet merged. Pre-existing `test_smoke_test_*` failures (missing `joblib`) are unrelated to this work and were present before the branch.

---

## 2026-05-10 — Engine-export contract: MHDE-side production code

**Branch:** `master`. Sixteen commits, full `tests/crypto/exports/`
suite green (42 passed + 1 skipped), production export files
produced, all docs updated.

**Trigger.** The crypto-trading-engine (separate repo at
`/home/jpcg/crypto-trading-engine/`) needs two inputs from MHDE for
Phase 2/3 paper trading: a strategy spec (rare updates, after Phase
1B re-runs) and a daily ranked predictions list. INTERFACE.md in the
engine repo documents the contract. This session built the MHDE-side
producers that emit those two files at `data/exports/`.

### What was completed

1. **Foundation modules** (`crypto/exports/`): `spec_config.py`
   (static fields + `PHASE1B_WINNER_RUN_ID` constant), `hashing.py`
   (`compute_spec_hash` byte-identical to engine reference, with
   cross-repo parity test reading a shared fixture from the engine
   repo), `_io.py` (atomic JSON write + atomic symlink replace).
   ~21 tests.
2. **Active spec writer** (`crypto/exports/write_active_spec.py`):
   reads Phase 1B winner row from `crypto_backtest_runs`, runs
   `report.simulate_portfolio` for portfolio-realistic metrics,
   reads `phase0_evaluate.evaluate_all` for verdict (lowercased).
   10 tests covering schema, hash self-consistency, missing-row
   error, dry-run.
3. **Daily predictions writer**
   (`crypto/exports/write_daily_predictions.py`): full-universe
   re-score (does NOT read filtered `crypto_ml_predictions`).
   Preflight: staleness-only (corrected from initial 100% coverage
   gate per KI-129). Atomic JSON write + symlink replace. 11 tests.
4. **CLI**: `crypto export-spec` and `crypto export-predictions`
   under the existing `crypto` Click group in `main.py`. Both with
   `--dry-run`; `export-predictions` also has `--date`. Exception
   types translate to `click.ClickException` for non-zero exit.
5. **Systemd timer**:
   `mhde-crypto-export-predictions.{service,timer}` — fires 06:15
   UTC daily, 7 days/week, 5h45m after `mhde-crypto-predict.timer`
   and 15 min before the engine's 06:30 UTC entry phase.
   `systemd-analyze verify` clean. Deployment to VPS is a separate
   operator action documented in OPERATIONS.md.
6. **Initial production run**: produced
   `data/exports/active_spec.json` (spec_hash
   `f4655cd46ff691267338fad765c2febc63021f35da191214e5350af4acf927e9`,
   Phase 1B winner `backtest_10d_D_top_n_a02e15a0`) and
   `predictions_2026-05-10.json` (n=48, model
   `crypto_10d_db171418`) plus the `predictions_latest.json`
   symlink. `data/exports/` gitignored.
7. **Doc updates**: CLAUDE.md read-first list grew from 9 to 10
   entries (added INTERFACE.md). DECISIONS.md gained ADR-017
   (engine-export contract). OPERATIONS.md gained an "Engine
   exports" runbook section. Spec at
   `docs/superpowers/specs/2026-05-10-mhde-engine-export-contract-design.md`
   and plan at
   `docs/superpowers/plans/2026-05-10-mhde-engine-export-contract.md`.

### In-flight corrections during the session

Two design bugs were caught by spec-review subagents and fixed
before the work landed in user-visible state:

1. **PortfolioResult unit transforms.** The spec said
   `result.max_drawdown_pct` was a percentage (`-23.7`) and
   prescribed `/100` to convert to fraction. Reading `report.py`
   showed it's actually a fraction (`(eq - peak) / peak`). The
   all-winner test seed produced `dd = 0`, masking the bug
   (`0 / 100 == 0`). Fix: passthrough on `max_drawdown_pct`,
   multiply by 100 on `annualized_return_pct` (which IS stored as
   a fraction but INTERFACE.md wants percentage form). Magnitude
   assertions in tests now catch regressions in either direction.
   Commit `2d018fb`. Spec/plan docs corrected in `9571784`.

2. **Preflight 100% coverage gate over-strict (KI-129).** The
   initial preflight refused to emit a partial 48/50 file when
   BSBUSDT/PRLUSDT had no features. Investigation showed those
   symbols are in their 60-day features warmup window
   (`compute_features` requires 60 days for `return_60d`); the
   pipeline correctly refuses to compute features for them. Fix:
   keep the staleness gate, drop the per-symbol coverage check.
   `n_predictions` reflects the predictable subset. Commit
   `ef0f12a` + spec/KI updates in `8eb8724`.

### Verification (L5)

- `tests/crypto/exports/`: 42 passed + 1 skipped (cross-repo parity
  test, expected — engine fixture not yet created on the engine
  side).
- Production export ran end-to-end: `active_spec.json` (1334 bytes,
  hash self-consistent) + `predictions_2026-05-10.json` (7145
  bytes, ranks 1..48 consecutive, all probabilities in [0, 1]).
- Symlink `predictions_latest.json` resolves to today's dated file.
- Pre-commit hook (5-file pytest smoke) green on every commit.

### KIs

- **KI-128** opened (carried from prior dirty working tree) — health
  check thresholds don't account for weekend market closure.
  Cosmetic; operator ignores weekend alerts.
- **KI-129** opened + resolved same session — engine-export preflight
  conflated stale pipeline with warmup-window symbols. Fix: loosened
  to staleness-only.
- Open observations: KI-122, KI-123, KI-126, KI-128.

### Files of record

- `crypto/exports/` (new module: `__init__.py`, `_io.py`, `hashing.py`,
  `spec_config.py`, `write_active_spec.py`, `write_daily_predictions.py`).
- `tests/crypto/exports/` (new test directory: 5 test files, 43 tests).
- `main.py` (added 2 Click commands).
- `systemd/mhde-crypto-export-predictions.{service,timer}`.
- `data/exports/` (operational artifacts, gitignored).
- `CLAUDE.md`, `DECISIONS.md`, `KNOWN_ISSUES.md`, `OPERATIONS.md`
  (read-first list extension, ADR-017, KI-128 + KI-129 lifecycle,
  runbook section).

### Pending operator deploy

```bash
sudo cp systemd/mhde-crypto-export-predictions.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mhde-crypto-export-predictions.timer
```

Until deployed, the daily timer doesn't fire on the VPS; the export
file already in `data/exports/` from this session's manual run is
correct for 2026-05-10. Operator can also re-run via
`venv/bin/python main.py crypto export-predictions` at any time.

### Pending engine-side coordination

The cross-repo hash parity test in MHDE is currently SKIPPED. To
activate it, the engine repo needs three coordinated changes
(out of scope for this MHDE-side session):

1. Create `crypto-trading-engine/tests/fixtures/specs/hash_test_vectors_v1.json`
   with 3+ vectors per the format documented in the spec.
2. Update `crypto-trading-engine/tests/unit/spec/test_hash.py` to
   read the fixture and assert per-vector hash equality.
3. Add INTERFACE.md §2.4 documenting the fixture path.

Once those land in the engine repo, MHDE's parity test
(`tests/crypto/exports/test_hashing.py::test_cross_repo_parity_with_engine_fixture`)
will activate automatically — no MHDE-side change needed.

---

## 2026-05-09 — Phase 0 evaluation infrastructure

**Branch:** `phase0-evaluation-infrastructure` off `master`. Six
commits, full test suite green.

**Trigger.** Earlier the same day the operator asked whether Phase 0
calibration validation was automated. Audit showed only partial
coverage via `monitoring/model_performance.py` (one-sided 7-day
precision check; no lift, no calibration buckets, no 200-sample
gate). This session built the missing infrastructure so weekly drift
surfaces before week 6 instead of waiting for the formal date.

### What was completed

1. `feat(crypto/ml): phase0_evaluate` — pure functions for the four
   Phase 0 criteria + reliability diagram + sample-accumulation
   projection. ``EngineConfig`` abstraction (CRYPTO wired today;
   equity/FX = one-config-block extension). 25 tests.
2. `feat(crypto/ml): phase0_report` — Markdown go/no-go renderer
   with PASS/FAIL/INTERIM verdict, criterion table, per-criterion
   detail, ASCII reliability diagram, sample accumulation block. 12 tests.
3. `feat(monitoring): phase0_calibration` weekly monitor +
   `phase0_milestones` schema. Three alert paths: drift (tighter
   than formal gates), sample-rate slowdown (week-over-week ETA
   slip > 7d), idempotent one-shot 200-reached notification. 6 tests.
4. `feat(systemd): mhde-monitor-phase0-calibration` unit. Sundays
   06:00 UTC, system-level, User=jpcg. The 10th monitor in the stack.
5. `feat(main): crypto phase0-report` + `monitor phase0-calibration`
   CLI bindings. `--model-id`, `--out` (with `-` for stdout-only).
6. Docs: KI-126 opened (definition (b) week-over-week relative drift
   detection deferred until snapshots accumulate). PATH_TO_LIVE_PLAN
   Phase 0 section references the new tooling. OPERATIONS monitor
   catalog grew from 6 to 10; new Phase 0 runbook section.

### Verification (L5)

Verification commands run during the session:
- Full test suite green (1202 + 43 new tests = 1245).
- `crypto phase0-report` against production DB rendered cleanly for
  both active crypto models in INTERIM mode (32 and 57 filled, well
  below 200-sample gate).
- `monitor phase0-calibration` against production with
  `MONITORING_DRY_RUN=true` returned ok (no false-positive alerts).
- Pre-commit hook: clean (~2-3s, 27 smoke tests).

### KIs

- KI-126 opened — week-over-week relative drift detection deferred.
- Open observations now: KI-122, KI-123, KI-126.

### Pending operator deploy

```bash
sudo cp systemd/mhde-monitor-phase0-calibration.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mhde-monitor-phase0-calibration.timer
```

Until deployed, the weekly run only fires via manual CLI; CLI
report and underlying evaluators are live in master.

---

## 2026-05-09 — Monitoring-gaps session: close the L4↔L5 gap

**Branch:** `monitoring-gaps-session` off `master @ aa5c53c`. Five
commits, full test suite (990 tests, +14 new) green, four monitors
verified ok against production.

**Trigger.** Earlier the same day, an equity dashboard maturity-date
fix passed every code-side check but the user's CSV was still empty
because Streamlit had been running stale code for 18 hours. Every
existing layer-monitor was green at the time. This session adds
monitors that catch user-experience failures, not just internal-layer
failures.

### What was completed

Five commits, each landing one item:

1. **Trust-ladder docs** (`7f7eca5`).
   ADR-016 codifies a six-level trust ladder (L0 code committed →
   L5 user-visible artifact matches expectation). HARDENING_PLAN
   universal exit criteria gain an explicit L5 verification bullet.
   OPERATIONS gets a new "Trust ladder" section with verification
   commands per level, plus a "Streamlit does NOT auto-reload"
   subsection under "Restarting after a code change" pointing at
   the new monitor.

2. **dashboard_consistency strengthened** (`967eb69`).
   Per-engine × per-horizon column-completeness checks. Asserts
   `price_at_prediction`, `maturity_date`, `current_price` populated
   for every row; `price_at_maturity` populated for filled and NULL
   for pending; realized columns populated for filled; `pct_move_str`
   non-empty/parseable (the format helper, when called, returns
   non-empty — "+0.00%" is a valid render). Five new tests.

3. **streamlit_freshness — new** (`a6811b8`).
   Compares `systemctl --user show mhde-streamlit -p
   ActiveEnterTimestamp --value` against `git log -1 --format=%ct
   master`. Warns if process predates latest commit by > 4h.
   Hourly system-level timer at :35. Four tests including the May 9
   incident shape.

4. **dashboard_synthetic — new** (`7521c5d`).
   Hourly E2E probe: HTTP GET on `/_stcore/health` (catches
   "Streamlit unreachable") plus calls each `get_*_predictions`
   helper (catches "helper raised" + "key column all-NULL").
   Three tests.

5. **cross_artifact — new** (`d5e5821`).
   Daily 06:30 UTC. Re-runs the health-check internals, parses the
   detail strings via regex, independently re-queries the DB for
   the same facts, alerts on disagreement. Plus verifies the
   assembled Telegram message contains each detail string. Catches
   the formatter-typo / dropped-section class of bug. Three tests.

### Verification

- `make test` (full suite): **990 passed in 228.47s** (was 976; +14
  new tests in `tests/equity/test_monitoring.py`).
- All four monitors smoke-tested end-to-end against the production
  DB / running services with `MONITORING_DRY_RUN=true`:
  - dashboard_consistency: status=ok across 3 engines × their
    horizons (equity 5d/10d/20d, crypto 5d/10d, fx 24h/48h).
  - streamlit_freshness: status=ok (Streamlit was restarted at
    09:26 UTC; latest commit ~10:00 UTC; lag well under 4h).
  - dashboard_synthetic: status=ok (HTTP 200 from
    `/_stcore/health`, all three helpers return non-empty).
  - cross_artifact: status=ok (all three engine details match DB).
- `bash scripts/pre-commit.sh`: 27 tests in 2-3s including KI-118
  regression.

### Files changed

New monitors:
- `monitoring/streamlit_freshness.py`
- `monitoring/dashboard_synthetic.py`
- `monitoring/cross_artifact.py`
Extended:
- `monitoring/dashboard_consistency.py`
New systemd units (added to `systemd/`, NOT yet deployed to
`/etc/systemd/system/` — operator step):
- `mhde-monitor-streamlit-freshness.{service,timer}` — hourly :35
- `mhde-monitor-dashboard-synthetic.{service,timer}` — hourly :40
- `mhde-monitor-cross-artifact.{service,timer}` — daily 06:30
Docs:
- `DECISIONS.md` ADR-016
- `HARDENING_PLAN.md` universal exit criteria
- `OPERATIONS.md` trust ladder + streamlit-restart subsection
CLI:
- `main.py` — three new `monitor <name>` subcommands
Tests:
- `tests/equity/test_monitoring.py` — 14 new test cases

### KIs

No new KIs surfaced — all four monitors reported `ok` against
production on first run. The pre-existing open-list (KI-119,
KI-122, KI-123) is unchanged.

### Pending (operator action — not blocking the merge)

Deploy the three new systemd timers:

```
sudo cp systemd/mhde-monitor-streamlit-freshness.{service,timer} /etc/systemd/system/
sudo cp systemd/mhde-monitor-dashboard-synthetic.{service,timer} /etc/systemd/system/
sudo cp systemd/mhde-monitor-cross-artifact.{service,timer}      /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
    mhde-monitor-streamlit-freshness.timer \
    mhde-monitor-dashboard-synthetic.timer \
    mhde-monitor-cross-artifact.timer
```

Until deployed, the monitors run only via the manual CLI; the
fixes themselves (extended dashboard_consistency, three new
monitors) ARE active in code.

### Branch status

`monitoring-gaps-session` ready to merge to master `--no-ff`.

---

## 2026-05-09 — KI-124 fix: equity recency budget

**Branch:** `fix-ki124-equity-recency-budget` off
`master @ 0e372e6`. Single targeted fix; one commit.

**Trigger.** KI-124 was opened earlier the same day during
KI-120 verification: monitor count side green for equity but
recency_ok=False because the 27h `RECENCY_BUDGET["equity"]`
couldn't accommodate equity's T-1 scoring (`prediction_date =
yesterday`) plus the Friday→Monday weekend roll (latest
`prediction_date` stays at Friday for ~72h).

### What changed

- `monitoring/pipeline_execution.py` —
  `RECENCY_BUDGET["equity"]` raised from 27h to 75h (72h weekend
  roll + 3h grace). Crypto stays at 27h (24/7 trading, no
  weekend gap); FX stays at 2h. Inline comments document why
  the three budgets are asymmetric. Module docstring schedule
  line corrected from "21:00 UTC" to "00:15 UTC, scores T-1"
  per KI-101.
- `DECISIONS.md` — ADR-015 documents the asymmetric per-engine
  recency budgets with explicit holiday-extended-weekend
  trade-off (deliberately not covered to keep the monitor
  responsive to real outages). Future option to land
  `row_inserted_at TIMESTAMP` and tighten all budgets is noted
  as deferred.

### Verification

- `pipeline_execution.run()` against production DB:
  ```
  [equity] recency_ok=True count_ok=True  latest=2026-05-08
           n_latest=43  n_avg=50.0  ratio=0.86
  [crypto] recency_ok=True count_ok=True  latest=2026-05-09
  [fx]     recency_ok=True count_ok=True  latest=2026-05-09 08:00
  ```
- `tests/equity/test_monitoring.py` + `tests/regression/`:
  **38 passed**, no regressions.

### KI status

- KI-124 → resolved.
- Open observations now: KI-119, KI-122, KI-123 (down from 4).

### Branch status

`fix-ki124-equity-recency-budget` ready to merge to master.
Single commit, narrow scope.

---

## 2026-05-09 — Equity ingestion fix: KI-120 resolved

**Branch:** `fix-equity-ingestion-degradation` off
`master @ aeefb36`. Six commits, full test suite (973 tests, +7
new) green, merge plan: `--no-ff` to master.

**Trigger.** KI-120 triage from earlier the same day pointed at
upstream Polygon ingestion as the root cause of equity prediction
volume thinning (May 5-8 saw 47-524 tickers/day vs 634 baseline).
The triage suggested investigation order; this session executed
the fix.

### Root cause (confirmed)

`ingestion/ingest_prices.py` looped per-ticker against
`/v2/aggs/ticker/{ticker}/range/1/day/...` once per universe
ticker. With ~520 active tickers and Polygon's free-tier 5
req/min limit, the run took ~50 minutes and rate-limited heavily.
Most days only 50-200 of 520 tickers actually got bars inserted.
The thinning then propagated linearly through `ml_features` and
`ml_predictions`.

### What was completed

Six commits, each landing one item:

1. **Doc upfront** (`7d09938`).
   KI-122 (universe builder leaks stale extended-tier rows) and
   KI-123 (misleading "Dev mode" log line) opened. ADR-014 added
   documenting that `max_symbols=520` is S&P 500 + named extras +
   ~16 SEC-filtered fillers — deliberate scope, not a Polygon-cost
   workaround. Authoritative source remains `config/universe.yaml`.

2. **Polygon ingestor refactor + tests** (`473b92a`).
   Switched primary path to the grouped-daily endpoint. Added
   `ingest_dates(conn, run_id, dates, tickers)` for explicit-date
   use (backfill). Per-ticker fallback retained but bounded
   (`DEFAULT_FALLBACK_LIMIT=10` per date). Throttling between
   consecutive calls (`DEFAULT_THROTTLE_S=13`) plus a 65s retry
   after 429 keeps the run inside free-tier budget.
   `tests/equity/test_ingest_prices.py` covers grouped filter,
   non-trading-day, fallback firing/cap, idempotency, missing key,
   default lookback.

3. **Backfill script** (added under `.claude/local_scripts/`,
   covered by the `.claude/local_scripts/*` gitignore prefix from
   the recovery audit). `equity_backfill_prices.py` re-fetches
   prices for explicit `TARGET_DATES`. Idempotent.

4. **Backfill executed against production DB.** Per-day rows
   inserted for May 5-8; idempotent inserts via `prices_daily` PK +
   `ON CONFLICT DO NOTHING`. Followed by `ml backfill-features` (one
   pass over all dates) and `ml predict --date <D> --skip-outcomes`
   for each of May 5-8 to write the missing predictions.

5. **Verification.** Post-fix data state:

   | trade_date | prices_daily | ml_features | ml_predictions |
   |---|---|---|---|
   | 2026-05-05 | 82 → **520** | 42 → **312** | 24 → **43** |
   | 2026-05-06 | 47 → **519** | 24 → **312** | 0  → **41** |
   | 2026-05-07 | 463 → **514** | 282 → **311** | 43 → **45** |
   | 2026-05-08 | 53 → **514** | 29 → **311** | 10 → **37** |

   pipeline_execution monitor against production DB: equity
   `count_ok=True` with `n_latest=43, n_avg=50.0, ratio=0.86`
   (above the 50% threshold). The recency side still flags —
   that's a pre-existing, unrelated monitor bug (KI-124, see
   below).
   smoke_test monitor: ok.
   `make test`: **973 passed in 227.06s** (was 966 before this
   session; +7 from `test_ingest_prices.py`).

6. **Doc finalization** (this entry + KNOWN_ISSUES + OPERATIONS).
   KI-120 moved to "Recently resolved" with full root cause and
   fix description. KI-124 opened. OPERATIONS.md Polygon section
   rewritten to document the grouped-daily architecture and call
   budget; backfill recipe pointed at the new script.

### KI-124 opened (out of scope, surfaced during verification)

`monitoring/pipeline_execution.py` derives recency from
`MAX(prediction_date)` treated as midnight UTC of that date. For
equity, which fires daily at 00:15 UTC and writes
`prediction_date = T-1`, that means the row is "47h old by the
midnight rule" by 23:00 UTC each day, far over the 27h budget.
Two suggested fixes captured in the KI: raise the budget to ~75h
(covers weekend gap) or add `row_inserted_at TIMESTAMP` and key
the recency check off the actual write time.

### Files changed

- `ingestion/ingest_prices.py` — refactored.
- `tests/equity/test_ingest_prices.py` — new, 7 tests.
- `KNOWN_ISSUES.md` — KI-120 resolved; KI-122/123/124 opened.
- `DECISIONS.md` — ADR-014.
- `OPERATIONS.md` — Polygon section rewritten.
- `.claude/local_scripts/{equity_backfill_prices,equity_post_backfill_state,run_smoke_monitor,probe_polygon_tier,probe_universe_composition,equity_volume_diagnostic}.py`
  — diagnostic + backfill scripts kept as session artifacts
  (under existing gitignore prefix-glob).

### Pending

1. **KI-122** — universe builder reconciliation for extended tier.
   Cosmetic (174 stale rows don't reach features). Future cleanup.
2. **KI-123** — daily_radar.py:83 "Dev mode" log line. Trivial
   one-liner; future cleanup.
3. **KI-124** — pipeline_execution recency budget for equity's
   T-1 schedule. Pick one of the two fixes captured in the KI;
   future monitor-tuning session.
4. **Operator items still deferred from the 2026-05-08 recovery
   audit** (unchanged): 6 modified `data/processed/*` files,
   3 untracked `docs/` files, and the FX migration mirror-table
   cleanup around 2026-05-15.

### Branch status

`fix-equity-ingestion-degradation` is ready to merge to master
with `--no-ff`. The six commits are independent enough that the
operator could cherry-pick (e.g. land just the ingestor refactor
without the docs) but they form a coherent unit.

---

## 2026-05-09 — Discipline session: monitor false-positive + tracking gates

**Branch:** `discipline-session-monitor-and-tracking` off
`master @ 52bd655`. Six commits, full test suite (966 tests) green,
merge plan: `--no-ff` to master with the issues opened in
`KNOWN_ISSUES.md` carried as deliberate follow-ups.

**Trigger.** The `pipeline_execution` monitor fired a `warn` on
crypto for two consecutive days (May 8 / May 9: ratios 0.30 / 0.24
vs the 50% threshold). Investigation showed the alert was a false
positive: the 14-day rolling baseline was contaminated by Phase
1A/1B walk-forward backtest rows that share the
`crypto_ml_predictions` table with production scoring. The
investigation surfaced two related discipline gaps from the prior
recovery audit (KI-118, the cross-chat coordination protocol)
which the operator scoped into one session.

### What was completed

Six items, each landed as its own commit on the branch:

1. **KI-118 regression test** (`cfb67ae`).
   `tests/regression/test_no_untracked_production_imports.py` walks
   every tracked `.py` outside `tests/`, `legacy/`,
   `.claude/local_scripts/`, `venv/`, `.venv/` and resolves every
   import to a path; if the path is inside the repo, it must be in
   `git ls-files`. Plus: every `.service`/`.timer` under `systemd/`
   must be tracked. Plus (when on the production host): every
   deployed `mhde-*` unit's source in `systemd/` must be tracked.
   Wired into `scripts/pre-commit.sh` smoke list. Verified
   fail-then-pass with a canary (a tracked importer of an untracked
   target produces a clear failure message).

2. **HARDENING_PLAN.md exit criteria** (`5a63c62`).
   Added a "Lesson from KI-118" paragraph near the top documenting
   the process gap. Added a "Universal exit criteria (every
   session)" block before the per-session breakdowns: clean `git
   status`, the new regression test passes, full `tests/regression/`
   green, SESSION_LOG + KNOWN_ISSUES updated. These apply on top of
   each session's specific exit criteria.

3. **CLAUDE.md cross-chat protocol** (`bc171d1`).
   New "Cross-chat protocol" section. Before substantial work,
   search SESSION_LOG.md for related ongoing workstreams; check
   branch state if found; ask the user when overlap is uncertain.
   The chat that starts substantial work owns updating SESSION_LOG
   before ending — even with a "Pending" section if unfinished.
   Motivated by the Phase 1A/1B / cutover-session collision that
   produced KI-119.

4. **pipeline_execution monitor false-positive fix** (`72040cd`).
   `monitoring/pipeline_execution.py:_check_engine_pipeline` now
   JOINs each predictions table with the corresponding
   `*_model_runs` table `WHERE is_active=true`. Both the latest
   count (`n_latest`) and the 14-day rolling baseline (`n_avg`)
   filter on the active set. Verified against the production DB:
   crypto's ratio went from 0.24 (warn) to 0.78 (ok) using the
   exact same data underneath — proving the prior alert was the
   baseline's fault, not a real volume drop. FX stays ok. Equity
   surfaces a separate flag (`n_latest=10` vs `n_avg=27.5`) which
   was previously masked by the same baseline contamination —
   tracked as KI-120 for separate triage.

5. **Monitor baseline composition regression test** (`86e4517`).
   `tests/regression/test_pipeline_execution_baseline.py` —
   seeds `crypto_ml_predictions` with 30 rows/day from an
   `is_active=true` model and 96 rows/day from an `is_active=false`
   model across the last 15 days. Asserts the monitor sees
   `n_latest=30` and `n_avg=30` (active-only on both sides). If
   anyone drops the `WHERE m.is_active=true` clause from either
   query, the test fails with a clear pointer to which side broke.
   Partner check asserts the monitor flags an engine whose
   `*_model_runs` has no `is_active=true` rows. Verified
   fail-then-pass.

6. **KNOWN_ISSUES.md updates** (`0a9fbe6`).
   KI-118 marked fully resolved (regression test now in place;
   the "owed" caveat removed). KI-119 opened — Phase 1A/1B
   walkfold backfill writes into `crypto_ml_predictions` without
   a matching `crypto_ml_model_runs` row, leaving downstream
   consumers unable to distinguish backtest from production.
   Reinforcement of writer isolation owed when
   `crypto-phase-1a-1b-backtest` is next reviewed; out of scope
   for this session. KI-120 opened — equity engine flag from the
   monitor verification (10 vs 27.5 baseline); three candidate
   interpretations listed; operator triage owed.

### Verification

- `make test` (full suite, no skips): **966 passed in 222.20s.**
- `bash scripts/pre-commit.sh`: 27 tests pass in 2s including the
  new KI-118 regression test.
- Production-DB monitor run via
  `.claude/local_scripts/verify_pipeline_monitor_after_fix.py`:
  crypto ok (ratio 0.78), fx ok (4/4 ratio 1.0), equity warn (10
  vs 27.5 — see KI-120).
- KI-118 regression test fail-then-pass demonstrated by injecting a
  canary: `pipelines/_ki118_canary_importer.py` (tracked) importing
  `pipelines/_ki118_canary_target.py` (untracked) → clear failure;
  cleaned up → pass.
- KI-119 (baseline-composition) test fail-then-pass demonstrated by
  commenting out the `WHERE m.is_active=true` clause: test reports
  "Got 126, expected 30"; restored → pass.

### Files changed

- `tests/regression/test_no_untracked_production_imports.py` (new)
- `tests/regression/test_pipeline_execution_baseline.py` (new)
- `tests/equity/test_monitoring.py` — `test_pipeline_execution_ok_when_fresh`
  now seeds `is_active=true` rows in each engine's `*_model_runs`
  table to match the monitor's new contract.
- `monitoring/pipeline_execution.py` — `_check_engine_pipeline` gains
  a `model_runs_table` parameter; all three queries filter on
  `m.is_active=true`.
- `scripts/pre-commit.sh` — regression test added to smoke list.
- `HARDENING_PLAN.md` — KI-118 lesson + universal exit criteria.
- `CLAUDE.md` — cross-chat protocol section.
- `KNOWN_ISSUES.md` — KI-118 resolution + KI-119 + KI-120.
- `.claude/local_scripts/crypto_volume_diagnostic.py`,
  `crypto_who_wrote.py`, `verify_pipeline_monitor_after_fix.py` —
  diagnostic scripts kept as session artifacts (already covered by
  the `.claude/local_scripts/*` gitignore prefix-glob from the
  recovery audit).

### Pending

1. **KI-119 reinforcement.** When `crypto-phase-1a-1b-backtest` is
   reviewed for merge, audit every writer that touches a
   `*_ml_predictions` table to confirm it registers a matching
   `*_ml_model_runs` row first (with `is_active=false`). Consider
   adding a regression test asserting "every distinct `model_id` in
   `*_ml_predictions` has a row in `*_ml_model_runs`".
2. **KI-120 triage.** Investigate the equity engine's
   `n_latest=10 vs n_avg=27.5` flag. Most likely path:
   `.claude/local_scripts/equity_volume_diagnostic.py` patterned on
   the crypto diagnostic from this session.
3. **Operator items still deferred from the 2026-05-08 recovery
   audit** (unchanged this session): 6 modified
   `data/processed/*.{jsonl,csv,md}` files (gitignore vs commit),
   3 untracked `docs/` files (tracked vs scratch), and the
   one-week stability buffer cleanup of FX migration mirror tables
   around 2026-05-15.

### Branch status

`discipline-session-monitor-and-tracking` is ready to merge to
master with `--no-ff`. The six commits are independent enough that
the operator could cherry-pick (e.g. land Item 1 alone) but they
were authored as a coherent unit and should land together.

---

## 2026-05-08 — Session 2 (FX migration): cutover Dukascopy → TwelveData

**Branch:** `fx-twelvedata-migration` (with master merged in earlier the
same day via the recovery sync).

ADR-013 cutover landed. `fx_prices_hourly` is now fed by TwelveData
through `fx/data/refresh.py` → `fx/data/refresh_twelvedata.py`. ATSRP
subprocess path retired from the data flow.

### What replaced the original 24h gate

The originally-planned 24-hour parallel-collection gate
(`fx compare-sources --hours 24 --threshold-pips 5`) was replaced by a
30-day historical backfill comparison built today, because the audit
work earlier in the day disrupted live parallel collection between 14:00
and 22:00 UTC and a same-day cutover decision was preferable to waiting
5 more days.

### Findings that drove the go decision

- **Coverage** (the headline). TwelveData covered 720/720 hourly bars
  over 30 days. Dukascopy was missing 240 (33%). Coverage gain alone
  justified the migration before considering price agreement.
- **Price agreement.** 472 of 480 matched bars within 5 pips on close
  (98.3%). The 8 breaches:
  - All occur at hour 20:00 or 21:00 UTC (NYSE close window).
  - All have Dukascopy > TwelveData by 5-7 pips (consistent sign).
  - Zero correspond to scheduled macro releases (NFP, FOMC, ECB, etc.).
  - Pattern is a known post-NYSE-close liquidity-venue rotation, not
    random source disagreement. Bounded, explainable, acceptable.
- **Weekend bars are real OTC.** 192 weekend bars sampled; 0/192 had
  collapsed OHLC; mean close-open 2.84 pips, mean high-low 8.20 pips.
  Saturday 03:00 / 12:00 UTC samples across 4 weekends all showed
  genuine ranges. No filtering needed downstream.

### Code changes

- `fx/data/refresh.py` rewritten as a thin wrapper that calls
  `fx.data.refresh_twelvedata.refresh_prices(conn, table="fx_prices_hourly")`.
  Pre-cutover ATSRP/Dukascopy subprocess implementation deleted.
- `fx/data/refresh_twelvedata.py` `upsert_new_bars` and `refresh_prices`
  gained a `table=` parameter (default unchanged → backwards-compat).
- `fx/data/compare_sources.py` `compare_recent` gained
  `dukascopy_table` / `twelvedata_table` kwargs (also backwards-compat)
  so the 30-day comparison could run against the backfill table.
- 240 historical bars copied from `fx_prices_hourly_twelvedata_backfill`
  into `fx_prices_hourly`. Row count: 71,038 → 71,278.
- `systemd/mhde-fx-predict.service`: removed the parallel
  `fx refresh-prices-twelvedata` ExecStart. Single `fx refresh-prices`
  ExecStart now uses TwelveData.
- Docs updated: ADR-013 → IMPLEMENTED, OPERATIONS.md FX section,
  ARCHITECTURE.md ATSRP-dependency section, INFRASTRUCTURE.md FX row.

### Diagnostic scripts kept under `.claude/local_scripts/`

`fx_backfill_twelvedata_30d.py`, `fx_compare_30d.py`,
`fx_check_twelvedata_weekend_bars.py`, `fx_cutover_premortem.py`,
`fx_cutover_backfill_gaps.py`.

### Verification

- `tests/fx/` (32 tests), `tests/integration/test_fx_pipeline.py`,
  `tests/dashboard/` (44 tests) all pass — 97/97 collected.
- Manual `venv/bin/python main.py fx refresh-prices` post-rewrite
  fetched the 22:00 UTC bar from TwelveData and inserted into
  `fx_prices_hourly` cleanly.

### Pending (operator action)

1. **Deploy the systemd unit change** to take effect on next firings:
   ```
   sudo cp /home/jpcg/MHDE/systemd/mhde-fx-predict.service \
           /etc/systemd/system/mhde-fx-predict.service
   sudo systemctl daemon-reload
   ```
   Until done, the deployed unit still calls the parallel
   `fx refresh-prices-twelvedata` ExecStart. Both fetchers run, both
   succeed, data is duplicated into the mirror table harmlessly.
2. **One-week stability buffer** until ~2026-05-15, then drop:
   - Tables `fx_prices_hourly_twelvedata`, `fx_prices_hourly_twelvedata_backfill`
   - CLI subcommands `fx refresh-prices-twelvedata`, `fx compare-sources`
   - Code: `fx/data/compare_sources.py`, `tests/fx/test_compare_sources.py`
   - Schema: `SCHEMA_FX_PRICES_HOURLY_TWELVEDATA` from `fx/schema.py`

### Branch status

`fx-twelvedata-migration` is ready to merge to master. The original
parallel-fetcher infrastructure plus today's cutover (writer flip,
240-bar backfill, doc updates) make a coherent landing.

---

## 2026-05-08 — Recovery audit: production files never tracked

**Not a `HARDENING_PLAN.md` session.** Triggered by an operator noticing
that `git status` on `master` showed files that ought to have been
committed during Sessions 0-7 still listed as `??` Untracked.

### Investigation

`git log --all -- <path>` returned **empty** for ten files that the
deployed system actively depends on. The reflog was clean — no reset,
no destructive op, no `.gitignore` change that would mask anything.
Conclusion: these files have been living on disk only since the
pre-rebuild "checkpoint" commit (`7b46c50`, before Session 0). The
hardening sessions never re-checked that the working tree was free of
untracked load-bearing source.

### Files committed (`fc6fc28` on master)

| Path | Caller / unit |
|---|---|
| `fx/bot/__init__.py`, `fx/bot/telegram_bot.py` | `fx/ml/signals.py:53`, `main.py:2151`, `monitoring/alert.py:82`, integration tests; `mhde-fx-bot.service` (system, `Restart=always`) |
| `fx/data/refresh.py` | `main.py:2010`; first `ExecStart` of `mhde-fx-predict.service` (hourly :05) |
| `pipelines/freshness.py` | All 3 prediction pipelines (`crypto`, `fx`, `ml`), `dashboard/app.py`, `main.py`, regression tests |
| `pipelines/health_check.py` | `main.py:2211`; user-level `mhde-health-check.service` (daily 06:00) |
| `systemd/mhde-fx-bot.service` | Installed in `/etc/systemd/system/`, `enabled` |
| `systemd/mhde-predict.{service,timer}` | Pre-suffix legacy names for the equity engine; both `enabled`, daily 00:15 / Sun 21:30 |
| `systemd/mhde-retrain.{service,timer}` | Same as above (retrain) |

### Other recovery actions

- **Phase 1A/1B crypto backtest WIP** isolated onto branch
  `crypto-phase-1a-1b-backtest` (commit `6db5674`, off `master @ cab91b8`).
  21 files, ~8.4k LOC. Includes walk-forward backfill (`crypto/ml/`) and
  the execution-backtest harness (`crypto/execution/backtest/`). All
  new model_runs rows insert with `is_active=false`; live predict
  pipeline isolated.
- **`.gitignore` extended** (`0623307` on master) to silence the ~60
  untracked diagnostic scripts under `.claude/local_scripts/` plus
  dated outputs. Already-tracked diagnostics (`audit_mhde_status.py`,
  `test_dashboard_queries.py`, `test_duckdb_failed_alter.py`, the four
  `outputs/daily_radar_2026-05-0{1..4}.{json,md}` snapshots, and
  `outputs/2026-05-04/`) deliberately preserved as tracked history.

### What's still untracked (deferred)

- 6 modified `data/processed/*.{jsonl,csv,md}` files — pipeline output
  churn from earlier runs. Operator chose to leave them; they're not
  noise from this session.
- 3 documents in `docs/` (codebase inventory + 2 sector-ETF planning
  notes). Operator wants to read first before deciding tracked vs
  scratch.

### Bugs found and recorded

- **KI-118** (added to `legacy/RESOLVED_ISSUES_ARCHIVE.md`) —
  production source files lived in the working tree without ever being
  `git add`-ed. Resolved by `fc6fc28`. **Regression test owed**: the
  Session 7 hardening exit-criteria didn't include a "no untracked
  load-bearing source" check, and none of the existing regression
  tests would catch a future recurrence. See KI-118 for proposed
  test design.

### Pending

- Write the regression test described in KI-118.
- Operator decides on the 3 deferred `docs/` files.
- Operator decides whether to gitignore the 6 `data/processed/*` mods
  or treat them as snapshots to commit.
- After all the above is clean, run the FX comparison gate from
  `fx-twelvedata-migration` per the Session 1 (TwelveData) cutover plan.

---

## 2026-05-07 — Session 7: Hardening & Validation (final session)

**Branch:** `session-7-hardening` off `master @ 11ad23b`.

**This is the last session in `HARDENING_PLAN.md`.** All exit criteria
that can be met inside a single session are met; the remaining
"7-day green" criteria are observation discipline post-this-session.

### What was completed

All 8 tasks:

1. Full test suite ran clean: `make test-unit` 607 / 37s,
   `make test-regression` 20 / 7s, `make test-integration` 56 + 1
   skipped / 67s. **No failures.**
2. Added `tests/regression/test_schema_consistency.py::test_active_model_paths_resolve`
   — walks every `is_active=TRUE` row across all 3 engine
   `*_model_runs` tables, asserts each `model_path` exists AND
   `joblib.load` succeeds. Bundle must contain the keys `predict.py`
   reads (`model`, `platt`, `medians`). This is the test that would
   have caught KI-009 directly. Skipped gracefully when production
   DB isn't available.
3. **Resolved KI-003** — added auto-deactivation in
   `ml/train.py:train_walk_forward` mirroring the pattern
   `crypto/ml/train.py` and `fx/ml/train.py` already had:
   ```sql
   UPDATE ml_model_runs SET is_active = false
   WHERE horizon = ? AND target_threshold = ? AND is_active = true;
   -- then INSERT the new row
   ```
   Equity train no longer leaves stale actives behind.
4. Ran all 6 monitors against real production DB. Results:

   | Monitor | Status | Note |
   |---|---|---|
   | dashboard-consistency | OK | |
   | pipeline-execution | WARN | equity 2d stale (will resolve at 21:00 today's firing — KI-009 fix already in); FX 3h stale (Dukascopy upstream HTTP 404 on recent bars — operator/upstream issue, not MHDE) |
   | config-drift | OK | |
   | model-performance | OK after fix | FX models had `precision_at_threshold ≈ 1.0` from training (stored `precision_top10` as the "baseline", which is ~1.0 by construction). Added a `baseline >= 0.95` skip-with-note guard so the monitor doesn't fire false alerts on this measurement quirk. Real fix would be to change `fx/ml/train.py` to store a more representative metric — recorded inline as a future enhancement, not a KI. |
   | data-quality | OK | |
   | smoke | OK | KI-009 fix from pre-Session-7 follow-up confirmed working. |
5. Doc refresh:
   - `ARCHITECTURE.md`: new "Monitors (Session 6)" section between
     Health Check and Cross-cutting infrastructure. Catalog table,
     telegram routing, contrast with health-check.
   - `OPERATIONS.md`: rewrote "Active model file missing" runbook to
     reflect KI-003 fix (no manual is_active flip needed; auto-deactivation
     in train) and to point at `test_active_model_paths_resolve`.
   - `DATABASE_SCHEMA.md`: spot-checked. No schema sources changed
     since Session 1 (`git diff master..HEAD -- '*schema*' 'storage/migrations.py'`
     is empty); doc remains accurate.
6. Cleared `KNOWN_ISSUES.md`. The full historical bug log moved to
   `legacy/RESOLVED_ISSUES_ARCHIVE.md` (421 lines, 28 entries
   preserved with regression-test pointers). `KNOWN_ISSUES.md` is
   now a 56-line stub with "No open issues" + the convention guide
   for adding the next bug.
7. Final SESSION_LOG.md entry (this one). Updated `HARDENING_PLAN.md`
   Session 7 status to executed.
8. Verification — all exit criteria met (see below).

### Bug found and fixed during this session

- `monitoring/model_performance.py` would fire a false alert against
  FX models because `fx_ml_model_runs.precision_at_threshold` stores
  `precision_top10` (~1.0 by construction). Added a
  `baseline >= 0.95` skip guard. Tests cover this.

### Bug fixed pre-emptively (KI-003, was the last open issue)

`ml/train.py` lacked auto-deactivation of prior actives. Added the
4-line UPDATE. Trains now mirror crypto/fx behavior. The Session 7
KI-009 retrain forensics surfaced this concretely.

### Plan exit-criteria status

| Criterion | Status |
|---|---|
| All tests pass | ✅ 683 tests across 3 suites |
| All monitors green right now | ✅ 5/6 OK; 1 (pipeline-execution) WARN on FX upstream — true positive on Dukascopy lag, not MHDE bug |
| All monitors green for 7 consecutive days | ⏳ observation discipline post-session — deploy first, then watch |
| Documentation matches reality | ✅ ARCHITECTURE / OPERATIONS / DATABASE_SCHEMA reviewed and patched |
| Zero items in KNOWN_ISSUES.md | ✅ archived 28 resolved → live tracker says "No open issues" |
| Health check passes 7 days running | ⏳ same observation discipline |

### Outstanding (post-Session-7 homework)

- **Deploy the 6 monitor systemd units** to production. `OPERATIONS.md`
  has the deploy steps; not auto-deployed in this session per
  Session 6's caution.
- **Watch for 7 days.** The monitors will Telegram if anything drifts.
  After 7 green days the "All monitors green" criterion is met.
- **Decide on `legacy/` deletion.** Plan says "Only if 2+ weeks of
  stability post-Session 0." Today is the same day as Session 0;
  earliest deletion window is 2026-05-21.
- **(Optional)** Refactor `fx/ml/train.py` to store a real
  `precision_at_threshold` metric so the monitor's `baseline >= 0.95`
  guard can be removed.

### Coda

The hardening plan as written has been executed end-to-end. The
system now has documented architecture, full schema docs, operations
runbook, automated tests at three levels, regression coverage for
every bug found along the way, and runtime monitoring that catches
new bugs (KI-008, KI-009, KI-010 were all surfaced by the work in
this plan). The discipline is in place; the rest is observation.

---

## 2026-05-07 — Session 2: Test Infrastructure

**Branch:** `session-2-test-infra` off `master @ fb744bf`.

Framework only. No production-code tests written this session — that's
Sessions 3-5.

### What was completed

All 9 tasks:

1. Categorized the 71 active tests via AST import analysis
   (`.claude/local_scripts/categorize_tests.py`): equity 60, integration
   8, crypto 2, dashboard 1, fx 0.
2. Created the 6 subdirs: `tests/equity/`, `tests/crypto/` (already
   existed), `tests/fx/`, `tests/dashboard/`, `tests/integration/`,
   `tests/regression/` — each with an `__init__.py`.
3. Reorganized all 71 tests via `git mv` in batches (history
   preserved). Ran offline test subset between batches; one test
   (`test_daily_analysis_script.py`) needed a `..` → `../..` path fix
   after moving into `tests/integration/`. All 540 offline tests still
   pass post-reorg.
4. Extended `tests/conftest.py` with 7 new fixtures: `temp_db`
   (in-memory DuckDB pre-loaded with all 4 schema sources →
   storage/migrations + ml/crypto/fx schema.py),
   `synthetic_prices_equity` / `synthetic_prices_crypto` /
   `synthetic_prices_fx` (deterministic random walks with engine-
   appropriate vol and weekend handling), `synthetic_filings`,
   `synthetic_fundamentals`, `mock_telegram` (intercepts
   `requests.post` to Telegram and any `notifications.telegram` helpers).
5. Added `tests/helpers.py` with `assert_db_state`,
   `assert_pipeline_completed_cleanly`, and a stub
   `assert_dashboard_renders` for Session 4.
6. Wrote `tests/test_session2_infra_smoke.py` — 7 tests validating the
   fixtures themselves. All pass in 0.5s.
7. Added `Makefile` with `test`, `test-unit`, `test-integration`,
   `test-regression`, `coverage`, `install-hooks`, `precommit`, `help`
   targets. Network-touching tests skipped by default; override with
   `make NET_SKIPS= test-unit`.
8. Wrote `scripts/pre-commit.sh` — 3-stage hook (py_compile staged
   .py, curated pytest smoke, forbidden-pattern lint). Wall-clock
   runtime 1.8s. Symlinked into `.git/hooks/pre-commit` via
   `make install-hooks`.
9. Added `pytest-cov` to `requirements.txt` and installed in venv.
   `make coverage` runs the unit subset with coverage and writes an
   HTML report to `htmlcov/`. Coverage and `.coverage` files added to
   `.gitignore`.

### What was changed

- Tests reorganized: 71 files moved (`git mv`).
- New: `tests/equity/__init__.py`, `tests/fx/__init__.py`,
  `tests/dashboard/__init__.py`, `tests/integration/__init__.py`,
  `tests/regression/__init__.py`,
  `tests/test_session2_infra_smoke.py`, `tests/helpers.py`,
  `Makefile`, `scripts/pre-commit.sh`.
- Modified: `tests/conftest.py` (extended), `requirements.txt`
  (pytest-cov), `.gitignore` (coverage outputs).
- Symlink installed: `.git/hooks/pre-commit` → `scripts/pre-commit.sh`.
- Path fix in `tests/integration/test_daily_analysis_script.py`
  (`../`→`../../` for the moved location).

### Bugs caught and fixed during the session

- One real path bug surfaced by the reorg:
  `test_daily_analysis_script.py` used a `dirname(__file__)/..` path
  that broke when the file moved one directory deeper. Caught
  immediately by running pytest after the integration batch.

### New known issues to track

None. Existing KI-003 (manual model promotion) is the only open item.

### Pending for the next session (Session 3)

- Write actual unit tests using the new fixtures: features, labels,
  predict, evaluate, signals — for each of the 3 engines. Target the
  80%+ coverage threshold called out in the plan.
- Decide whether `assert_dashboard_renders` should call the underlying
  query functions in `dashboard/services/queries.py` (likely yes —
  matches what the dashboard consumes without booting Streamlit).

---

## 2026-05-07 — Pre-Session-7 follow-ups (KI-009 retrain, KI-010 forensics)

**Branch:** `pre-session-7-fixes` off `master @ 969fdd6`.

Three operational follow-ups before Session 7:

### 1. KI-009 fixed — equity models retrained

Ran walk-forward training for all three horizons:
```
venv/bin/python main.py ml train --label label_5d_3pct  --horizon 5d  --threshold 0.03
venv/bin/python main.py ml train --label label_10d_5pct --horizon 10d --threshold 0.05
venv/bin/python main.py ml train --label label_20d_5pct --horizon 20d --threshold 0.05
```

Results — all PASSED walk-forward success criteria (Lift > 1.3, AUC > 0.55):

| Horizon | Avg precision | Avg AUC | Avg lift | Joblib |
|---|---|---|---|---|
| 5d  | 61.4% | 0.671 | 1.91x | `models/saved/5d_label_5d_3pct_20260507_180903.joblib` |
| 10d | 63.7% | 0.691 | 2.10x | `models/saved/10d_label_10d_5pct_20260507_180922.joblib` |
| 20d | 72.6% | 0.666 | 1.59x | `models/saved/20d_label_20d_5pct_20260507_180936.joblib` |

Wall-clock ~10s per training. Top features (gain-based): `atr_pct_20d`,
`realized_vol_60d`, `yield_curve_10y_2y`, `price_vs_200d_ma`, `vix_level`.

Then manually deactivated the 3 stale May-5 rows via UPDATE
(KI-003: train doesn't auto-deactivate). Final `is_active=TRUE` set
contains exactly the 3 new models pointing at present joblibs.

`ml predict --skip-outcomes` ran cleanly: 7 predictions on 20d horizon
(+ 5d / 10d). Engine confirmed working.

### 2. KI-010 — May 5 anomaly investigated, root cause is KI-106

The "12 vs 40 prediction" anomaly from May 5 had nothing to do with
the ML engine itself. It was a downstream consequence of **KI-106**
(User=/Group= lines on the user-level mhde-daily-analysis.service)
that hadn't been fixed at the time:

- May 5 23:15 firing: `journalctl` shows exit code 216/GROUP.
- No `data/logs/daily_analysis_2026-05-05.log` exists (script never ran).
- `prices_daily` for May 5 had 47 of ~522 expected tickers; `ml_features`
  had 19 of ~312 expected rows.
- May 5 21:00 predict scored against the partial feature universe → 12
  predictions instead of 40.

KI-106 was fixed on 2026-05-06; May 5 was the last bad night. No new
code fix needed; documented as the cascade record (KI-010).

### 3. Session 7 hardening note

`tests/regression/test_schema_consistency.py::test_models_saved_path_exists`
is too lax — it only asserts the directory exists, not that
`*_model_runs.is_active=true` rows have resolvable joblib paths.
Session 7 should add `test_active_model_paths_resolve` that walks
every is_active row across all 3 engines and asserts
`Path(model_path).exists()` AND `joblib.load(model_path)` doesn't raise.

### Verification

- `make test-unit`: passes.
- `make test-regression`: passes.
- `monitoring/smoke_test.py` dry-run: now reports OK on the
  model-loadability check.

---

## 2026-05-07 — Session 6: Monitoring & Verification

**Branch:** `session-6-monitoring` off `master @ 0808b47`.

Six runtime monitors built, each as a Python module under `monitoring/`,
wired to a `main.py monitor <name>` CLI subcommand and a paired
`systemd/mhde-monitor-*.{service,timer}`. The smoke monitor caught a
real production issue (KI-009) on its first dry-run.

### What was completed

All 12 tasks. New code:

- `monitoring/__init__.py` + `monitoring/alert.py`: shared dispatcher
  with `MonitorResult` dataclass, severity prefixing, and a
  `MONITORING_DRY_RUN=true` env switch that suppresses real Telegram
  sends. Bottoms out in `fx.bot.telegram_bot.send_message`.
- `monitoring/dashboard_consistency.py` (6h): dashboard query layer
  vs direct DB count parity.
- `monitoring/pipeline_execution.py` (hourly): per-engine recency +
  row-count vs 14d rolling avg. Floors: 50% warn, 20% fail.
- `monitoring/config_drift.py` (daily): repo `systemd/*` ↔ deployed
  copies in `/etc/systemd/system` + `~/.config/systemd/user`.
- `monitoring/model_performance.py` (daily): rolling 7d precision per
  active model vs walk-forward baseline. 0.8x threshold.
- `monitoring/data_quality.py` (daily): per-engine ticker / symbol /
  bar coverage on latest day vs 14d avg. 0.8x floor.
- `monitoring/smoke_test.py` (hourly): DB opens, every active joblib
  loads, dashboard query layer returns rows.

CLI: new `cli.group monitor` with 6 subcommands in `main.py`.

systemd: 12 unit files in `systemd/mhde-monitor-*.{service,timer}`.
**Not auto-deployed.** Install instructions in OPERATIONS.md.
Schedules staggered to avoid the FX :05 firing window:
  dashboard 03/09/15/21:30 | pipeline :40 | config-drift 12:15 |
  model-perf 13:15 | data-quality 02:00 | smoke :50.

Tests: `tests/equity/test_monitoring.py` — 12 tests covering each
monitor's pure-logic path with `temp_db` and `mock_telegram`.

OPERATIONS.md: new "Monitors" section — catalog table, manual
invocation, deploy steps, threshold tuning constants, alert format,
overlap with existing health-check, alert-suppression playbook.

### Bug found mid-session — KI-009

**Equity active-model joblibs missing on disk.** The smoke test on
its first dry-run reported:

```
[!!] MHDE monitor: smoke_test
End-to-end smoke failed
- equity model: path missing: models/saved/5d_label_5d_3pct_20260505_092040.joblib
```

`ml_model_runs` has 3 rows with `is_active=true` pointing at:
- `models/saved/5d_label_5d_3pct_20260505_092040.joblib`
- `models/saved/10d_label_10d_5pct_20260505_092031.joblib`
- `models/saved/20d_label_20d_5pct_20260505_092022.joblib`

`/home/jpcg/MHDE/models/saved/` currently contains only `crypto/` and
`fx/` subdirectories — the 3 equity joblibs are gone. Likely cause is
some operation between caf77e4 (KI-004 `git rm --cached`) and now
that deleted the on-disk files. Git history cannot recover them since
they were de-tracked.

**Why Session 5's regression test missed it.** `test_models_saved_path_exists`
only asserts the directory exists, not that specific paths in
`*_model_runs.is_active=true` resolve. Recorded as KI-009; Session 7
should harden the test.

**Action required for production.** Re-run `venv/bin/python main.py
ml train --label label_5d_3pct …` (and 10d / 20d) to regenerate, or
wait for the weekly Sun 21:30 retrain.

### Bugs found in monitor code (caught by tests, fixed)

- `monitoring/dashboard_consistency.py`: invalid SQL
  `SELECT COUNT(*) … ORDER BY as_of_date DESC LIMIT 200` — DuckDB
  rejects ORDER BY without aggregation. Dropped the ORDER BY.
- `monitoring/model_performance.py`: queried `target_threshold` for
  all engines, but `fx_ml_model_runs` uses `target_pips`. Per-engine
  column selection now.

### Verification

- All 6 monitors run cleanly under `MONITORING_DRY_RUN=true`. They
  produced a mix of OK and real-alert results — alerts surfaced
  KI-009 plus an FX freshness lag (latest bar 3h old vs 2h threshold)
  and FX-baseline anomalies (training stored 1.0 / 0.99 sentinel
  baselines, real rolling precision is below the 0.8× cutoff).
- `make test-unit`: 607 passed in 37s (was 595 — +12 from monitor
  tests).
- `make test-regression`: 20 passed in 7s.

### Pending for next session (Session 7)

- **Resolve KI-009** by retraining or copying joblibs from a backup.
  This is operational, not code.
- Harden `test_models_saved_path_exists` to validate every
  `is_active=true` row's `model_path` resolves.
- Investigate the FX freshness lag (3h old) vs the 2h threshold —
  scheduled hourly run may be drifting.
- Investigate FX baseline = 1.0 sentinel in `fx_ml_model_runs.precision_at_threshold`.
  Either fix the train code or change the monitor's "no real
  baseline" guard.
- Deploy the 6 monitors to production once Session 7 audit completes.

---

## 2026-05-07 — Session 5: Regression Tests

**Branch:** `session-5-regression-tests` off `master @ 8e45129`.

20 dedicated regression tests across 5 files in `tests/regression/`,
plus a coverage map in `tests/regression/__init__.py` linking each
KI in `KNOWN_ISSUES.md` to the test that guards it. Found and fixed
one new production bug (KI-008) in the daily-analysis wrapper.

### What was completed

All 7 tasks. Files added:

- `tests/regression/__init__.py` — KI-→-test mapping table.
- `tests/regression/test_systemd_units.py` (7 tests). Covers KI-101
  (retrain timer staggering), KI-102 (equity predict ExecStart chain),
  KI-106 (no User=/Group= in user-level units), KI-108 (crypto predict
  6-step chain), KI-109 (health-check timer deployed), KI-112 (every
  repo unit validates + matches deployed copy).
- `tests/regression/test_dashboard_structure.py` (3 tests). Covers
  KI-105 (no module-level DB connection — AST scan that walks only
  Module.body, not function bodies, after a v1 false positive),
  KI-113 (outcome columns present in all 3 schemas).
- `tests/regression/test_legacy_isolation.py` (3 tests). Session 0
  hold-the-line: no active code imports legacy/, legacy/ exists with
  ~99 .py files, README explains the dormant status.
- `tests/regression/test_schema_consistency.py` (4 tests). KI-001
  (nginx /review/ 404 block in conf), KI-117 (models/saved/ exists),
  schema migration: every CREATE TABLE has reader+writer in active
  code (engine schemas + storage/schema.sql, with explicit DORMANT
  exclusions for `scorecard_experiments` and `promotion_gate_results`).
- `tests/regression/test_cli_registry.py` (3 tests). Every `main.py`
  command invoked from systemd / shell wrappers responds to `--help`.
  KI-004 trained-model artifacts gitignored.

### Bug found and fixed during this session

**KI-008** — `priority-refresh-queue` invoked at the wrong CLI level.

The daily-analysis wrapper `run_mhde_daily_analysis.sh` ran
`main.py priority-refresh-queue --enriched-csv ...` but the CLI is
actually registered under the `data` group (`main.py data priority-refresh-queue`,
defined at `main.py:581`). The wrapper's `tee`-pipe swallows the click
exit code, so `set -e` couldn't trap it. **Step d has been silently
failing every Mon-Fri 23:15** since the command was moved under
`data`.

Fix: changed the wrapper to invoke `main.py data priority-refresh-queue`.
Recorded as KI-008 with the lesson "set -e doesn't propagate through
tee — either drop the tee or add explicit error checks."

The new test (`test_systemd_main_commands_invokable`) caught it
immediately by parsing every `main.py X Y` invocation in systemd /
shell wrappers and running `--help` on each.

### Coverage map — every KI now has a regression test

See `tests/regression/__init__.py` for the full table. Highlights:

| Layer | Coverage |
|---|---|
| Sessions 3 / 4 unit + integration | KI-103, KI-104, KI-107, KI-110, KI-111 (already in place) |
| Session 5 regression | KI-001, KI-004, KI-005, KI-006, KI-007, KI-008, KI-101, KI-102, KI-105, KI-106, KI-108, KI-109, KI-112, KI-113, KI-116, KI-117 |
| No test (documentation drift, not code) | KI-002 |
| Open (will get tests when fixed) | KI-003 |

### Verification

- `make test-regression`: 20 passed in 5.4s.
- `make test-unit`: 595 passed in 36s (unchanged — regression target is
  separate per plan).
- `make test-integration`: 56 passed + 1 skipped in 66s (unchanged).

### Pending for the next session (Session 6)

- Build the 6 monitoring jobs: dashboard-vs-DB consistency, pipeline
  execution, configuration drift, model performance, data quality,
  end-to-end smoke. Each fires a Telegram alert on failure.
- Decide whether to expand pre-commit-hook smoke to include
  `make test-regression` (5.4s — fits the budget).
- Investigate how long Step d of daily-analysis has been silently
  failing in production (KI-008). If priority-refresh-queue.csv
  hasn't been refreshed in months, the root-cause-enrichment chain
  may have stale inputs.

---

## 2026-05-07 — Session 4: Integration Tests

**Branch:** `session-4-integration-tests` off `master @ 985e243`.

End-to-end pipeline tests with synthetic data plus failure-mode
coverage. All major regression cases from `KNOWN_ISSUES.md` are now
covered by at least one integration test.

### What was completed

All 8 tasks. Tests added by file:

- `tests/integration/_helpers.py` — `train_tiny_model` (XGBoost +
  Platt + medians bundle), `register_active_*_model` (3 engines),
  `seed_active_company`, `seed_crypto_universe`, and price-insert
  helpers. Reusable across all engine tests.
- `tests/integration/test_equity_pipeline.py` — 3 tests. 50 tickers ×
  220 days → labels → features → score → fill_outcomes. Covers KI-104
  (trading-day window).
- `tests/integration/test_crypto_pipeline.py` — 3 tests. 5 symbols ×
  80 days. Covers KI-103 (horizon window match).
- `tests/integration/test_fx_pipeline.py` — 4 tests (1 skipped on
  weekend bar). 600 hourly bars × 4 active models. Covers KI-110
  (position-aware alert suppression) end-to-end through the bot
  helper, with `_open_conn` monkeypatched to return temp_db.
- `tests/integration/test_cross_engine_consistency.py` — 6 tests:
  shared prediction columns, distinct entity keys
  (ticker / symbol / time-only), per-engine model_runs tables,
  freshness coverage, health orchestrator parity.
- `tests/integration/test_failure_modes.py` — 8 tests: stale-data skip
  (equity / crypto), stale-but-continue (FX, ADR-010), empty-universe
  graceful handling, no active models → empty predictions, **DuckDB
  lock-retry (KI-111)** with monkeypatched `duckdb.connect` and
  `time.sleep`, non-lock IOException propagation, missing model file
  → FileNotFoundError raised.

Plus the Session 4 deliverable from `tests/helpers.py`:

- `assert_dashboard_renders` — replaced the Session 2 stub with a real
  implementation that calls `dashboard/services/queries.py` directly
  (one of 12 page-query functions) and validates row count + key set.
  Sidesteps Streamlit's runtime entirely.

### Design notes

- **Tiny model factory.** Integration tests need a real joblib bundle
  for `predict.py` to load. `train_tiny_model` fits an XGBClassifier
  on noise with `positive_rate=0.85` so the model produces probabilities
  above the LOW_THRESHOLD=0.50 filter — otherwise predict.py drops all
  predictions before they reach the table.
- **Precision metrics not asserted.** Synthetic random-walk data has
  no predictive signal by construction; an integration test asserting
  precision range against a noise-trained model is meaningless.
  Tests assert structural completeness (predictions written, outcomes
  filled, schema parity, dashboard query returns rows) instead.
- **FX bot connection.** `fx/bot/telegram_bot.py:_open_conn` opens its
  own connection from `storage.config`, not a passed-in conn. Tests
  monkeypatch `_open_conn` to return a wrapper around `temp_db` whose
  `close()` is a no-op (the fixture owns lifetime).

### Bugs caught during this session

None. All four engine pipelines produced expected output on first
correct setup. One test failure was self-inflicted (used
`'long_gbp'` as the position string when production code expects
`'HOLDING_GBP'`); fixed in the test.

### Verification

- `make test-integration`-equivalent: 56 passed + 1 skipped in 66s.
- `make test-unit` unaffected: still passes.
- Integration tests cover regressions for: KI-103, KI-104, KI-110,
  KI-111. Plus the new Session 3 fixes are exercised through pipeline
  runs (KI-005, KI-006, KI-007).

### Pending for the next session (Session 5)

- Convert each entry in `KNOWN_ISSUES.md` resolved-section into a
  dedicated regression test that the suite runs going forward. Many
  are already implicitly covered; Session 5 makes that coverage
  explicit by name and adds the structural regression tests called
  out in the plan (schema migration, CLI registry, service files,
  timer schedules, legacy isolation).
- Fix the `datetime.utcnow()` deprecation warnings (Python 3.12+).
  Mostly cosmetic; ~10 call sites across pipelines/daily_radar,
  conftest, and a few others.

---

## 2026-05-07 — Session 3: Unit Tests

**Branch:** `session-3-unit-tests` off `master @ d69837f`.

Wrote ~80 unit tests across the 12 target modules called out in the
plan (features, labels, predict, evaluate / signals — per engine —
plus health checks and pipeline freshness). All target modules now at
or above the 80% coverage target.

### What was completed

All 7 tasks. Tests added by file:

- `tests/fx/test_features.py` — 6 tests
- `tests/fx/test_labels.py` — 7 tests
- `tests/fx/test_predict.py` — 5 tests
- `tests/fx/test_signals.py` — 9 tests (full BUY/SELL/WAIT decision matrix)
- `tests/crypto/test_features.py` — 5 tests
- `tests/crypto/test_labels.py` — 6 tests
- `tests/crypto/test_predict.py` — 7 tests
- `tests/crypto/test_evaluate.py` — 5 tests
- `tests/equity/test_ml_features.py` — 5 tests
- `tests/equity/test_ml_labels.py` — 6 tests
- `tests/equity/test_ml_predict.py` — 7 tests (incl. KI-104 trading-day window regression)
- `tests/equity/test_ml_evaluate.py` — 5 tests
- `tests/equity/test_health_ml_checks.py` — 8 tests
- `tests/equity/test_pipeline_freshness.py` — 12 tests (per-engine freshness reports)

Plus `tests/equity/test_health.py` was rescued: 6 tests that had been
failing pre-Session-0 (CatalogException due to a missing
`ml_predictions` table in the local fixture) now pass — the local
`conn` fixture was rewritten to delegate to the project-wide `temp_db`
fixture, which loads every active schema.

### Coverage on plan-listed modules

All ≥ 80% target met:

| Module | Coverage |
|---|---|
| ml/features.py | 80% |
| ml/labels.py | 100% |
| ml/predict.py | 90% |
| ml/evaluate.py | 98% |
| crypto/ml/features.py | 92% |
| crypto/ml/labels.py | 100% |
| crypto/ml/predict.py | 83% |
| crypto/ml/evaluate.py | 100% |
| fx/ml/features.py | 81% |
| fx/ml/labels.py | 100% |
| fx/ml/predict.py | 98% |
| fx/ml/signals.py | 92% |
| health/checks.py | 89% |
| health/ml_checks.py | 87% |
| pipelines/freshness.py | 97% |

Average across the 15 modules: ~92%.

### Bugs caught during the session and fixed

Three real production bugs surfaced by the new tests:

- **KI-005** `fx/ml/labels.py`: `IndexError` on empty `fx_prices_hourly`.
  Second `for` loop did `range(n - 48, n - 24)` → negative-bounded
  range. Fix: `range(max(0, n - 48), max(0, n - 24))`.
- **KI-006** `ml/features.py`: `Parser Error` on empty equity ML
  universe — `WHERE ticker IN ()` is invalid SQL. Fix: early return
  when `tickers` is empty.
- **KI-007** `ml/evaluate.py`: `ValueError: min() arg is empty` when
  `print_walk_forward_results` is called with zero folds. Fix: guard
  the success-criteria block on `if fold_results:`.

All three documented in `KNOWN_ISSUES.md` with regression-test pointers.

Plus one fixture fix in `tests/conftest.py`: `synthetic_prices_fx`
default `data_quality` was `"good"` (the schema default) but production
writes `"OK"` and the labels/features queries filter for `'OK'`. Synthetic
default now `"OK"` to match.

### Verification

- `make test-unit`: 595 passed in 38.5s. ~38s wall-clock — slightly over
  the plan's 30s target, mostly from the equity ml/features feature
  computation over 600 synthetic bars × 2 symbols.
- All 15 target modules ≥ 80% coverage.
- Pre-commit hook still 1.6s.

### Pending for the next session (Session 4)

- Integration tests: end-to-end pipeline runs with synthetic data
  (already established by the test-infra), plus failure-mode tests
  (missing data, lock retry, model file absent).
- Replace the stub `assert_dashboard_renders` in `tests/helpers.py` —
  the suggested implementation is to call
  `dashboard/services/queries.py` directly without booting Streamlit.
- Decide whether to widen `make test-unit` time budget from 30s to 45s
  given Session 3 made it ~38s; or split slow tests into `test-slow`.

---

## 2026-05-07 — Pre-Session-2 follow-ups (KI-001, KI-004)

**Branch:** `pre-session-2-fixes` off `master @ 1050eab`.

Two outstanding issues from earlier sessions resolved before starting
Session 2 (test infrastructure).

### KI-001 — `/review/` returns 502 → 404

The nginx conf at `/home/jpcg/homeboard/nginx/nginx.conf` already had
the `location /review/ { return 404; }` block from Session 0's
follow-up edit, but `nginx -s reload` was leaving the response at 502.

Diagnosis: the host file is a **single-file bind mount** into the
nginx container. The Edit tool writes via atomic rename, which changes
the host file's inode. Docker single-file bind mounts pin to the
original inode and don't follow rename-replace, so nginx kept reading
the old config inside the container even after a reload.

Fix: `docker compose restart nginx` to force the container to re-mount
and re-read the file. `/review/` now returns 404 cleanly.

Lesson recorded in `KNOWN_ISSUES.md` KI-001: future host-file edits
that feed bind-mounted single files need either a full container
restart or an inode-preserving editor (`sed -i`, `cat > file << EOF`).
Plain `nginx -s reload` will silently serve stale config.

### KI-004 — `models/saved/**` gitignored

Added four patterns to `.gitignore`:
```
models/saved/**/*.joblib
models/saved/**/*.pkl
models/saved/**/*.bin
models/saved/**/*.model
```

Removed the 3 previously-tracked equity joblibs from the index with
`git rm --cached` (files preserved on disk). All 9 model binaries (3
equity + 2 crypto + 4 FX) on disk are now ignored. Verified by
`git ls-files models/saved/` returning empty.

### Pending for Session 2

Test infrastructure: pytest fixtures (in-memory DuckDB with all
schemas, synthetic data per engine, mock Telegram), helpers, Makefile
targets, CI runner, coverage reporting.

---

## 2026-05-07 — Session 1: Documentation as Source of Truth

**Branch:** `session-1-documentation` off `master @ f59baf9`.

### What was completed

All 9 tasks from the Session 1 task list:

1. Mapped every database table from `ml/schema.py`, `crypto/schema.py`,
   `fx/schema.py`, `storage/schema.sql`, and `storage/migrations.py`,
   plus enumerated the 52 tables in the live DB to confirm complete
   coverage.
2. Wrote `DATABASE_SCHEMA.md` — purpose + columns + reader/writer
   modules per table, grouped by engine. Cross-cutting notes on time
   conventions, outcome filling, active-model resolution, single-row
   tables.
3. Traced each engine's pipeline end-to-end by reading
   `pipelines/{ml,crypto,fx}_prediction_pipeline.py` and
   `pipelines/freshness.py`. Captured the chained ExecStart structure,
   freshness policies, fill_outcomes behavior.
4. Wrote `ARCHITECTURE.md` — system overview with ASCII data flow,
   per-engine sections (equity ML, crypto ML, FX ML), the
   daily-analysis path, dashboard, health check, cross-cutting infra,
   and the ATSRP external dependency. Plus a "what's not in production"
   pointer at `legacy/`.
5. Wrote `OPERATIONS.md` — runbook layer: daily smoke checks, manual
   pipeline invocations per engine, recovery procedures (DuckDB lock,
   stale data, missing model file, Telegram, dashboard 502, nginx),
   deploy procedures, dashboard auth rotation, prediction history
   queries, source-specific ingestion debugging, escalation matrix.
6. Wrote `KNOWN_ISSUES.md` — bug tracker with naming convention
   (KI-0XX open, KI-1XX resolved). 4 open issues (the /review/ 502,
   plan-vs-codebase drift now resolved, manual model promotion, and
   `models/saved/` not gitignored) plus 17 resolved entries with
   Session 5 regression-test pointers.
7. Expanded `DECISIONS.md` from 5 to 12 ADRs. Added ADR-006 (XGBoost
   choice), ADR-007 (walk-forward CV), ADR-008 (DuckDB single-file),
   ADR-009 (service chaining in ExecStart), ADR-010 (freshness guards),
   ADR-011 (position-aware FX alerts), ADR-012 (per-engine
   `schema.py`). Verified each claim against active code before
   recording.
8. Updated `CLAUDE.md` read-first list to point at the new docs in the
   right reading order. Appended this Session 1 entry to
   `SESSION_LOG.md`.
9. Verified Session 1 exit criteria — every database table documented,
   every systemd unit referenced via `INFRASTRUCTURE.md` from
   `ARCHITECTURE.md` and `OPERATIONS.md`, every major decision has an
   ADR, the new docs are internally cross-linked.

### What was changed

- New: `DATABASE_SCHEMA.md`, `ARCHITECTURE.md`, `OPERATIONS.md`,
  `KNOWN_ISSUES.md`.
- `DECISIONS.md`: appended 7 new ADRs.
- `CLAUDE.md`: read-first list expanded from 5 entries to 8, ordered
  by what's needed first.
- `SESSION_LOG.md`: this entry.

No code changes. Session 1 was a pure documentation pass.

### Bugs caught and fixed during the session

- One spec drift caught while writing `DATABASE_SCHEMA.md`: the dead
  `outcomes/labels.py` file was supposedly resolved in Session 0, but
  the per-table reader/writer audit confirmed `outcomes/__init__.py`
  no longer references it. Accurate.

### New known issues to track

None new. All issues recorded as KI entries already existed.

### Pending for the next session (Session 2)

- Build pytest scaffolding: `tests/conftest.py` fixtures for in-memory
  DuckDB with all schemas applied, synthetic data generators per
  engine, mock Telegram. CI runner. Coverage reporting.
- Decide on `models/saved/` gitignore policy (KI-004) before the next
  retrain otherwise the binaries will grow the repo.
- Decide on auto-promotion for `*_train_cmd` (KI-003) so the weekly
  retrain actually changes the live model.

---

## 2026-05-07 — Session 0: Legacy code cleanup

**Branch:** `session-0-legacy-cleanup` off `master @ 7b46c50`.

### What was completed

All 11 tasks from the Session 0 task list:

1. Pre-flight checkpoint commit (`7b46c50`) capturing in-flight FX /
   pipeline / systemd work that was in the dirty tree at session start.
2. Inventory of all 250+ project .py files via reachability analysis
   (`.claude/local_scripts/inventory_active_legacy.py`). Entry points
   were derived from systemd unit `ExecStart` lines, the
   `mhde-daily-analysis.service` shell wrapper, and dashboard imports.
3. Confirmed every LEGACY candidate is unreachable from ACTIVE code via
   grep + import-graph BFS.
4. Moved 70 dormant code files into `legacy/` via `git mv` (history
   preserved). 5 whole directories: `backtest/`, `governance/`,
   `learning/`, `models/`, `review/`. Plus targeted moves under
   `crypto/ml/`, `fx/ml/`, `ml/`, `missed/`, `outcomes/`, `pipelines/`,
   `reports/`, `scoring/`, `storage/`, `universe/`, `hypotheses/`, and
   the entirety of `dashboard/pages/_legacy/` (19 pages).
5. Moved 29 legacy-targeting tests to `legacy/tests/`
   (`.claude/local_scripts/find_legacy_targeting_tests.py` derived the
   list).
6. Disabled `mhde-review-server.service` and `mhde-bridge-relay.service`
   (`systemctl --user disable --now`).
7. Removed the `upstream mhde_review` block and the `location /review/`
   block from `/home/jpcg/homeboard/nginx/nginx.conf`. JP ran the
   `nginx -t` and reload (config valid; `/` still 200; `/review/` now
   returns 502).
8. Fixed two import breakages caused by the move:
   - `outcomes/__init__.py` re-exported a function from the
     now-legacy `outcomes/labels.py`. Re-export deleted (no callers).
   - `reports/weekly_review.py` was an orphan tied to the dead
     `weekly_review` CLI; moved to `legacy/reports/weekly_review.py`.
9. Verified safe-checks per JP's choice (no live pipelines, no test
   telegram):
   - `python -m py_compile` over every active .py: clean (exit 0).
   - Import-resolution smoke on 50 entry-point modules: 50/50 OK.
   - `systemd-analyze verify` on every unit in `systemd/`: 13/13 clean.
   - Dashboard query smoke (`MHDE_DASHBOARD_AUTH_ENABLED=false …
     test_dashboard_queries.py`): 10/10 queries pass.
   - `pytest --collect-only`: 743 tests collected, no errors.
10. Wrote new docs: `legacy/README.md`, `DECISIONS.md` (5 ADRs),
    updated `INFRASTRUCTURE.md` (review server section + bridge-relay
    + nginx route), updated `CLAUDE.md` (read-first list + legacy
    pointer), initialized this `SESSION_LOG.md`.

### Plan corrections (recorded in DECISIONS.md ADR-005)

`HARDENING_PLAN.md` Session 0 listed several items as legacy that
turned out to be ACTIVE:

- `scoring/scorecard.py` is still imported by `pipelines/daily_radar.py`
  via `mhde-daily-analysis.service` (Mon-Fri 23:15). Only
  `scoring/incomplete_diagnostics.py` was movable from `scoring/`.
- `features/feature_builder.py` is still imported transitively by the
  same path. `features/` stays.
- The "missed" CLI is partly active: `missed.catalyst_queue`,
  `missed.catalyst_digest`, `missed.prediction_report`,
  `missed.root_cause_enrichment` are all invoked by the daily-analysis
  shell script with `--no-mock --provider openai`. Only the dormant
  subset (9 files) moved.
- `daily_radar` orchestration is fully active.
- `mhde-health-check.service` exists and runs `main.py system
  health-check`; the plan didn't mention it.

### What was changed

- 18 prior in-flight tracked files committed as `7b46c50` (FX
  position-aware alerts, pipeline freshness guards, service chaining).
- ~100 .py files moved into `legacy/` plus 29 tests.
- `outcomes/__init__.py`: dead `compute_forward_returns` re-export
  removed.
- `INFRASTRUCTURE.md`: review server / bridge-relay sections retired;
  user-services table updated (added `mhde-health-check`); restart
  cheat sheet pruned; reverse-proxy routes pruned.
- `CLAUDE.md`: read-first list now points at `HARDENING_PLAN.md`,
  `DECISIONS.md`, `SESSION_LOG.md`, plus a `legacy/` warning.
- `/home/jpcg/homeboard/nginx/nginx.conf`: review upstream + location
  removed.
- New: `DECISIONS.md`, `legacy/README.md`, this `SESSION_LOG.md`.

### Bugs found and fixed during the session

- **`models/saved/` was almost lost.** `git mv models/ legacy/models/`
  swept the trained-artifact directory into `legacy/`. Caught when
  active config grep showed `ml/train.py:26`, `crypto/config.py:26`,
  `fx/config.py:31`, `health/ml_checks.py:17` all hardcode
  `models/saved`. Restored with `git mv legacy/models/saved
  models/saved` before any pipeline could miss the artifacts.
- **Dead `outcomes.compute_forward_returns` re-export.** First active
  module to fail the import smoke test. Removed the line from
  `outcomes/__init__.py` (ADR-004).

### New known issues to track

- `https://mhde.duckdns.org/review/` returns 502 instead of 404. The
  Streamlit catch-all matches the path and the relay errors. Add an
  explicit `location /review/ { return 404; }` block in a follow-up.
- `HARDENING_PLAN.md` Session 0 description is partially wrong about
  what's legacy (see ADR-005). Update the plan in Session 1 so
  Sessions 2-7 don't re-derive the same misclassifications.
- 8 tests under `legacy/tests/` (and the 29 total) won't run from
  there — they import top-level `governance.*`, `learning.*`, etc.,
  which now live under `legacy.governance.*`. Acceptable for
  reference-only state. Session 5 (regression tests) will replace
  them with new active tests where appropriate.

### Pending for the next session (Session 1)

- Update `HARDENING_PLAN.md` with the corrected legacy / active
  classification before doing the full ARCHITECTURE.md /
  DATABASE_SCHEMA.md / OPERATIONS.md / KNOWN_ISSUES.md write-up.
- Initialize `KNOWN_ISSUES.md` (the 502 issue and the plan-vs-code
  drift go in there).
- Decide whether to delete or archive the empty `dashboard/pages/`
  directory (currently has no content but is still tracked).
- Decide whether `data` CLI subcommands (`data inventory`,
  `data enrich-ticker-details`, `data sector-diagnostics`,
  `data peer-cluster-diagnostics`) are worth keeping in `main.py`
  given their underlying modules moved to `legacy/storage/inventory.py`
  and `legacy/universe/ticker_details_enricher.py`. Currently the CLI
  registers but the commands ImportError when invoked.
