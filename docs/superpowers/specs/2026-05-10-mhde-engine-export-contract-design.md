# MHDE Engine-Export Contract — Design

Date: 2026-05-10
Author: JP / Claude Code (brainstorming session)
Related contract: `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md` (v1.0)
Path-to-live: `/home/jpcg/MHDE/docs/PATH_TO_LIVE_PLAN.md`
Phase 1B winner: `/home/jpcg/MHDE/docs/PHASE1B_HANDOFF.md`

## 1. Goal

Produce two JSON files that form the entire interface between MHDE and the
crypto-trading-engine:

1. `data/exports/active_spec.json` — strategy spec; rare updates
   (when Phase 1B winner is re-derived).
2. `data/exports/predictions_YYYY-MM-DD.json` (+ `predictions_latest.json`
   symlink) — daily predictions for the engine's 06:30 UTC entry phase.

Schemas, hash algorithm, and validation rules are pinned in INTERFACE.md.
This design covers the MHDE-side production code only.

## 2. Constraints from INTERFACE.md

- `spec_hash` is `sha256:<hex>` over `json.dumps(spec_copy, sort_keys=True,
  separators=(",", ":"))` with the `spec_hash` field substituted by the
  empty string (not removed). Engine and MHDE MUST produce byte-identical
  canonicalisation.
- `predictions_YYYY-MM-DD.json` must list ALL 50 universe coins ranked
  1..N consecutively (engine validation: ranks unique and consecutive
  starting from 1).
- Symlink `predictions_latest.json` must point at today's file.
- `phase_0_status` is `"interim"` until phase0 evaluation produces a
  verdict; engine in live mode requires `"passed"`.
- `sizing.deploy_pct + sizing.reserve_pct == 1.0`, `leverage` ∈ {1.0, 2.0}.

## 3. Decisions (locked in this session)

| Decision | Choice | Rationale |
|---|---|---|
| Predictions source | Re-score full universe in export script | `crypto_ml_predictions` is capped at 15 by `score_universe()`'s adaptive threshold + `MAX_PREDICTIONS`; export needs full 50. Re-scoring decouples the contract from pipeline-side behavior changes. |
| Backtest expectations methodology | Portfolio-realistic (`report.simulate_portfolio`) | Engine compares paper-trade portfolio P&L vs these → must use same methodology. Sum-of-fractions metrics are docs-flagged as ranking-only / inflated absolute. |
| Risk params (max_account_drawdown_pct, daily_loss_limit_usd, position_size_min/max) | Adopt INTERFACE.md §2 example values: `0.30 / 100.0 / 5.0 / 0.20` | Drafted for $1k Phase 2 paper trading. New ADR in DECISIONS.md will document revisit at Phase 3 → 4 transition. |
| Static-config home | `crypto/exports/spec_config.py` (Python module) | Phase-1B-derived fields read from DB; static fields as named constants. Git history is the audit trail. |
| Phase 1B winner reference | Hardcoded constant `PHASE1B_WINNER_RUN_ID = "backtest_10d_D_top_n_a02e15a0"` in `spec_config.py` | Phase 1B re-runs require an explicit code edit + commit. No new DB column / no migration. Matches locked-decisions table in PATH_TO_LIVE_PLAN.md. |
| `data/exports/` tracking | Gitignored | Operational artifacts updated daily; mirrors `data/reports/` policy from commit 0f04fc5. |
| Timer cadence for export-predictions | Daily 7 days/week, 06:15 UTC | Crypto markets don't close; engine entry phase 06:30 UTC; consistent with existing `mhde-crypto-predict.timer`. |
| Symlink replacement | Atomic, replace silently | `os.replace(tmp_symlink, link_path)` on the same filesystem is POSIX-atomic. Engine never sees a partially-written link. |

## 4. Module structure

New tree:

```
crypto/exports/
├── __init__.py
├── spec_config.py              # static spec fields + PHASE1B_WINNER_RUN_ID
├── hashing.py                  # compute_spec_hash() — INTERFACE.md §2.3 algorithm
├── _io.py                      # atomic_write_json, atomic_replace_symlink
├── write_active_spec.py        # build & write active_spec.json
└── write_daily_predictions.py  # full-universe rescore + ranked JSON + symlink
```

