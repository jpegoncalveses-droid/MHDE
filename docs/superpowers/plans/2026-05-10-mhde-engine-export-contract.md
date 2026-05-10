# MHDE Engine-Export Contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the MHDE-side production code that produces the two JSON files defined by `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md` — `data/exports/active_spec.json` (rare updates, on Phase 1B re-run) and `data/exports/predictions_YYYY-MM-DD.json` + `predictions_latest.json` symlink (daily, by 06:30 UTC).

**Architecture:** New module tree `crypto/exports/` containing `spec_config.py` (static fields), `hashing.py` (canonical SHA256 per INTERFACE.md §2.3), `_io.py` (atomic write + symlink replace), `write_active_spec.py`, `write_daily_predictions.py`. Two new CLI commands under existing `crypto` group in `main.py`. One new systemd timer for the daily export. The daily writer re-scores the full 50-coin universe via the active 10d model bundle (does NOT read `crypto_ml_predictions`, which is filtered) and enforces strict freshness + 100% coverage preflight gates.

**Tech Stack:** Python 3.11+, DuckDB, joblib, sklearn (already used by predict.py), Click, pytest, systemd. No new dependencies.

**Stop conditions:** test failure that cannot be fixed safely | scoring logic modified | DB schema modified | secret exposed | `tests/regression/test_no_untracked_production_imports.py` regresses

**Source spec:** `docs/superpowers/specs/2026-05-10-mhde-engine-export-contract-design.md` (commits `c057bea`, `29fa9e2`, `4bf6a29`).

---

## Context

The crypto-trading-engine repo at `/home/jpcg/crypto-trading-engine/` consumes two JSON files from this repo. INTERFACE.md is the contract; both repos must respect it. Engine-repo source of record:

- `crypto-trading-engine/docs/INTERFACE.md` — the contract.
- `crypto-trading-engine/engine/spec/hash.py` — reference hash implementation. MHDE's `compute_spec_hash()` must produce byte-identical output.
- `crypto-trading-engine/tests/fixtures/specs/hash_test_vectors_v1.json` — shared test fixture (NOT YET CREATED; engine-side commit lands separately). MHDE's parity test skips with a clear message until that fixture exists.

**Phase 1B winner row (verified in DB on 2026-05-10):**

- `crypto_backtest_summary.run_id = 'backtest_10d_D_top_n_a02e15a0'`
- horizon `10d`, exit_policy `D`, selection_rule `top_n`
- `parameters = {"policy_params":{"trail_pct":0.3},"selection_params":{"n":6}}`
- `date_start=2025-04-05`, `date_end=2026-05-07`
- summary metrics (sum-of-fractions, ranking-only): sharpe 6.32, max_dd_pct -0.170, hit_rate 0.871

**Active 10d model (verified):** `crypto_ml_model_runs.model_id = 'crypto_10d_db171418'`, `is_active=true`, `target_threshold=0.1`.

**Active universe:** 50 coins in `crypto_universe WHERE is_active=true`.

**Existing codebase patterns to follow:**

- `temp_db` fixture in `tests/conftest.py` — in-memory DuckDB with all schemas. Note: `crypto_backtest_*` tables are NOT in `crypto.schema.create_all_tables()`; tests that touch them must call `crypto.execution.backtest.harness.ensure_backtest_tables(conn)` first.
- `tests/crypto/test_predict.py` mocks the joblib bundle via `monkeypatch.setattr(predict_mod.joblib, "load", lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}})`. Reuse this pattern for `write_daily_predictions.py` integration tests.
- `main.py` CLI uses the `_engine_setup()` helper to open a DB connection. `crypto` Click group sits at lines 1614+.
- Pre-commit hook runs a 5-file pytest smoke. It must keep passing.

---

## Codebase Orientation

| Area | Path |
|------|------|
| Spec contract | `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md` |
| Reference hash impl | `/home/jpcg/crypto-trading-engine/engine/spec/hash.py` |
| Existing crypto predict | `crypto/ml/predict.py:score_universe`, `_load_features_for_date`, `_get_active_models` |
| Phase 1B simulate_portfolio | `crypto/execution/backtest/report.py:371` (`simulate_portfolio(conn, run_id, *, starting_capital=1000, max_positions=6, deploy_fraction=0.8, leverage=1.0) -> PortfolioResult`) |
| Phase 0 verdict | `crypto/ml/phase0_evaluate.py:evaluate_all(conn, engine, model_id) -> list[Phase0Verdict]` (`overall: Literal["PASS","FAIL","INTERIM"]`) |
| Phase 0 engine config constant | `crypto/ml/phase0_evaluate.py:CRYPTO_ENGINE` (existing module-level singleton) |
| Backtest tables (not in crypto.schema) | `crypto/execution/backtest/harness.py:ensure_backtest_tables(conn)` |
| Feature columns | `crypto.config.FEATURE_COLS` |
| CLI insertion point | `main.py:1614` (`@cli.group() def crypto():`) — new commands go after the existing `crypto-predict` block |
| Existing crypto timer | `systemd/mhde-crypto-predict.timer` (00:30 UTC daily); new timer fires 06:15 UTC |
| Universal exit gates | `tests/regression/test_no_untracked_production_imports.py`, full `tests/regression/` suite, clean working tree, SESSION_LOG.md updated |
| Project rules | `.claude/CLAUDE.md` — no `python -c` blocks; multi-line Python goes to `.claude/local_scripts/` |

---

## File Map

### Created

- `crypto/exports/__init__.py`
- `crypto/exports/spec_config.py` — static spec field constants + `PHASE1B_WINNER_RUN_ID`
- `crypto/exports/hashing.py` — `compute_spec_hash()` (verbatim INTERFACE.md §2.3)
- `crypto/exports/_io.py` — `atomic_write_json`, `atomic_replace_symlink`, `EXPORTS_DIR` path
- `crypto/exports/write_active_spec.py` — `build_spec`, `write`, `ExportSpecError`
- `crypto/exports/write_daily_predictions.py` — `build_predictions`, `write`, `ExportPreflightError`
- `tests/crypto/exports/__init__.py`
- `tests/crypto/exports/test_hashing.py`
- `tests/crypto/exports/test_io_atomic.py`
- `tests/crypto/exports/test_spec_config.py`
- `tests/crypto/exports/test_write_active_spec.py`
- `tests/crypto/exports/test_write_daily_predictions.py`
- `systemd/mhde-crypto-export-predictions.service`
- `systemd/mhde-crypto-export-predictions.timer`

### Modified

- `main.py` — add `crypto export-spec` and `crypto export-predictions` commands
- `.gitignore` — add `data/exports/`
- `CLAUDE.md` — add INTERFACE.md to read-first list
- `DECISIONS.md` — new ADR for the four design choices
- `OPERATIONS.md` — new "Engine exports" runbook section
- `SESSION_LOG.md` — append new session entry

### Produced (operational artifacts, gitignored)

- `data/exports/active_spec.json`
- `data/exports/predictions_2026-05-10.json`
- `data/exports/predictions_latest.json` (symlink → predictions_2026-05-10.json)

---

# Phase 1: Foundation modules (TDD inside)

## Task 1: scaffold `crypto/exports/` package

**Files:**
- Create: `crypto/exports/__init__.py`

- [ ] **Step 1: Create the package**

```python
# crypto/exports/__init__.py
"""MHDE → crypto-trading-engine file-based export contract.

See docs/superpowers/specs/2026-05-10-mhde-engine-export-contract-design.md
and /home/jpcg/crypto-trading-engine/docs/INTERFACE.md for the contract.
"""
```

- [ ] **Step 2: Create the test package**

```python
# tests/crypto/exports/__init__.py
```

- [ ] **Step 3: Verify nothing collects yet but the structure imports**

Run: `venv/bin/python -c "import crypto.exports"` — wait, NO. Write a script per project rules.

Write `.claude/local_scripts/scratch_import_exports.py`:

```python
import crypto.exports
print("ok")
```

Run: `venv/bin/python .claude/local_scripts/scratch_import_exports.py`
Expected: `ok`

Then delete it: `rm .claude/local_scripts/scratch_import_exports.py`

- [ ] **Step 4: Commit**

```bash
git add crypto/exports/__init__.py tests/crypto/exports/__init__.py
git commit -m "feat(exports): scaffold crypto/exports package"
```

---

## Task 2: `hashing.py` — TDD with cross-repo parity hook

