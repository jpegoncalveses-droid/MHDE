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