Tests:

```
tests/crypto/exports/
├── __init__.py
├── test_hashing.py                              # golden-vector + invariants
├── test_io_atomic.py                            # symlink + json atomicity
├── test_active_spec_schema.py                   # schema match + validation rules
├── test_daily_predictions_schema.py             # rank uniqueness + prob bounds + n=universe
├── test_write_active_spec_integration.py        # synthetic DB → produces valid spec
└── test_write_daily_predictions_integration.py  # synthetic universe + features + model bundle → ranked JSON
```

## 5. Module responsibilities

### 5.1 `spec_config.py`

Pure constants. No imports from other crypto modules. The single source of
truth for everything in `active_spec.json` that is NOT derived from the
Phase 1B winner row in DB.

Exports:

- `SPEC_VERSION = "1.0.0"`
- `PHASE1B_WINNER_RUN_ID = "backtest_10d_D_top_n_a02e15a0"`
- `SIZING = {"deploy_pct": 0.80, "reserve_pct": 0.20, "max_concurrent": 6, "min_concurrent": 5, "leverage": 1.0, "margin_mode": "isolated"}`
- `RISK = {"max_account_drawdown_pct": 0.30, "daily_loss_limit_usd": 100.0, "position_size_min_usd": 5.0, "position_size_max_pct": 0.20}`
- `UNIVERSE = {"source": "binance_usdtm_perp_top_50", "excluded": []}`
- `RUNTIME = {"polling_interval_seconds": 60, "monitoring_window_hours": 24, "reconciliation_time_utc": "23:00", "entry_time_utc": "06:30"}`
- `DIVERGENCE_ALERT_THRESHOLD_PCT = 0.20`

These are tested by `test_active_spec_schema.py` (post-build) for shape and by
`test_write_active_spec_integration.py` (verifies values flow through).

### 5.2 `hashing.py`

```python
import hashlib, json

def compute_spec_hash(spec_dict: dict) -> str:
    spec_copy = {**spec_dict, "spec_hash": ""}
    canonical = json.dumps(spec_copy, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
```

Byte-for-byte identical to INTERFACE.md §2.3.

**Cross-repo hash compatibility — shared test vector fixture**:

The whole purpose of this function is interop with the engine. A
self-pinned golden vector in MHDE alone proves only that MHDE is
self-consistent — it does not prove the engine produces the same hash
from the same input. To enforce real compatibility:

- **Single source of truth**: a JSON fixture file at
  `<engine-repo>/tests/fixtures/specs/hash_test_vectors_v1.json`
  containing an array of `{input, expected_hash}` pairs.
- **Engine repo**: `engine/tests/unit/spec/test_hash.py` reads the
  fixture and asserts `compute_spec_hash(input) == expected_hash` for
  each pair. (Engine-repo update is out of scope for this session;
  see §10.)
- **MHDE repo**: `tests/crypto/exports/test_hashing.py` resolves the
  fixture path via the `MHDE_ENGINE_REPO` environment variable
  (default `/home/jpcg/crypto-trading-engine`), reads the same JSON,
  asserts the same expected hashes against MHDE's `compute_spec_hash`.
  If the engine repo is not present at the resolved path, the test
  calls `pytest.skip(f"engine repo not found at {path}; cross-repo "
  f"hash fixture unavailable. Set MHDE_ENGINE_REPO or check out "
  f"the engine repo to enable this gate.")`.
- **Path documented in INTERFACE.md §2.4** (engine-repo update — see
  §10) so both sides have a contract-anchored reference.

Fixture file format (proposed for the engine-side commit):