**Files:**
- Create: `crypto/exports/hashing.py`
- Create: `tests/crypto/exports/test_hashing.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto/exports/test_hashing.py
"""Tests for crypto.exports.hashing.compute_spec_hash.

The function is byte-for-byte identical to INTERFACE.md §2.3 and to the
engine-side reference at crypto-trading-engine/engine/spec/hash.py.

The cross-repo parity test reads a shared fixture from the engine repo;
it pytest.skip's with a clear message when the engine repo isn't checked
out at the resolved path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from crypto.exports.hashing import compute_spec_hash


def _ref(spec):
    """Local reference computation — matches INTERFACE.md §2.3 exactly."""
    spec_copy = {**spec, "spec_hash": ""}
    canonical = json.dumps(spec_copy, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def test_idempotent():
    spec = {"spec_hash": "", "spec_version": "1.0.0", "x": 1}
    assert compute_spec_hash(spec) == compute_spec_hash(spec)


def test_independent_of_existing_spec_hash_value():
    base = {"spec_version": "1.0.0", "x": 1}
    a = {**base, "spec_hash": "sha256:aaa"}
    b = {**base, "spec_hash": "sha256:bbb"}
    c = {**base, "spec_hash": ""}
    assert compute_spec_hash(a) == compute_spec_hash(b) == compute_spec_hash(c)


def test_changes_when_other_field_changes():
    a = {"spec_hash": "x", "spec_version": "1.0.0"}
    b = {"spec_hash": "x", "spec_version": "1.0.1"}
    assert compute_spec_hash(a) != compute_spec_hash(b)


def test_format_is_sha256_prefixed_64_hex():
    h = compute_spec_hash({"spec_hash": "", "x": 1})
    assert h.startswith("sha256:")
    hex_part = h.removeprefix("sha256:")
    assert len(hex_part) == 64
    assert all(c in "0123456789abcdef" for c in hex_part)


def test_independent_of_input_dict_key_order():
    a = {"spec_hash": "", "alpha": 1, "beta": 2, "gamma": {"z": 1, "a": 2}}
    b = {"gamma": {"a": 2, "z": 1}, "beta": 2, "alpha": 1, "spec_hash": ""}
    assert compute_spec_hash(a) == compute_spec_hash(b)


def test_unicode_field_hashed_deterministically():
    a = {"spec_hash": "", "name": "café", "spec_version": "1.0.0"}
    expected = _ref(a)
    assert compute_spec_hash(a) == expected


def test_matches_local_reference_implementation():
    """Local sanity gate — same algorithm written twice must agree."""
    spec = {
        "spec_hash": "sha256:placeholder",
        "spec_version": "1.0.0",
        "phase_1b_winner": {"run_id": "backtest_10d_D_top_n_a02e15a0", "n": 6},
        "x": [3, 1, 2],
    }
    assert compute_spec_hash(spec) == _ref(spec)


def test_cross_repo_parity_with_engine_fixture():
    """Cross-repo gate — read shared fixture from engine repo, assert
    every (input, expected_hash) pair matches MHDE's compute_spec_hash.

    Skips with a clear message when the engine repo isn't present at
    the resolved path (e.g., MHDE-only CI). When both repos are checked
    out (dev box, coordinated CI) this is the single test that proves
    the contract holds across the boundary.
    """
    engine_repo = Path(os.environ.get(
        "MHDE_ENGINE_REPO", "/home/jpcg/crypto-trading-engine"
    ))
    fixture = engine_repo / "tests" / "fixtures" / "specs" / "hash_test_vectors_v1.json"
    if not fixture.exists():
        pytest.skip(
            f"engine repo not found at {engine_repo}; cross-repo hash "
            f"fixture unavailable. Set MHDE_ENGINE_REPO or check out the "
            f"engine repo to enable this gate."
        )

    payload = json.loads(fixture.read_text())
    assert payload.get("fixture_version") == "1.0", \
        f"unexpected fixture_version: {payload.get('fixture_version')!r}"
    assert payload.get("interface_version") == "1.0", \
        f"unexpected interface_version: {payload.get('interface_version')!r}"
    vectors = payload.get("vectors", [])
    assert vectors, "fixture has no vectors"

    failures = []
    for v in vectors:
        got = compute_spec_hash(v["input"])
        if got != v["expected_hash"]:
            failures.append(f"{v['name']}: expected {v['expected_hash']}, got {got}")
    assert not failures, "cross-repo hash mismatch:\n" + "\n".join(failures)
```

- [ ] **Step 2: Run tests, verify all FAIL with ImportError**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_hashing.py -v 2>&1 | tail -30`
Expected: every test errors with `ModuleNotFoundError: No module named 'crypto.exports.hashing'`.

- [ ] **Step 3: Implement `hashing.py`**

```python
# crypto/exports/hashing.py
"""Canonical SHA256 of a strategy spec dict.

Byte-for-byte identical to /home/jpcg/crypto-trading-engine/engine/spec/
hash.py and to INTERFACE.md §2.3. Do NOT modify the canonicalization
without coordinating with the engine repo and updating INTERFACE.md.
"""
from __future__ import annotations

import hashlib
import json