```json
{
  "fixture_version": "1.0",
  "interface_version": "1.0",
  "vectors": [
    {
      "name": "minimal_spec_v1",
      "input": { "spec_hash": "", "spec_version": "1.0.0", "x": 1 },
      "expected_hash": "sha256:<computed once on engine side, pinned here>"
    },
    {
      "name": "full_active_spec_shape",
      "input": { /* a fully-populated spec matching INTERFACE.md §2 example */ },
      "expected_hash": "sha256:<computed once, pinned>"
    },
    {
      "name": "unicode_field",
      "input": { "spec_hash": "", "name": "café", "spec_version": "1.0.0" },
      "expected_hash": "sha256:<computed once, pinned>"
    }
  ]
}
```

`test_hashing.py` test list:
- **Cross-repo fixture parity** (the gate): for each vector in the
  shared fixture, `compute_spec_hash(input) == expected_hash`. Skips
  with clear message if engine repo not present.
- Idempotency: hashing the same dict twice returns the same string.
- Field substitution: `compute_spec_hash({...})` returns the same
  value regardless of any pre-existing `spec_hash` field in the input.
- Sensitivity: changing any non-`spec_hash` field changes the SHA.
- Format: result starts with `sha256:` and the hex portion is 64 chars.

Note: the parity test is the only one that could be silently bypassed
in CI environments without the engine checkout. The other four are
unconditional. So even in a CI without the engine repo, MHDE's hash
function is verified for self-consistency; cross-repo compatibility
is verified locally and in coordinated-CI runs that have both repos.

### 5.3 `_io.py`

Two helpers:

```python
def atomic_write_json(path: Path, obj) -> None:
    """Write json to <path>.tmp.<pid>, fsync, os.replace to path."""

def atomic_replace_symlink(link_path: Path, target_name: str) -> None:
    """Create symlink at <link_path>.tmp.<pid> -> target_name, os.replace
    to link_path. POSIX-atomic on same filesystem."""
```

`test_io_atomic.py`:
- Concurrent writes don't produce partial files.
- Replacing an existing symlink is silent (no error, points at new target).
- Replacing an existing non-symlink file with a symlink is allowed (initial bootstrap).

### 5.4 `write_active_spec.py`

```python
def build_spec(conn) -> dict
def write(conn, output_path: Path = ACTIVE_SPEC_PATH, dry_run: bool = False) -> dict
```

Flow of `build_spec`:
1. SELECT row from `crypto_backtest_summary` and `crypto_backtest_runs` for
   `PHASE1B_WINNER_RUN_ID`. Raise on missing.
2. Parse `parameters` JSON → extract `policy_params.trail_pct` and
   `selection_params.n`.
3. Run `report.simulate_portfolio(conn, run_id=PHASE1B_WINNER_RUN_ID,
   starting_capital=1000.0, max_positions=6, deploy_fraction=0.8,
   leverage=1.0)` (signature confirmed in `crypto/execution/backtest/
   report.py:371`). Returns a `PortfolioResult` dataclass. Map fields to
   `backtest_expectations` as follows.

   **Critical units note (corrected post-implementation):** despite the
   `*_pct` suffix, `PortfolioResult.max_drawdown_pct`,
   `total_return_pct`, and `annualized_return_pct` are all stored as
   **fractions** (e.g., `-0.237` for a 23.7% drawdown). Evidence:
   `report.py:548` computes `dd_pct_series = (eq_series - peak) / peak`
   directly as a ratio; format code at `report.py:626/628/636`
   multiplies each value by 100 for display; the decision-criterion
   threshold at `report.py:70` is `v > -0.25` (fraction form). INTERFACE.md
   §2's example mixes units: `portfolio_max_dd_pct: -0.237` is a fraction,
   but `expected_annualized_return_pct: 21.36` is a percentage value
   (21.36 = 21.36%).

   | INTERFACE.md field | Source field | Unit transform |
   |---|---|---|
   | `portfolio_sharpe` | `result.sharpe_ratio` | passthrough |
   | `portfolio_max_dd_pct` | `result.max_drawdown_pct` | passthrough (already a fraction) |
   | `expected_hit_rate` | `crypto_backtest_summary.hit_rate` | passthrough fraction |
   | `expected_annualized_return_pct` | `result.annualized_return_pct` | multiply by 100 (fraction → percentage value) |
   | `expected_n_trades_per_year` | `result.n_trades_taken / result.span_days × 365` | round to int |
   | `divergence_alert_threshold_pct` | `spec_config.DIVERGENCE_ALERT_THRESHOLD_PCT = 0.20` | passthrough |

   Pinned in `tests/crypto/exports/test_write_active_spec.py` with
   magnitude assertions: a regression that re-introduces `/100` on
   `max_drawdown_pct` would shrink the seed's ≈-0.0395 drawdown to
   ≈-0.000395 and trip `abs(dd) >= 0.01`; a regression that drops
   `*100` on `annualized_return_pct` would shrink ≈12.0 to ≈0.12 and
   trip `abs(annualized) >= 1.0`. Both bugs were caught and fixed in
   commit `2d018fb` against the original implementation `4e043a3`.
4. Read current MHDE git commit SHA via `subprocess.check_output(['git',
   'rev-parse', '--short', 'HEAD'])`. Fall back to `"unknown"` if not in
   a git workdir.
5. Read phase0 status by calling `crypto.ml.phase0_evaluate.evaluate_all(
   conn, engine=CRYPTO_ENGINE, model_id="crypto_10d_db171418")`. Take
   the single returned `Phase0Verdict.overall` value
   (`"PASS"|"FAIL"|"INTERIM"`) and lowercase it to
   `"passed"|"failed"|"interim"` per INTERFACE.md. (Note: `phase0_milestones`
   only stores `last_eta_projection` and `200_reached` checkpoints — the
   terminal verdict is computed dynamically from the data each call.)
6. Assemble dict per INTERFACE.md §2 schema with `spec_hash=""`.
7. Compute hash via `compute_spec_hash`, fill in.
8. Return dict.

Flow of `write`:
1. `dict = build_spec(conn)`.
2. If `dry_run`: print canonical JSON to stdout, return.
3. `atomic_write_json(output_path, dict)`.
4. Log written path + hash + spec_version.

### 5.5 `write_daily_predictions.py`

```python
class ExportPreflightError(Exception): ...

def build_predictions(conn, prediction_date: date | None = None) -> dict
def write(conn, prediction_date: date | None = None,
          output_dir: Path = EXPORTS_DIR, dry_run: bool = False) -> dict
```

**Preflight checks (run inside `build_predictions` before any inference)**:

The export depends on `crypto_ml_features` having a fresh, complete row
set for today. The hard upstream is the `backfill-features` step of
`mhde-crypto-predict.service` (steps 1–5 of its `ExecStart` sequence —
prices/funding/OI/labels/features). The `predict` step (step 6) writes
to `crypto_ml_predictions`, which the export does NOT read; so the
export is robust to step 6 failing but not to steps 1–5.

Two gates, both raise `ExportPreflightError` on failure (CLI catches,
logs to stderr, exits non-zero, leaves all output files and the
symlink untouched):

1. **Staleness gate (strict, today-only)**: `MAX(trade_date) FROM
   crypto_ml_features` must equal `prediction_date` (default = today
   UTC). If older, raise with message naming the actual MAX and
   suggesting `mhde-crypto-predict.service` status check.
2. **Coverage gate (100%)**: count of `(crypto_ml_features f JOIN
   crypto_universe u ON f.symbol=u.symbol WHERE u.is_active=true AND
   f.trade_date = prediction_date)` must equal count of active universe.
   If any active symbol has no feature row, raise listing the missing
   symbols.

When the export aborts in preflight, `predictions_latest.json` keeps
pointing at yesterday's file. The engine independently validates
`export_date == today UTC` per INTERFACE.md §3.2 and follows §5.3
("Predictions File Missing or Stale" → Telegram alert + skip entry
phase). Two layers of defense.

**Date semantics note**: `prediction_date` here is the date used for
both feature inference AND `export_date` in the JSON. The convention in
this codebase (verified empirically: `crypto_ml_features.MAX(trade_date)
= 2026-05-10` at 06:00 UTC on 2026-05-10) is that the daily Binance
candle for date D is ingested and `compute_features` writes a row for
`trade_date = D` by ~00:35 UTC of D. So at 06:15 UTC export-time, the
feature row for today exists and the strict-today gate is the natural
choice.