def compute_spec_hash(spec_dict: dict) -> str:
    """Return ``sha256:<hex>`` over a deterministic byte stream of the
    spec, with the ``spec_hash`` field substituted by the empty string.

    The canonicalization is fixed: ``sort_keys=True``, compact
    separators ``(",", ":")``, default ``ensure_ascii=True``,
    ``utf-8`` encoding. See INTERFACE.md §2.3.
    """
    spec_copy = {**spec_dict, "spec_hash": ""}
    canonical = json.dumps(spec_copy, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
```

- [ ] **Step 4: Re-run tests, verify all PASS (parity test SKIPS)**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_hashing.py -v 2>&1 | tail -30`
Expected: 7 passed, 1 skipped (the parity test, with the documented "engine repo not found at /home/jpcg/crypto-trading-engine" message — except the engine repo IS at that path in the dev environment, but the fixture file doesn't exist yet).

If running on the dev box where `/home/jpcg/crypto-trading-engine/` exists but `tests/fixtures/specs/hash_test_vectors_v1.json` does not exist (current state), the test will skip. If both exist, the test passes. Either is acceptable; failure is not.

- [ ] **Step 5: Commit**

```bash
git add crypto/exports/hashing.py tests/crypto/exports/test_hashing.py
git commit -m "feat(exports): canonical spec hash matching INTERFACE.md §2.3

Byte-identical to crypto-trading-engine/engine/spec/hash.py.
Cross-repo parity test reads a shared fixture from the engine
repo and skips with a clear message when the engine repo isn't
present (or the fixture file hasn't been created yet on the
engine side)."
```

---

## Task 3: `_io.py` — atomic file ops

**Files:**
- Create: `crypto/exports/_io.py`
- Create: `tests/crypto/exports/test_io_atomic.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto/exports/test_io_atomic.py
"""Tests for crypto.exports._io — atomic JSON writes and symlink replace.

POSIX-atomic ops via os.replace on the same filesystem. Tested by:
- producing a complete file on the destination path (no partial reads)
- replacing an existing symlink silently
- replacing an existing regular file with a symlink (initial bootstrap)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto.exports._io import atomic_write_json, atomic_replace_symlink


def test_atomic_write_json_creates_file(tmp_path):
    dst = tmp_path / "out.json"
    atomic_write_json(dst, {"a": 1, "b": [2, 3]})
    assert dst.exists()
    assert json.loads(dst.read_text()) == {"a": 1, "b": [2, 3]}


def test_atomic_write_json_overwrites_existing(tmp_path):
    dst = tmp_path / "out.json"
    dst.write_text('{"old": true}')
    atomic_write_json(dst, {"new": True})
    assert json.loads(dst.read_text()) == {"new": True}


def test_atomic_write_json_no_temp_file_left_behind(tmp_path):
    dst = tmp_path / "out.json"
    atomic_write_json(dst, {"x": 1})
    leftover = list(tmp_path.glob("out.json.tmp.*"))
    assert leftover == [], f"temp files leaked: {leftover}"


def test_atomic_replace_symlink_creates_new_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "latest.json"
    atomic_replace_symlink(link, "target.json")
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_atomic_replace_symlink_replaces_existing_symlink(tmp_path):
    a = tmp_path / "a.json"
    a.write_text('{"v": "a"}')
    b = tmp_path / "b.json"
    b.write_text('{"v": "b"}')
    link = tmp_path / "latest.json"
    atomic_replace_symlink(link, "a.json")
    atomic_replace_symlink(link, "b.json")
    assert link.is_symlink()
    assert json.loads(link.read_text()) == {"v": "b"}


def test_atomic_replace_symlink_replaces_regular_file(tmp_path):
    """Initial bootstrap case: a regular file at the symlink path is
    replaced silently."""
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "latest.json"
    link.write_text('{"old_regular_file": true}')
    atomic_replace_symlink(link, "target.json")
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_atomic_replace_symlink_uses_relative_target(tmp_path):
    """Symlink target is stored as the relative name passed in, not
    an absolute path. This keeps the link valid if data/exports/ is
    moved or rsync'd."""
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "latest.json"
    atomic_replace_symlink(link, "target.json")
    assert link.readlink() == Path("target.json")
```

- [ ] **Step 2: Run tests, verify all FAIL with ImportError**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_io_atomic.py -v 2>&1 | tail -20`
Expected: ModuleNotFoundError on `crypto.exports._io`.

- [ ] **Step 3: Implement `_io.py`**

```python
# crypto/exports/_io.py
"""Atomic file operations for the export pipeline.

All ops use ``os.replace`` for POSIX-atomic rename on the same
filesystem. Failure modes (disk full, permission denied) bubble up;
caller is responsible for not modifying outputs on preflight failure.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

EXPORTS_DIR = Path("/home/jpcg/MHDE/data/exports")


def atomic_write_json(path: Path, obj) -> None:
    """Write ``obj`` as JSON to ``path`` atomically.

    Strategy: write to ``<path>.tmp.<pid>``, fsync, ``os.replace`` to
    the final path. Concurrent readers either see the old file or the
    new file, never a partial write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_replace_symlink(link_path: Path, target_name: str) -> None:
    """Atomically (re)create a symlink at ``link_path`` pointing at
    ``target_name`` (relative).

    Strategy: ``os.symlink`` to a temp link, ``os.replace`` to the
    final link path. Replaces existing symlinks AND existing regular
    files silently (the latter for initial bootstrap).
    """
    link_path = Path(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = link_path.with_name(f"{link_path.name}.tmp.{os.getpid()}")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target_name, tmp)
        os.replace(tmp, link_path)
    finally:
        if tmp.is_symlink() or tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
```

- [ ] **Step 4: Re-run tests, verify all PASS**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_io_atomic.py -v 2>&1 | tail -20`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add crypto/exports/_io.py tests/crypto/exports/test_io_atomic.py
git commit -m "feat(exports): atomic_write_json + atomic_replace_symlink

POSIX-atomic write via tmp + os.replace. Symlink replace handles
both existing-symlink and existing-regular-file (initial bootstrap)
cases. Symlink targets are stored as relative names so links remain
valid if data/exports/ is moved."
```

---

## Task 4: `spec_config.py` — static field constants

**Files:**
- Create: `crypto/exports/spec_config.py`
- Create: `tests/crypto/exports/test_spec_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto/exports/test_spec_config.py
"""Tests for crypto.exports.spec_config — shape and value invariants
of the static spec fields.

The Phase 1B winner run_id is hardcoded here; updates require an
explicit code edit + commit. Risk envelope values come from
INTERFACE.md §2 example (see DECISIONS.md ADR for justification).
"""
from __future__ import annotations

from crypto.exports import spec_config as sc


def test_spec_version_is_semver_string():
    assert sc.SPEC_VERSION == "1.0.0"


def test_phase1b_winner_run_id_pinned():
    assert sc.PHASE1B_WINNER_RUN_ID == "backtest_10d_D_top_n_a02e15a0"


def test_sizing_invariants():
    s = sc.SIZING
    assert s["deploy_pct"] + s["reserve_pct"] == 1.0
    assert s["leverage"] in (1.0, 2.0)
    assert s["max_concurrent"] >= s["min_concurrent"]
    assert s["margin_mode"] == "isolated"


def test_risk_values_match_interface_example():
    r = sc.RISK
    assert r["max_account_drawdown_pct"] == 0.30
    assert r["daily_loss_limit_usd"] == 100.0
    assert r["position_size_min_usd"] == 5.0
    assert r["position_size_max_pct"] == 0.20


def test_runtime_values():
    rt = sc.RUNTIME
    assert rt["polling_interval_seconds"] >= 30
    assert rt["entry_time_utc"] == "06:30"
    assert rt["reconciliation_time_utc"] == "23:00"


def test_universe_source_label():
    u = sc.UNIVERSE
    assert u["source"] == "binance_usdtm_perp_top_50"
    assert u["excluded"] == []


def test_divergence_alert_threshold():
    assert sc.DIVERGENCE_ALERT_THRESHOLD_PCT == 0.20
```

- [ ] **Step 2: Run tests, verify FAIL with ImportError**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_spec_config.py -v 2>&1 | tail -20`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `spec_config.py`**

```python
# crypto/exports/spec_config.py
"""Static spec fields for active_spec.json.

Phase 1B-derived fields (run_id, trail_pct, n, horizon, expectations)
are read from DB at spec-generation time; everything else lives here.
Phase 1B re-runs require an explicit edit of PHASE1B_WINNER_RUN_ID
plus a commit.

Risk envelope values are adopted from INTERFACE.md §2 example for
$1k Phase 2 paper trading. Revisit at the Phase 3 → 4 transition.
See DECISIONS.md.
"""
from __future__ import annotations

SPEC_VERSION = "1.0.0"

PHASE1B_WINNER_RUN_ID = "backtest_10d_D_top_n_a02e15a0"

SIZING = {
    "deploy_pct": 0.80,
    "reserve_pct": 0.20,
    "max_concurrent": 6,
    "min_concurrent": 5,
    "leverage": 1.0,
    "margin_mode": "isolated",
}

RISK = {
    "max_account_drawdown_pct": 0.30,
    "daily_loss_limit_usd": 100.0,
    "position_size_min_usd": 5.0,
    "position_size_max_pct": 0.20,
}

UNIVERSE = {
    "source": "binance_usdtm_perp_top_50",
    "excluded": [],
}

RUNTIME = {
    "polling_interval_seconds": 60,
    "monitoring_window_hours": 24,
    "reconciliation_time_utc": "23:00",
    "entry_time_utc": "06:30",
}

DIVERGENCE_ALERT_THRESHOLD_PCT = 0.20
```

- [ ] **Step 4: Re-run tests, verify PASS**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_spec_config.py -v 2>&1 | tail -20`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add crypto/exports/spec_config.py tests/crypto/exports/test_spec_config.py
git commit -m "feat(exports): spec_config static fields + Phase 1B winner pin

Hardcodes PHASE1B_WINNER_RUN_ID = backtest_10d_D_top_n_a02e15a0;
Phase 1B re-runs require an explicit edit + commit. Risk envelope
values from INTERFACE.md §2 example. Sizing/runtime/universe per
PATH_TO_LIVE_PLAN.md locked decisions."
```

---

# CHECKPOINT 1 — Foundation modules complete

**Pause here for review.** Verify:

- [ ] `venv/bin/python -m pytest tests/crypto/exports/ -v 2>&1 | tail -20` shows ~21 tests, all passing or skipping (the parity test).
- [ ] No untracked files, clean working tree.
- [ ] Three commits since the start of this plan.

Resume with **Phase 2** after operator approval.

---

# Phase 2: Active spec writer

## Task 5: `write_active_spec.py` — TDD with synthetic DB

**Files:**
- Create: `crypto/exports/write_active_spec.py`
- Create: `tests/crypto/exports/test_write_active_spec.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto/exports/test_write_active_spec.py
"""Tests for crypto.exports.write_active_spec.

Schema-conformance against INTERFACE.md §2 + integration with
synthetic crypto_backtest_summary / crypto_backtest_runs /
crypto_backtest_trades rows so simulate_portfolio runs end-to-end.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from crypto.exports import write_active_spec, spec_config
from crypto.exports.hashing import compute_spec_hash
from crypto.execution.backtest.harness import ensure_backtest_tables


def _seed_phase1b_winner(conn):
    """Insert the rows write_active_spec needs.

    A small set of trades (10) is enough for simulate_portfolio to
    return a non-degenerate PortfolioResult.
    """
    ensure_backtest_tables(conn)
    run_id = spec_config.PHASE1B_WINNER_RUN_ID
    conn.execute(
        "INSERT INTO crypto_backtest_runs ("
        "  run_id, horizon, exit_policy, selection_rule, parameters,"
        "  date_start, date_end, n_trades"
        ") VALUES (?, '10d', 'D', 'top_n', "
        "  '{\"policy_params\":{\"trail_pct\":0.3},\"selection_params\":{\"n\":6}}',"
        "  DATE '2025-04-05', DATE '2026-05-07', 10)",
        [run_id],
    )
    conn.execute(
        "INSERT INTO crypto_backtest_summary ("
        "  run_id, net_pnl_total_pct, net_pnl_annualized_pct, sharpe_ratio,"
        "  max_drawdown_pct, hit_rate, profit_factor, avg_holding_days,"
        "  pct_exits_trailing"
        ") VALUES (?, 51.2, 47.0, 6.32, -0.17, 0.871, 3.13, 3.66, 0.87)",
        [run_id],
    )
    # Seed 10 winners for simulate_portfolio to get past its empty-trades
    # branch. Exact values aren't asserted; just that PortfolioResult
    # comes back non-degenerate.
    for i in range(10):
        conn.execute(
            "INSERT INTO crypto_backtest_trades ("
            "  run_id, trade_id, coin, entry_date, entry_price,"
            "  exit_date, exit_price, exit_reason, holding_days,"
            "  net_pnl_pct, probability_at_entry"
            ") VALUES (?, ?, 'BTCUSDT',"
            "  DATE '2025-04-05', 60000.0,"
            "  DATE '2025-04-15', 63000.0, 'trailing', 10,"
            "  0.05, 0.80)",
            [run_id, f"t{i}"],
        )


def test_build_spec_includes_all_required_top_level_fields(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    required = {
        "spec_version", "spec_hash", "generated_at", "generated_by_mhde_commit",
        "phase_0_status", "phase_1b_winner", "sizing", "risk", "universe",
        "runtime", "backtest_expectations",
    }
    assert required <= set(spec.keys())


def test_build_spec_phase1b_winner_pulled_from_db(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    w = spec["phase_1b_winner"]
    assert w["run_id"] == spec_config.PHASE1B_WINNER_RUN_ID
    assert w["horizon_days"] == 10
    assert w["exit_policy"] == "D"
    assert w["selection_mode"] == "top_n"
    assert w["selection_n"] == 6
    assert w["trail_pct"] == 0.30
    assert w["activation_pct"] == 0.01


def test_build_spec_sizing_passes_validation(temp_db):
    _seed_phase1b_winner(temp_db)
    s = write_active_spec.build_spec(temp_db)["sizing"]
    assert s["deploy_pct"] + s["reserve_pct"] == 1.0
    assert s["leverage"] in (1.0, 2.0)


def test_build_spec_risk_values_match_config(temp_db):
    _seed_phase1b_winner(temp_db)
    r = write_active_spec.build_spec(temp_db)["risk"]
    assert r == spec_config.RISK


def test_build_spec_phase_0_status_defaults_to_interim(temp_db):
    """No phase0 verdict computed (no outcome-filled predictions) →
    evaluate_all returns INTERIM → lowercased to 'interim'."""
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    assert spec["phase_0_status"] == "interim"


def test_build_spec_hash_is_self_consistent(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    declared = spec["spec_hash"]
    recomputed = compute_spec_hash(spec)
    assert declared == recomputed


def test_build_spec_backtest_expectations_pulled_from_simulate_portfolio(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    e = spec["backtest_expectations"]
    # All seeded trades are winners (+5%) → portfolio Sharpe is finite
    # and positive, max_dd_pct == 0 (no drawdown), n_trades == 10.
    assert e["portfolio_sharpe"] > 0
    assert e["portfolio_max_dd_pct"] <= 0  # fraction (negative or zero)
    assert e["expected_hit_rate"] == pytest.approx(0.871)
    assert e["expected_n_trades_per_year"] >= 1
    assert e["divergence_alert_threshold_pct"] == 0.20


def test_build_spec_raises_when_winner_row_missing(temp_db):
    ensure_backtest_tables(temp_db)
    # No insert. PHASE1B_WINNER_RUN_ID has no rows.
    with pytest.raises(write_active_spec.ExportSpecError, match="not found"):
        write_active_spec.build_spec(temp_db)


def test_write_creates_file_with_valid_json_and_hash(temp_db, tmp_path):
    _seed_phase1b_winner(temp_db)
    out = tmp_path / "active_spec.json"
    write_active_spec.write(temp_db, output_path=out)
    payload = json.loads(out.read_text())
    assert payload["spec_hash"] == compute_spec_hash(payload)
    assert payload["spec_version"] == "1.0.0"


def test_write_dry_run_does_not_create_file(temp_db, tmp_path, capsys):
    _seed_phase1b_winner(temp_db)
    out = tmp_path / "active_spec.json"
    write_active_spec.write(temp_db, output_path=out, dry_run=True)
    assert not out.exists()
    captured = capsys.readouterr()
    assert "spec_hash" in captured.out
```

- [ ] **Step 2: Run tests, verify all FAIL with ImportError**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_write_active_spec.py -v 2>&1 | tail -30`
Expected: ModuleNotFoundError on `crypto.exports.write_active_spec`.

- [ ] **Step 3: Implement `write_active_spec.py`**

```python
# crypto/exports/write_active_spec.py
"""Build and write data/exports/active_spec.json per INTERFACE.md §2.

Reads:
  - crypto_backtest_summary, crypto_backtest_runs, crypto_backtest_trades
    (Phase 1B winner — run_id from spec_config.PHASE1B_WINNER_RUN_ID)
  - crypto_ml_predictions (via phase0_evaluate.evaluate_all for verdict)

Does not write to DB. Atomic file write via _io.atomic_write_json.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from crypto.exports import spec_config
from crypto.exports._io import EXPORTS_DIR, atomic_write_json
from crypto.exports.hashing import compute_spec_hash

logger = logging.getLogger("mhde.exports.spec")

ACTIVE_SPEC_PATH = EXPORTS_DIR / "active_spec.json"

# Phase0 model id — the active 10d model the engine cares about.
# Hardcoded because the export is Phase 1B-winner-specific.
PHASE0_MODEL_ID = "crypto_10d_db171418"


class ExportSpecError(Exception):
    """Raised when the spec cannot be built (missing winner row, etc.)."""


def _git_short_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd="/home/jpcg/MHDE", stderr=subprocess.DEVNULL,
        )
        return out.decode("utf-8").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _phase_0_status(conn) -> str:
    """Lowercase the active 10d model's Phase0Verdict.overall.

    Returns one of "passed", "failed", "interim". Engine in live mode
    requires "passed".
    """
    from crypto.ml.phase0_evaluate import evaluate_all, CRYPTO_ENGINE
    verdicts = evaluate_all(conn, engine=CRYPTO_ENGINE, model_id=PHASE0_MODEL_ID)
    if not verdicts:
        return "interim"
    overall = verdicts[0].overall  # "PASS" | "FAIL" | "INTERIM"
    return {"PASS": "passed", "FAIL": "failed", "INTERIM": "interim"}[overall]


def _phase1b_winner_fields(conn) -> dict:
    run_id = spec_config.PHASE1B_WINNER_RUN_ID
    row = conn.execute(
        "SELECT horizon, exit_policy, selection_rule, parameters "
        "FROM crypto_backtest_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        raise ExportSpecError(
            f"Phase 1B winner run_id={run_id} not found in crypto_backtest_runs"
        )
    horizon, exit_policy, selection_rule, params_json = row
    params = json.loads(params_json) if params_json else {}
    policy_params = params.get("policy_params", {}) or {}
    selection_params = params.get("selection_params", {}) or {}

    horizon_days = int(horizon.rstrip("d"))
    winner = {
        "run_id": run_id,
        "horizon_days": horizon_days,
        "exit_policy": exit_policy,
        "selection_mode": selection_rule,
        "trail_pct": float(policy_params.get("trail_pct", 0.30)),
        "activation_pct": float(policy_params.get("activation_pct", 0.01)),
    }
    if selection_rule == "top_n":
        winner["selection_n"] = int(selection_params.get("n", 6))
    elif selection_rule == "threshold":
        winner["selection_threshold"] = float(selection_params.get("threshold", 0.55))
    return winner


def _backtest_expectations(conn) -> dict:
    """Map simulate_portfolio output + summary.hit_rate to the
    INTERFACE.md §2 backtest_expectations fields.

    Unit transforms (pinned by tests; see spec §5.4):
      - portfolio_sharpe         ← result.sharpe_ratio (passthrough)
      - portfolio_max_dd_pct     ← result.max_drawdown_pct / 100 (→ fraction)
      - expected_hit_rate        ← summary.hit_rate (passthrough fraction)
      - expected_annualized_return_pct ← result.annualized_return_pct (percentage)
      - expected_n_trades_per_year     ← round(n_trades_taken / span_days × 365)
    """
    from crypto.execution.backtest.report import simulate_portfolio
    run_id = spec_config.PHASE1B_WINNER_RUN_ID

    result = simulate_portfolio(
        conn, run_id=run_id,
        starting_capital=1000.0, max_positions=6,
        deploy_fraction=0.8, leverage=1.0,
    )
    hit_rate = conn.execute(
        "SELECT hit_rate FROM crypto_backtest_summary WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if hit_rate is None:
        raise ExportSpecError(
            f"Phase 1B winner run_id={run_id} has no row in "
            f"crypto_backtest_summary"
        )
    hit_rate_value = float(hit_rate[0]) if hit_rate[0] is not None else 0.0
    n_trades_per_year = (
        round(result.n_trades_taken / result.span_days * 365)
        if result.span_days > 0 else 0
    )
    return {
        "portfolio_sharpe": float(result.sharpe_ratio),
        "portfolio_max_dd_pct": float(result.max_drawdown_pct) / 100.0,
        "expected_hit_rate": hit_rate_value,
        "expected_annualized_return_pct": float(result.annualized_return_pct),
        "expected_n_trades_per_year": n_trades_per_year,
        "divergence_alert_threshold_pct": spec_config.DIVERGENCE_ALERT_THRESHOLD_PCT,
    }


def build_spec(conn: duckdb.DuckDBPyConnection) -> dict:
    """Assemble the active_spec.json dict, hash-filled."""
    winner = _phase1b_winner_fields(conn)
    expectations = _backtest_expectations(conn)
    phase0 = _phase_0_status(conn)

    spec = {
        "spec_version": spec_config.SPEC_VERSION,
        "spec_hash": "",  # filled below
        "generated_at": datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "generated_by_mhde_commit": _git_short_sha(),
        "phase_0_status": phase0,
        "phase_1b_winner": winner,
        "sizing": dict(spec_config.SIZING),
        "risk": dict(spec_config.RISK),
        "universe": dict(spec_config.UNIVERSE),
        "runtime": dict(spec_config.RUNTIME),
        "backtest_expectations": expectations,
    }
    spec["spec_hash"] = compute_spec_hash(spec)
    return spec


def write(
    conn: duckdb.DuckDBPyConnection,
    output_path: Path = ACTIVE_SPEC_PATH,
    dry_run: bool = False,
) -> dict:
    spec = build_spec(conn)
    if dry_run:
        print(json.dumps(spec, indent=2, sort_keys=True))
        return spec
    atomic_write_json(output_path, spec)
    logger.info(
        "wrote %s (spec_hash=%s, version=%s)",
        output_path, spec["spec_hash"], spec["spec_version"],
    )
    return spec
```

- [ ] **Step 4: Re-run tests, verify all PASS**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_write_active_spec.py -v 2>&1 | tail -30`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add crypto/exports/write_active_spec.py tests/crypto/exports/test_write_active_spec.py
git commit -m "feat(exports): write_active_spec — INTERFACE.md §2 schema

Pulls phase_1b_winner from crypto_backtest_runs, computes
backtest_expectations via report.simulate_portfolio (portfolio-
realistic methodology), reads phase_0_status from
phase0_evaluate.evaluate_all (lowercased). Hash-fills via
crypto.exports.hashing. Atomic write."
```

---

# CHECKPOINT 2 — Active spec writer complete

**Pause here for review.** Verify:

- [ ] `venv/bin/python -m pytest tests/crypto/exports/ -v 2>&1 | tail -30` shows ~31 tests, passing/skipping.
- [ ] `tests/regression/test_no_untracked_production_imports.py` passes.

Resume with **Phase 3** after operator approval.

---

# Phase 3: Daily predictions writer

## Task 6: `write_daily_predictions.py` — preflight gates first

**Files:**
- Create: `crypto/exports/write_daily_predictions.py`
- Create: `tests/crypto/exports/test_write_daily_predictions.py`

- [ ] **Step 1: Write the failing tests (preflight + happy-path + integration)**

```python
# tests/crypto/exports/test_write_daily_predictions.py
"""Tests for crypto.exports.write_daily_predictions.

Preflight:
  - staleness gate (MAX(trade_date) < today UTC → error)
  - coverage gate (any active universe symbol missing → error)
  - happy path (full coverage, today UTC → success)

Schema integration:
  - n_predictions == count(active universe)
  - ranks 1..N consecutive
  - probabilities sorted descending
  - all probabilities in [0, 1]
  - export_date matches prediction_date
  - symlink points at the dated file

Joblib bundle is mocked via monkeypatch (same pattern as
tests/crypto/test_predict.py).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from crypto.config import FEATURE_COLS
from crypto.exports import write_daily_predictions as wdp


# ──────────────────────────────────────────────────────────────────────
# Fixture helpers
# ──────────────────────────────────────────────────────────────────────


def _seed_universe(conn, symbols):
    for sym in symbols:
        conn.execute(
            "INSERT INTO crypto_universe (symbol, base_asset, is_active, "
            "rank_by_volume) VALUES (?, ?, true, 1)",
            [sym, sym.removesuffix("USDT")],
        )


def _seed_active_10d_model(conn, model_path="/tmp/fake.joblib"):
    conn.execute(
        "INSERT INTO crypto_ml_model_runs ("
        "  model_id, horizon, target_threshold, model_path, is_active"
        ") VALUES ('crypto_10d_test', '10d', 0.10, ?, true)",
        [model_path],
    )


def _seed_features(conn, symbols, trade_date):
    cols = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    for sym in symbols:
        conn.execute(
            f"INSERT INTO crypto_ml_features (symbol, trade_date, {cols}) "
            f"VALUES (?, ?, {placeholders})",
            [sym, trade_date] + [0.0] * len(FEATURE_COLS),
        )


def _mock_joblib_load(monkeypatch, probs_per_call):
    """Replace joblib.load to return a bundle whose model returns the
    given probabilities (list of floats, in feature-row order)."""
    fake_model = MagicMock()
    arr = np.array([[1 - p, p] for p in probs_per_call])
    fake_model.predict_proba = lambda X: arr
    fake_platt = MagicMock()
    fake_platt.predict_proba = lambda raw: arr
    monkeypatch.setattr(
        wdp.joblib, "load",
        lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}},
    )