Flow of `build_predictions`:
1. Resolve `prediction_date` → today UTC if `None`.
1a. **Staleness gate** — raise `ExportPreflightError` if stale.
1b. **Coverage gate** — raise `ExportPreflightError` if any active
    symbol missing features for `prediction_date`.
2. SELECT active 10d model:
   ```sql
   SELECT model_id, horizon, model_path
   FROM crypto_ml_model_runs
   WHERE is_active=true AND horizon='10d'
     AND model_id NOT LIKE 'crypto_%_walkfold_%'
   ```
   Expect exactly 1 row.
3. Fetch features for `prediction_date` from `crypto_ml_features` (same
   shape query as `predict.py:_load_features_for_date`). Restrict to
   `crypto_universe.is_active=true` symbols (so n_predictions is exactly
   universe size).
4. Load the joblib model bundle from `model_path`. Apply Platt calibration
   exactly as `score_universe()` does:
   ```python
   raw = model.predict_proba(X_imputed)[:, 1].reshape(-1, 1)
   cal = platt.predict_proba(raw)[:, 1]
   ```
5. Build `(symbol, probability)` list. Sort descending by probability.
   Assign `rank = idx + 1`.
6. Construct dict per INTERFACE.md §3:
   - `export_date`: `prediction_date.isoformat()`
   - `generated_at`: `datetime.utcnow().isoformat() + "Z"`
   - `model_id`: from query
   - `horizon_days`: 10
   - `n_predictions`: `len(predictions)`
   - `predictions`: ranked list, `predicted_at` = midnight UTC of
     `prediction_date` (deterministic).

Flow of `write`:
1. `dict = build_predictions(...)`.
2. If `dry_run`: print, return.
3. `atomic_write_json(EXPORTS_DIR / f"predictions_{date_iso}.json", dict)`.
4. `atomic_replace_symlink(EXPORTS_DIR / "predictions_latest.json",
   f"predictions_{date_iso}.json")` (relative target so the symlink stays
   valid if `data/exports/` is moved).

## 6. CLI integration

In `main.py` under the existing `crypto` group (next to `crypto-predict`):

```python
@crypto.command("export-spec")
@click.option("--dry-run", is_flag=True)
def crypto_export_spec(dry_run): ...

@crypto.command("export-predictions")
@click.option("--date", default=None, help="YYYY-MM-DD")
@click.option("--dry-run", is_flag=True)
def crypto_export_predictions(date, dry_run): ...
```

Both use the existing `_engine_setup()` connection helper.

## 7. Systemd timer

New unit pair:

```
systemd/mhde-crypto-export-predictions.service
systemd/mhde-crypto-export-predictions.timer
```

Service: `ExecStart=/home/jpcg/MHDE/venv/bin/python /home/jpcg/MHDE/main.py
crypto export-predictions`. `Type=oneshot`. `User=jpcg` (system-level, per
the equity / crypto / fx pattern).

Timer: `OnCalendar=*-*-* 06:15:00 UTC`. `Persistent=true` (catch up after
reboots). `After=mhde-crypto-predict.service`. Schedule rationale:
`mhde-crypto-predict.timer` fires `00:30:00` UTC; the predict service
normally completes in under five minutes; the engine entry phase reads
the file at `06:30:00` UTC. The 5h45m buffer is generous and absorbs
predict-side retries.

**Timer ordering note**: `After=` only sequences when both units are in
the same transaction. Since the timers are independent and 5h45m apart,
`After=` is informational here. The actual dependency on
`crypto_ml_features` freshness is enforced by the preflight gates in
§5.5, NOT by systemd ordering. If `mhde-crypto-predict.service` failed
or didn't fire, the export's staleness gate trips, the timer unit
fails, journald shows the error, and the engine independently sees a
stale `predictions_latest.json` and skips entry per INTERFACE.md §5.3.

`active_spec.json` does NOT get a timer — manual via `crypto export-spec`
after each Phase 1B winner re-derivation.

## 8. Initial run plan

After implementation + tests pass:

1. `mkdir -p data/exports`
2. `venv/bin/python main.py crypto export-spec` → produces
   `data/exports/active_spec.json` from current Phase 1B winner.
3. `venv/bin/python main.py crypto export-predictions` → produces
   `data/exports/predictions_2026-05-10.json` + symlink.
4. Verify hash by re-loading the file and re-computing in a Python REPL
   (in `.claude/local_scripts/`, per project rules).
5. Confirm to user. Engine repo will then be able to read both files.

## 9. Doc updates

- **CLAUDE.md** "Read first" list → insert as item #10:
  > `INTERFACE.md` — file-based contract between MHDE and the
  > crypto-trading-engine (lives at
  > `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md`). The export
  > pipeline at `crypto/exports/` is built to this spec; do not change
  > schemas without coordinating with the engine repo.
- **DECISIONS.md** → new ADR documenting the four design choices in §3
  (predictions source, expectations methodology, risk envelope, static
  config home).
- **OPERATIONS.md** → new section "Engine exports": when to run
  `crypto export-spec`, where the timer for `crypto export-predictions`
  is defined, what to do if `predictions_latest.json` is missing.
- **SESSION_LOG.md** → end-of-session entry covering the design + the
  implementation work.

## 10. Out of scope

- Engine-side validation logic (lives in crypto-trading-engine repo).
- Cross-engine generalisation of the export pipeline (FX/equity will not
  use this contract — only the crypto path goes to live trading per the
  PATH_TO_LIVE_PLAN.md).
- Backwards-compat for spec_version bumps (INTERFACE.md §6 covers
  versioning; this design implements v1.0.0 only).
- Any DB schema migrations. The export reads existing tables; no new
  columns or tables.
- **Engine-repo coordinated changes** — these are required for full
  cross-repo hash gating but are NOT done in this MHDE session:
  1. Create `crypto-trading-engine/tests/fixtures/specs/hash_test_vectors_v1.json`
     with the format specified in §5.2.
  2. Update `crypto-trading-engine/tests/unit/spec/test_hash.py` to
     read the fixture and assert per-vector hash equality (replacing
     or augmenting the current `_ref_hash` helper which only proves
     self-consistency).
  3. Add INTERFACE.md §2.4 documenting the fixture path and format.

  Until those land, MHDE's cross-repo parity test will skip with the
  documented "engine repo not found at $MHDE_ENGINE_REPO" message
  (the fixture file path simply doesn't exist yet). The other four
  hash tests in MHDE remain unconditional, so MHDE-side
  self-consistency is still enforced.

## 11. Test strategy summary

- **Unit**: hashing golden vector + invariants. _io atomic ops.
  spec_config constants shape.
- **Schema**: build_spec output and build_predictions output both pass
  the validation rules in INTERFACE.md §2.2 and §3.2.
- **Integration**: synthetic DB rows + synthetic joblib bundle → both
  writers produce files that re-parse and pass schema tests; symlink
  points at today's file; second run replaces silently.
- **Preflight (write_daily_predictions)**: two negative tests —
    1. seed temp DB with `crypto_ml_features.MAX(trade_date) =
       today - 1 day`, assert `ExportPreflightError` with substring
       "stale" and assert no file written, no symlink modified.
    2. seed temp DB with full features for today minus one active
       universe symbol, assert `ExportPreflightError` with substring
       naming the missing symbol, assert no file written.
   Plus one positive test — full coverage, today's date, succeeds.
- **No live-pipeline coupling**: tests use temp DuckDB and tmp_path.

## 12. Success criteria

1. `tests/crypto/exports/` all green.
2. `tests/regression/test_no_untracked_production_imports.py` still
   green (new files added to git, including the systemd unit pair).
3. `crypto export-spec` produces a file the engine's loader (per
   INTERFACE.md §2.3) verifies hash on.
4. `crypto export-predictions` produces a file with `n_predictions=50`,
   ranks 1..50 consecutive, all probs in [0,1].
5. `predictions_latest.json` resolves to today's file.
6. CLAUDE.md, OPERATIONS.md, DECISIONS.md, SESSION_LOG.md updated.