# ──────────────────────────────────────────────────────────────────────
# Preflight tests
# ──────────────────────────────────────────────────────────────────────


def test_preflight_fails_when_features_stale(temp_db):
    """MAX(trade_date) = yesterday → ExportPreflightError('stale')."""
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], yesterday)

    with pytest.raises(wdp.ExportPreflightError, match="stale"):
        wdp.build_predictions(temp_db, prediction_date=today)


def test_preflight_fails_when_features_missing_for_symbol(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)  # ETHUSDT missing

    with pytest.raises(wdp.ExportPreflightError, match="ETHUSDT"):
        wdp.build_predictions(temp_db, prediction_date=today)


def test_preflight_passes_with_full_today_coverage(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7, 0.6])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    assert out["n_predictions"] == 2


# ──────────────────────────────────────────────────────────────────────
# Schema tests (full coverage, mocked model)
# ──────────────────────────────────────────────────────────────────────


def test_predictions_full_universe_ranked(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, today)
    _mock_joblib_load(monkeypatch, [0.50, 0.91, 0.30, 0.75, 0.10])

    out = wdp.build_predictions(temp_db, prediction_date=today)

    assert out["export_date"] == today.isoformat()
    assert out["n_predictions"] == 5
    assert out["model_id"] == "crypto_10d_test"
    assert out["horizon_days"] == 10

    preds = out["predictions"]
    assert len(preds) == 5
    # Sorted descending by probability
    probs = [p["probability"] for p in preds]
    assert probs == sorted(probs, reverse=True)
    # Ranks consecutive 1..5
    assert [p["rank"] for p in preds] == [1, 2, 3, 4, 5]
    # Top is ETHUSDT @ 0.91
    assert preds[0]["symbol"] == "ETHUSDT"
    assert preds[0]["probability"] == pytest.approx(0.91)
    # All probabilities in [0, 1]
    for p in preds:
        assert 0.0 <= p["probability"] <= 1.0


def test_predicted_at_is_utc_iso8601(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    pa = out["predictions"][0]["predicted_at"]
    # Engine validation per INTERFACE.md §3.1: ISO 8601 UTC. Accept
    # both '...Z' and '+00:00' forms.
    assert "T" in pa
    assert pa.endswith("Z") or pa.endswith("+00:00")


def test_generated_at_is_utc_iso8601(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    g = out["generated_at"]
    assert "T" in g and (g.endswith("Z") or g.endswith("+00:00"))


# ──────────────────────────────────────────────────────────────────────
# write() — dated file + symlink
# ──────────────────────────────────────────────────────────────────────


def test_write_creates_dated_file_and_symlink(temp_db, monkeypatch, tmp_path):
    today = date(2026, 5, 10)
    syms = ["BTCUSDT", "ETHUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, today)
    _mock_joblib_load(monkeypatch, [0.7, 0.5])

    wdp.write(temp_db, prediction_date=today, output_dir=tmp_path)

    dated = tmp_path / "predictions_2026-05-10.json"
    latest = tmp_path / "predictions_latest.json"
    assert dated.exists()
    assert latest.is_symlink()
    assert latest.readlink() == Path("predictions_2026-05-10.json")

    payload = json.loads(dated.read_text())
    assert payload["n_predictions"] == 2


def test_write_replaces_existing_symlink_silently(
    temp_db, monkeypatch, tmp_path
):
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    syms = ["BTCUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, yesterday)
    _mock_joblib_load(monkeypatch, [0.7])

    wdp.write(temp_db, prediction_date=yesterday, output_dir=tmp_path)
    yesterday_file = tmp_path / "predictions_2026-05-09.json"
    assert yesterday_file.exists()
    latest = tmp_path / "predictions_latest.json"
    assert latest.readlink() == Path("predictions_2026-05-09.json")

    # Today's run replaces the symlink
    _seed_features(temp_db, syms, today)
    _mock_joblib_load(monkeypatch, [0.8])
    wdp.write(temp_db, prediction_date=today, output_dir=tmp_path)

    assert latest.readlink() == Path("predictions_2026-05-10.json")
    # Yesterday's dated file is still there (the old dated file is NOT
    # deleted; only the symlink is replaced).
    assert yesterday_file.exists()


def test_write_dry_run_does_not_create_files(
    temp_db, monkeypatch, tmp_path, capsys
):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7])

    wdp.write(temp_db, prediction_date=today, output_dir=tmp_path, dry_run=True)

    assert not (tmp_path / "predictions_2026-05-10.json").exists()
    assert not (tmp_path / "predictions_latest.json").exists()
    captured = capsys.readouterr()
    assert "BTCUSDT" in captured.out


def test_write_does_not_touch_files_on_preflight_failure(
    temp_db, monkeypatch, tmp_path
):
    """Stale features → ExportPreflightError → no file written, no
    symlink modified. Pre-existing symlink (yesterday's) is intact."""
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    syms = ["BTCUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, yesterday)
    _mock_joblib_load(monkeypatch, [0.7])

    # Day 1: write yesterday's file
    wdp.write(temp_db, prediction_date=yesterday, output_dir=tmp_path)
    yesterday_file = tmp_path / "predictions_2026-05-09.json"
    latest = tmp_path / "predictions_latest.json"
    assert yesterday_file.exists()
    assert latest.readlink() == Path("predictions_2026-05-09.json")

    # Day 2: try to write today's file but features are stale
    with pytest.raises(wdp.ExportPreflightError, match="stale"):
        wdp.write(temp_db, prediction_date=today, output_dir=tmp_path)

    # Symlink unchanged, no today-file written
    assert latest.readlink() == Path("predictions_2026-05-09.json")
    assert not (tmp_path / "predictions_2026-05-10.json").exists()


def test_build_raises_when_no_active_10d_model(temp_db):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_features(temp_db, ["BTCUSDT"], today)
    # No active model

    with pytest.raises(wdp.ExportPreflightError, match="active 10d model"):
        wdp.build_predictions(temp_db, prediction_date=today)
```

- [ ] **Step 2: Run tests, verify FAIL with ImportError**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_write_daily_predictions.py -v 2>&1 | tail -30`
Expected: ModuleNotFoundError on `crypto.exports.write_daily_predictions`.

- [ ] **Step 3: Implement `write_daily_predictions.py`**

```python
# crypto/exports/write_daily_predictions.py
"""Build and write data/exports/predictions_YYYY-MM-DD.json + symlink
per INTERFACE.md §3.

The exporter does its OWN inference on the full active universe — it
does NOT read crypto_ml_predictions, which is filtered/capped by
score_universe()'s threshold logic. Two preflight gates:

  1. Staleness — MAX(trade_date) FROM crypto_ml_features must equal
     prediction_date (strict today-only).
  2. Coverage — every active universe symbol must have a feature row
     for prediction_date.

Failure raises ExportPreflightError; no output files are touched.
The engine handles the resulting stale predictions_latest.json per
INTERFACE.md §5.3.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import joblib
import numpy as np

from crypto.config import FEATURE_COLS
from crypto.exports._io import (
    EXPORTS_DIR, atomic_write_json, atomic_replace_symlink,
)

logger = logging.getLogger("mhde.exports.predictions")


class ExportPreflightError(Exception):
    """Raised when preflight gates fail.

    Caller (CLI) should log the message and exit non-zero. No output
    files have been touched at the point this is raised.
    """


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


def _resolve_active_10d_model(conn) -> dict:
    rows = conn.execute(
        """
        SELECT model_id, horizon, model_path
        FROM crypto_ml_model_runs
        WHERE is_active = true
          AND horizon = '10d'
          AND model_id NOT LIKE 'crypto_%_walkfold_%'
        """
    ).fetchall()
    if len(rows) == 0:
        raise ExportPreflightError("no active 10d model in crypto_ml_model_runs")
    if len(rows) > 1:
        ids = ", ".join(r[0] for r in rows)
        raise ExportPreflightError(
            f"more than one active 10d model: {ids}"
        )
    model_id, horizon, model_path = rows[0]
    return {"model_id": model_id, "horizon": horizon, "model_path": model_path}


def _check_freshness_and_coverage(conn, prediction_date: date) -> list[str]:
    """Run the two preflight gates. Returns the list of active-universe
    symbols (in deterministic order) on success; raises
    ExportPreflightError otherwise."""
    max_row = conn.execute(
        "SELECT MAX(trade_date) FROM crypto_ml_features"
    ).fetchone()
    max_trade_date = max_row[0] if max_row else None
    if max_trade_date != prediction_date:
        raise ExportPreflightError(
            f"features stale: MAX(trade_date)={max_trade_date}, "
            f"expected {prediction_date}. Check "
            f"mhde-crypto-predict.service status."
        )

    rows = conn.execute(
        """
        SELECT u.symbol
        FROM crypto_universe u
        LEFT JOIN crypto_ml_features f
          ON f.symbol = u.symbol AND f.trade_date = ?
        WHERE u.is_active = true AND f.symbol IS NULL
        ORDER BY u.symbol
        """,
        [prediction_date],
    ).fetchall()
    missing = [r[0] for r in rows]
    if missing:
        raise ExportPreflightError(
            f"missing features for {len(missing)} active universe "
            f"symbol(s) on {prediction_date}: {', '.join(missing)}"
        )

    symbols = conn.execute(
        "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in symbols]


def _load_features(conn, symbols, prediction_date: date):
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(symbols))
    return conn.execute(
        f"""
        SELECT f.symbol, {feature_select}
        FROM crypto_ml_features f
        WHERE f.trade_date = ?
          AND f.symbol IN ({placeholders})
        """,
        [prediction_date] + list(symbols),
    ).fetchdf()


def build_predictions(
    conn: duckdb.DuckDBPyConnection,
    prediction_date: date | None = None,
) -> dict:
    """Construct the predictions dict per INTERFACE.md §3.

    Steps:
      1. Resolve prediction_date (default today UTC).
      2. Resolve active 10d model.
      3. Run preflight gates (staleness + 100% coverage).
      4. Load features, run model + Platt calibration.
      5. Sort descending, assign ranks 1..N.
    """
    if prediction_date is None:
        prediction_date = _today_utc()

    model_info = _resolve_active_10d_model(conn)
    symbols = _check_freshness_and_coverage(conn, prediction_date)

    features_df = _load_features(conn, symbols, prediction_date)

    bundle = joblib.load(model_info["model_path"])
    model = bundle["model"]
    platt = bundle["platt"]
    medians = bundle.get("medians", {}) or {}

    X = features_df[FEATURE_COLS].copy()
    for col in FEATURE_COLS:
        X[col] = X[col].fillna(medians.get(col, 0))

    raw = model.predict_proba(X)[:, 1].reshape(-1, 1)
    cal = platt.predict_proba(raw)[:, 1]

    # midnight UTC of the prediction date — deterministic per
    # INTERFACE.md §3.1 ("when MHDE generated this prediction"). The
    # actual wall-clock varies by timer fire time; engine cares about
    # the date.
    predicted_at = datetime.combine(
        prediction_date, datetime.min.time(), tzinfo=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = []
    for sym, prob in zip(features_df["symbol"].tolist(), cal.tolist()):
        rows.append((sym, float(prob)))
    rows.sort(key=lambda x: x[1], reverse=True)
    predictions = [
        {
            "symbol": sym,
            "probability": prob,
            "rank": idx + 1,
            "predicted_at": predicted_at,
        }
        for idx, (sym, prob) in enumerate(rows)
    ]

    return {
        "export_date": prediction_date.isoformat(),
        "generated_at": datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "model_id": model_info["model_id"],
        "horizon_days": int(model_info["horizon"].rstrip("d")),
        "n_predictions": len(predictions),
        "predictions": predictions,
    }


def write(
    conn: duckdb.DuckDBPyConnection,
    prediction_date: date | None = None,
    output_dir: Path = EXPORTS_DIR,
    dry_run: bool = False,
) -> dict:
    """Build + atomically write the dated file + replace symlink.

    On preflight failure: raises ExportPreflightError before any file
    is touched.
    """
    payload = build_predictions(conn, prediction_date)
    output_dir = Path(output_dir)
    if dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    dated_name = f"predictions_{payload['export_date']}.json"
    dated_path = output_dir / dated_name
    latest_path = output_dir / "predictions_latest.json"
    atomic_write_json(dated_path, payload)
    atomic_replace_symlink(latest_path, dated_name)
    logger.info(
        "wrote %s (n=%d) and updated symlink %s",
        dated_path, payload["n_predictions"], latest_path,
    )
    return payload
```

- [ ] **Step 4: Re-run tests, verify all PASS**

Run: `venv/bin/python -m pytest tests/crypto/exports/test_write_daily_predictions.py -v 2>&1 | tail -30`
Expected: 11 passed.

- [ ] **Step 5: Run the full exports test directory**

Run: `venv/bin/python -m pytest tests/crypto/exports/ -v 2>&1 | tail -40`
Expected: ~42 passed (or 41 passed + 1 skipped if engine-repo fixture missing).

- [ ] **Step 6: Commit**

```bash
git add crypto/exports/write_daily_predictions.py tests/crypto/exports/test_write_daily_predictions.py
git commit -m "feat(exports): write_daily_predictions — full universe re-score

Strict preflight gates: staleness (MAX(trade_date) == today UTC)
and 100% active-universe coverage. Failure raises
ExportPreflightError before any file is touched; engine handles
stale predictions_latest.json per INTERFACE.md §5.3.

Inference path mirrors crypto.ml.predict.score_universe (joblib
bundle: model + Platt + medians), but does NOT apply the threshold
cap — full ranked list is the contract. Atomic JSON write +
atomic symlink replace via crypto.exports._io."
```

---

# CHECKPOINT 3 — Daily predictions writer complete

**Pause here for review.** Verify:

- [ ] `venv/bin/python -m pytest tests/crypto/exports/ -v 2>&1 | tail -50` shows ~42 tests passing/skipping.
- [ ] All commits since checkpoint 2 follow the convention.

Resume with **Phase 4** after operator approval.

---

# Phase 4: CLI integration

## Task 7: `crypto export-spec` and `crypto export-predictions` commands

**Files:**
- Modify: `main.py` — insert two new commands after `crypto-predict` (around line 1748)

- [ ] **Step 1: Locate insertion point**

Read `main.py` around line 1748. The new commands go AFTER the `crypto_predict` definition and BEFORE `crypto_retrain`.

- [ ] **Step 2: Add the two CLI commands**

In `main.py`, insert the following code immediately after the closing line of `def crypto_predict(...)` (after `conn.close()`):

```python
@crypto.command("export-spec")
@click.option("--dry-run", is_flag=True, help="Print the spec JSON without writing.")
def crypto_export_spec(dry_run):
    """Build active_spec.json from current Phase 1B winner row.

    Reads crypto_backtest_summary / crypto_backtest_runs for run_id
    pinned in crypto/exports/spec_config.py:PHASE1B_WINNER_RUN_ID.
    Computes portfolio metrics via simulate_portfolio. Writes to
    data/exports/active_spec.json (atomic).
    """
    from crypto.exports import write_active_spec

    cfg, conn = _engine_setup()
    try:
        spec = write_active_spec.write(conn, dry_run=dry_run)
        if not dry_run:
            click.echo(
                f"wrote {write_active_spec.ACTIVE_SPEC_PATH} "
                f"(spec_hash={spec['spec_hash']})"
            )
    except write_active_spec.ExportSpecError as e:
        raise click.ClickException(str(e))
    finally:
        conn.close()


@crypto.command("export-predictions")
@click.option("--date", "date_str", default=None,
              help="Prediction date YYYY-MM-DD. Default: today UTC.")
@click.option("--dry-run", is_flag=True,
              help="Print the predictions JSON without writing.")
def crypto_export_predictions(date_str, dry_run):
    """Build predictions_YYYY-MM-DD.json (full active universe ranked)
    and update predictions_latest.json symlink.

    Strict preflight: features for prediction_date must exist for
    every active universe symbol. Failure exits non-zero without
    touching output files; engine handles stale symlink per
    INTERFACE.md §5.3.
    """
    from datetime import date as date_cls
    from crypto.exports import write_daily_predictions

    pred_date = date_cls.fromisoformat(date_str) if date_str else None
    cfg, conn = _engine_setup()
    try:
        payload = write_daily_predictions.write(
            conn, prediction_date=pred_date, dry_run=dry_run,
        )
        if not dry_run:
            click.echo(
                f"wrote predictions_{payload['export_date']}.json "
                f"(n={payload['n_predictions']}, model={payload['model_id']})"
            )
    except write_daily_predictions.ExportPreflightError as e:
        raise click.ClickException(f"preflight failed: {e}")
    finally:
        conn.close()
```

- [ ] **Step 3: Verify the CLI registers the commands**

Write a temp verification script at `.claude/local_scripts/verify_export_cli.py`:

```python
"""Smoke-check that the two new CLI commands register under the
`crypto` Click group."""
from click.testing import CliRunner
from main import cli

runner = CliRunner()
result = runner.invoke(cli, ["crypto", "--help"])
print(result.output)
assert "export-spec" in result.output, "export-spec missing"
assert "export-predictions" in result.output, "export-predictions missing"
print("OK")
```

Run: `venv/bin/python .claude/local_scripts/verify_export_cli.py 2>&1 | tail -20`
Expected: prints help text containing `export-spec` and `export-predictions`, then `OK`.

Then delete: `rm .claude/local_scripts/verify_export_cli.py`

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat(main): crypto export-spec + crypto export-predictions CLI

Both commands registered under the existing crypto Click group, use
the standard _engine_setup() connection helper, support --dry-run,
and surface ExportSpecError / ExportPreflightError as ClickException
with non-zero exit."
```

---

# Phase 5: Systemd timer for daily export

## Task 8: Service + timer units

**Files:**
- Create: `systemd/mhde-crypto-export-predictions.service`
- Create: `systemd/mhde-crypto-export-predictions.timer`

- [ ] **Step 1: Write the service unit**

```ini
# systemd/mhde-crypto-export-predictions.service
[Unit]
Description=MHDE crypto daily predictions export to data/exports/
After=mhde-crypto-predict.service

[Service]
Type=oneshot
User=jpcg
WorkingDirectory=/home/jpcg/MHDE
Environment=MHDE_DB_PATH=/home/jpcg/MHDE/data/mhde.duckdb
ExecStart=/home/jpcg/MHDE/venv/bin/python main.py crypto export-predictions
StandardOutput=append:/home/jpcg/MHDE/data/logs/crypto_export_predictions.log
StandardError=append:/home/jpcg/MHDE/data/logs/crypto_export_predictions.log
TimeoutStartSec=300
```

- [ ] **Step 2: Write the timer unit**

```ini
# systemd/mhde-crypto-export-predictions.timer
[Unit]
Description=MHDE crypto daily predictions export timer (06:15 UTC daily, 7 days/week)

[Timer]
OnCalendar=*-*-* 06:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Validate unit syntax**

Run: `systemd-analyze verify systemd/mhde-crypto-export-predictions.service systemd/mhde-crypto-export-predictions.timer 2>&1 | tail -10`
Expected: no output (or only known harmless warnings; the existing repo units produce a warning about `User=` outside of `[Service]` for user-mode units, but this is a system-mode unit).

If the verify step reports an error not seen in existing crypto units, fix it before continuing.

- [ ] **Step 4: Commit**

```bash
git add systemd/mhde-crypto-export-predictions.service systemd/mhde-crypto-export-predictions.timer
git commit -m "feat(systemd): mhde-crypto-export-predictions timer (06:15 UTC daily)

Service runs 'venv/bin/python main.py crypto export-predictions'.
Timer fires 06:15 UTC every day (7 days/week, crypto markets don't
close), 5h45m after mhde-crypto-predict.timer (00:30 UTC) and 15
minutes before the engine's 06:30 UTC entry phase. Persistent=true
catches up after reboots. Deployment + enable on the VPS is a
separate operator step (see OPERATIONS.md)."
```

**NOTE:** The systemd units are committed but NOT installed on the VPS by this plan. Activation (`sudo systemctl daemon-reload && sudo systemctl enable --now mhde-crypto-export-predictions.timer`) is documented in OPERATIONS.md and performed manually by the operator.

---

# CHECKPOINT 4 — CLI + systemd complete

**Pause here for review.** Verify:

- [ ] `venv/bin/python main.py crypto --help 2>&1 | grep export` shows both commands.
- [ ] `systemd-analyze verify systemd/mhde-crypto-export-predictions.{service,timer}` clean.
- [ ] All commits up to this point present.

Resume with **Phase 6** after operator approval.

---

# Phase 6: Initial production run

## Task 9: Bootstrap `data/exports/` and produce the first files

**Files:**
- Modify: `.gitignore`
- Created at runtime: `data/exports/active_spec.json`, `data/exports/predictions_2026-05-10.json`, `data/exports/predictions_latest.json`

- [ ] **Step 1: Add `data/exports/` to `.gitignore`**

Read `.gitignore` and append a new entry near the existing `data/reports/` line (or wherever ops artifacts live). Add:

```
# Engine export contract artifacts (operational, regenerated daily by
# mhde-crypto-export-predictions.service; written by `crypto export-spec`
# manually after Phase 1B re-run). See INTERFACE.md.
data/exports/
```

Verify with:

```bash
git status --short data/exports/ 2>&1
```

Expected: nothing reported (since `data/exports/` doesn't exist yet, but the gitignore rule is now in place).

- [ ] **Step 2: Create `data/exports/`**

```bash
mkdir -p /home/jpcg/MHDE/data/exports
ls -la /home/jpcg/MHDE/data/exports/
```

Expected: empty directory.

- [ ] **Step 3: Run `crypto export-spec` (dry-run first)**

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py crypto export-spec --dry-run 2>&1 | tail -50
```

Expected: a JSON dump containing `spec_version: "1.0.0"`, the `phase_1b_winner` block with `run_id: backtest_10d_D_top_n_a02e15a0`, `phase_0_status: "interim"`, `backtest_expectations` populated.

- [ ] **Step 4: Run for real**

```bash
venv/bin/python main.py crypto export-spec 2>&1 | tail -10
```

Expected: `wrote /home/jpcg/MHDE/data/exports/active_spec.json (spec_hash=sha256:...)`.

- [ ] **Step 5: Run `crypto export-predictions` (dry-run first)**

```bash
venv/bin/python main.py crypto export-predictions --dry-run 2>&1 | tail -30
```

Expected: a JSON dump with `n_predictions: 50`, ranks 1..50, all probabilities in [0, 1].

- [ ] **Step 6: Run for real**

```bash
venv/bin/python main.py crypto export-predictions 2>&1 | tail -10
```

Expected: `wrote predictions_2026-05-10.json (n=50, model=crypto_10d_db171418)`.

- [ ] **Step 7: Verify the produced files structurally**

```bash
ls -la /home/jpcg/MHDE/data/exports/
```

Expected: 3 entries — `active_spec.json`, `predictions_2026-05-10.json`, `predictions_latest.json` (symlink → predictions_2026-05-10.json).

Write `.claude/local_scripts/verify_initial_exports.py`:

```python
"""Verify the two produced files satisfy INTERFACE.md schemas at a
high level: hash matches, n_predictions=50, ranks consecutive."""
import json
from pathlib import Path

from crypto.exports.hashing import compute_spec_hash

EXPORTS = Path("/home/jpcg/MHDE/data/exports")

spec = json.loads((EXPORTS / "active_spec.json").read_text())
assert spec["spec_hash"] == compute_spec_hash(spec), "spec_hash mismatch"
assert spec["phase_1b_winner"]["run_id"] == "backtest_10d_D_top_n_a02e15a0"
print(f"active_spec.json OK (spec_hash={spec['spec_hash']})")

preds = json.loads((EXPORTS / "predictions_latest.json").read_text())
assert preds["n_predictions"] == len(preds["predictions"]) == 50, \
    f"expected 50, got {preds['n_predictions']}/{len(preds['predictions'])}"
ranks = [p["rank"] for p in preds["predictions"]]
assert ranks == list(range(1, 51)), f"ranks not consecutive 1..50: {ranks[:5]}..."
for p in preds["predictions"]:
    assert 0.0 <= p["probability"] <= 1.0
print(f"predictions_latest.json OK (n={preds['n_predictions']}, model={preds['model_id']})")
```

Run: `venv/bin/python .claude/local_scripts/verify_initial_exports.py 2>&1`

Expected:
```
active_spec.json OK (spec_hash=sha256:...)
predictions_latest.json OK (n=50, model=crypto_10d_db171418)
```

Then delete: `rm .claude/local_scripts/verify_initial_exports.py`

- [ ] **Step 8: Confirm gitignore is honored**

```bash
git status --short 2>&1 | grep -E "data/exports|active_spec|predictions_" 2>&1
```

Expected: no output (gitignore is suppressing all three).

- [ ] **Step 9: Commit the gitignore change**

```bash
git add .gitignore
git commit -m "chore: gitignore data/exports/ (engine contract artifacts)

Mirrors data/reports/ policy. The two files (active_spec.json, daily
predictions_*.json + symlink) are operational artifacts regenerated
by 'crypto export-spec' and the mhde-crypto-export-predictions.timer.
Engine reads them from the working tree on the same VPS."
```

---

# Phase 7: Documentation + exit gates

## Task 10: Update CLAUDE.md (read-first list)

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Read CLAUDE.md to find the read-first list**

Read `CLAUDE.md`. Find the numbered list under "Read first" (item 1 is `ARCHITECTURE.md`, items 2-9 are the existing docs).

- [ ] **Step 2: Add INTERFACE.md as item 10**

Insert after the existing `docs/PATH_TO_LIVE_PLAN.md` entry:

```markdown
10. `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md` — file-based
    contract MHDE must respect when producing
    `data/exports/active_spec.json` and the daily
    `data/exports/predictions_YYYY-MM-DD.json` files. Both files are
    produced by `crypto/exports/` (see `crypto export-spec` and
    `crypto export-predictions` CLI commands plus the
    `mhde-crypto-export-predictions.timer` systemd unit). Schema or
    hash-canonicalization changes require a coordinated commit on
    both repos.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): add INTERFACE.md to read-first list

The crypto-trading-engine repo's INTERFACE.md is the contract for
the two files MHDE produces under data/exports/. Treat it like the
in-repo architecture docs: read before changing anything in
crypto/exports/, the export CLI, or the timer."
```

---

## Task 11: Update DECISIONS.md with new ADR

**Files:**
- Modify: `DECISIONS.md`

- [ ] **Step 1: Read DECISIONS.md to find the next ADR number**

Read the existing ADRs. Find the highest numbered one (e.g., ADR-016) and use the next number for this one.

- [ ] **Step 2: Append new ADR**

At the end of DECISIONS.md, append (replacing `ADR-NN` with the actual next number):

```markdown
---

## ADR-NN: Engine export contract — file-based, MHDE-side production

**Date:** 2026-05-10
**Status:** Accepted

**Context:**
The crypto-trading-engine (separate repo at
`/home/jpcg/crypto-trading-engine/`) needs two inputs from MHDE for
Phase 2/3 paper trading: a strategy spec (rare updates) and a daily
ranked predictions list. Direct DB access from engine to MHDE was
ruled out as too coupling-heavy.

**Decision:** A file-based contract under `data/exports/`, fully
specified in `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md`.
Four production-relevant choices made on the MHDE side this session:

1. **Predictions source — re-score full universe in export script.**
   `crypto_ml_predictions` is filtered/capped (max 15 per horizon) by
   `score_universe()`'s adaptive-threshold logic. INTERFACE.md §3
   requires the full ranked universe (50 coins). Solution: the export
   script does its own inference on the active 10d model bundle. It
   does not write to DB; the existing prediction pipeline is
   unchanged.

2. **Backtest expectations methodology — portfolio-realistic.**
   `report.simulate_portfolio` results (engine compares paper-trade
   portfolio P&L against these). Sum-of-fractions metrics in
   `crypto_backtest_summary` are docs-flagged as ranking-only and
   inflate absolute values; using them for divergence checks would
   compare the wrong methodology.

3. **Risk envelope — adopt INTERFACE.md §2 example values for $1k
   Phase 2 paper trading.** `max_account_drawdown_pct=0.30`,
   `daily_loss_limit_usd=100`, `position_size_min_usd=5`,
   `position_size_max_pct=0.20`. Revisit at Phase 3 → 4 transition
   once paper trading shows real friction.

4. **Static config home — `crypto/exports/spec_config.py`.** Phase 1B
   winner `run_id` plus risk/sizing/runtime/universe constants live
   here as Python constants. Phase 1B re-runs require an explicit
   edit + commit. Git history is the audit trail.

Plus three smaller decisions:
- Hash function byte-for-byte identical to engine reference. Cross-repo
  parity test reads a shared fixture from the engine repo (skips when
  not present).
- `data/exports/` gitignored (operational artifact, mirrors
  `data/reports/` policy).
- Daily predictions timer fires 06:15 UTC, 7 days/week, between the
  existing crypto predict timer (00:30 UTC) and the engine's 06:30 UTC
  entry phase. The export's preflight gates (strict staleness + 100%
  coverage) enforce the freshness contract; systemd ordering is
  informational.

**Consequences:**
- New module `crypto/exports/`, two new CLI commands, one new systemd
  timer.
- Phase 1B re-runs become a deliberate two-step ritual: re-run the
  grid, then edit `spec_config.PHASE1B_WINNER_RUN_ID` and commit.
- No DB schema changes; export reads existing tables only.
- Engine-side coordinated changes (test fixture file, INTERFACE.md
  §2.4 path documentation, engine's `test_hash.py` update) are tracked
  separately in the engine repo.
```

- [ ] **Step 3: Commit**

```bash
git add DECISIONS.md
git commit -m "docs(decisions): ADR for engine export contract

Captures the four production-relevant design choices: re-score full
universe (not read filtered DB), portfolio-realistic expectations,
INTERFACE example risk envelope, static-config home in
crypto/exports/spec_config.py."
```

---

## Task 12: Update OPERATIONS.md with engine-export runbook

**Files:**
- Modify: `OPERATIONS.md`

- [ ] **Step 1: Read OPERATIONS.md to find a sensible insertion point**

Find the section about systemd timers (or recovery procedures). The new section can go near the bottom, before any "Trust ladder" or "Rollback" sections.

- [ ] **Step 2: Append the runbook section**

Append at an appropriate spot:

```markdown
## Engine exports — `data/exports/active_spec.json` + daily predictions

**Contract:** `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md`.
**Producer module:** `crypto/exports/`.

### When to run `crypto export-spec` (rare)

After every Phase 1B re-run that changes the winner config:

1. Run the new sensitivity grid; identify the new winner row.
2. Edit `crypto/exports/spec_config.py:PHASE1B_WINNER_RUN_ID` to the
   new `run_id`.
3. Commit (`feat(exports): Phase 1B winner update — <reason>`).
4. Run `venv/bin/python main.py crypto export-spec` to regenerate
   `data/exports/active_spec.json`.
5. Engine picks up the change on its next entry phase (hash mismatch
   triggers reload + Telegram alert + `spec_history` insert).

### Daily predictions timer

`mhde-crypto-export-predictions.timer` fires at 06:15 UTC daily,
7 days/week. Service runs `venv/bin/python main.py crypto
export-predictions`, which:

1. Resolves active 10d model from `crypto_ml_model_runs`.
2. Pre-flight: requires `MAX(trade_date) FROM crypto_ml_features` ==
   today UTC AND every active universe symbol has a feature row.
3. Re-scores the full universe via the joblib bundle, ranks 1..N, and
   writes `data/exports/predictions_YYYY-MM-DD.json` (atomic) plus
   replaces the `predictions_latest.json` symlink.

If preflight fails, the script exits non-zero and writes nothing.
The engine's own validator sees `predictions_latest.json` pointing
at yesterday's file (`export_date != today`), alerts via Telegram,
and skips the entry phase per INTERFACE.md §5.3.

### First-time deployment on the VPS

```bash
sudo cp /home/jpcg/MHDE/systemd/mhde-crypto-export-predictions.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mhde-crypto-export-predictions.timer
systemctl status mhde-crypto-export-predictions.timer
```

### Recovery — `predictions_latest.json` missing or stale

If the engine pages: `[ENGINE] Predictions stale or missing`.

1. Check `journalctl -u mhde-crypto-export-predictions -n 50` for the
   most recent failure message.
2. Common cause: `mhde-crypto-predict.service` failed earlier in the
   day, so `crypto_ml_features` is missing today's row. Run
   `journalctl -u mhde-crypto-predict -n 50` to confirm.
3. Fix the upstream issue (e.g., re-run `crypto backfill-prices` /
   `crypto backfill-features`).
4. Once features for today exist, re-run manually:
   `venv/bin/python main.py crypto export-predictions`.
5. Verify with: `cat data/exports/predictions_latest.json | head -10`.

### Recovery — `active_spec.json` missing

This file is rarely regenerated. If missing:

1. Confirm `data/exports/` exists. If not: `mkdir -p data/exports`.
2. Run `venv/bin/python main.py crypto export-spec`.
3. Verify the file is valid by running a small script under
   `.claude/local_scripts/` that loads it and re-computes the hash
   (project rules forbid inline `python -c` invocations).

### What NOT to do

- Don't `git add data/exports/`. The directory is gitignored; commits
  would be daily noise.
- Don't edit `data/exports/active_spec.json` directly. The hash field
  protects against tampering — engine validation will fail. Always
  regenerate via `crypto export-spec`.
- Don't change `crypto/exports/hashing.py` without coordinating an
  engine-repo commit (see ADR for the engine-export contract).
```

- [ ] **Step 3: Commit**

```bash
git add OPERATIONS.md
git commit -m "docs(operations): engine-export runbook section

When to run crypto export-spec (rare, post-Phase-1B-rerun ritual),
the daily timer behavior, first-time VPS deployment commands, and
recovery procedures for stale or missing artifacts."
```

---

## Task 13: Update SESSION_LOG.md

**Files:**
- Modify: `SESSION_LOG.md`

- [ ] **Step 1: Read the most recent entry to follow style**

Read SESSION_LOG.md, find the most recent entry. Match its formatting.

- [ ] **Step 2: Append a new entry**

```markdown
### 2026-05-10 — engine-export contract (MHDE side)

**Focus:** Build the MHDE-side export pipeline that produces the two
files defined by `/home/jpcg/crypto-trading-engine/docs/INTERFACE.md`:
`data/exports/active_spec.json` (rare; regenerated after Phase 1B
re-runs via `crypto export-spec`) and the daily
`data/exports/predictions_YYYY-MM-DD.json` + `predictions_latest.json`
symlink (`crypto export-predictions`, scheduled by
`mhde-crypto-export-predictions.timer` at 06:15 UTC).

**Spec:** `docs/superpowers/specs/2026-05-10-mhde-engine-export-contract-design.md`
(commits c057bea, 29fa9e2, 4bf6a29).
**Plan:** `docs/superpowers/plans/2026-05-10-mhde-engine-export-contract.md`.

**Completed:**
- New module `crypto/exports/` (`spec_config.py`, `hashing.py`,
  `_io.py`, `write_active_spec.py`, `write_daily_predictions.py`).
  ~42 tests under `tests/crypto/exports/`, all passing (1 skip when
  the engine-repo cross-repo hash fixture isn't present).
- `crypto export-spec` and `crypto export-predictions` CLI commands.
- New systemd unit pair `mhde-crypto-export-predictions.{service,timer}`
  (06:15 UTC daily, 7 days/week). Validated with `systemd-analyze
  verify`. Deployment to the VPS is a separate operator step.
- Initial production run produced the first
  `data/exports/active_spec.json` (Phase 1B winner
  `backtest_10d_D_top_n_a02e15a0`) and `predictions_2026-05-10.json`
  (50 ranked coins for `crypto_10d_db171418`).
- Doc updates: CLAUDE.md read-first list extended with INTERFACE.md;
  new ADR in DECISIONS.md; OPERATIONS.md gained an "Engine exports"
  runbook section.

**Pending:**
- Engine-side commit creating
  `crypto-trading-engine/tests/fixtures/specs/hash_test_vectors_v1.json`
  (3 vectors) plus the engine `test_hash.py` update + INTERFACE.md
  §2.4 path documentation. Tracked separately in the engine repo.
- VPS install of the new timer (manual `sudo systemctl daemon-reload
  && sudo systemctl enable --now mhde-crypto-export-predictions.timer`
  per OPERATIONS.md).

**Known issues:** none introduced. Cross-repo hash parity test
remains skipped until the engine-side fixture lands.
```

- [ ] **Step 3: Commit**

```bash
git add SESSION_LOG.md
git commit -m "docs(session-log): engine-export contract — MHDE-side build

Captures the 2026-05-10 session: crypto/exports/ module, two new
CLI commands, daily systemd timer, initial production run, doc
updates. Engine-side fixture + INTERFACE.md §2.4 update tracked
separately as cross-repo follow-up."
```

---

# Phase 8: Universal exit gates

## Task 14: Final regression pass

- [ ] **Step 1: Run the no-untracked-imports regression**

```bash
venv/bin/python -m pytest tests/regression/test_no_untracked_production_imports.py -v 2>&1 | tail -10
```

Expected: 1 passed.

- [ ] **Step 2: Run the full regression suite**

```bash
venv/bin/python -m pytest tests/regression/ -v 2>&1 | tail -20
```

Expected: all passing (count varies; should match the count from before this plan started).

- [ ] **Step 3: Run the full crypto/exports test directory**

```bash
venv/bin/python -m pytest tests/crypto/exports/ -v 2>&1 | tail -50
```

Expected: ~42 passed (or 41 + 1 skipped).

- [ ] **Step 4: Verify clean working tree**

```bash
git status 2>&1
```

Expected: `nothing to commit, working tree clean`.

- [ ] **Step 5: Verify the data/exports/ artifacts still resolve correctly**

```bash
ls -la data/exports/
readlink data/exports/predictions_latest.json
```

Expected: `predictions_2026-05-10.json` (or whatever today's date is when run).

---

# Self-review checklist (run before declaring complete)

- [ ] Every section of the spec has an implementing task.
- [ ] No "TBD"/"TODO"/placeholder content in any task.
- [ ] All function names, exception classes, and constants are
      consistent across tasks (e.g., `ExportPreflightError` is the
      same name in tests, in `write_daily_predictions.py`, and in
      `OPERATIONS.md`).
- [ ] Every test step shows actual test code, not "test for X".
- [ ] Every implementation step shows actual code or actual file
      contents.
- [ ] Commit messages match the repo convention from `git log`
      (`feat(scope): ...`, `docs(scope): ...`, `chore: ...`).
- [ ] No use of `python -c` inline blocks anywhere; verification
      scripts go to `.claude/local_scripts/` and are deleted after
      use.
- [ ] The plan honors the spec's "Out of scope" list (no engine-repo
      edits, no DB schema migrations).
