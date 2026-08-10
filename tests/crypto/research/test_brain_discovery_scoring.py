"""Component 3 (§5 + §6.1) — risk-adjusted excursion label binding + permutation null.

THE most important test in the whole PR (§12): a rule built on PURE NOISE must be
rejected by the permutation null. The null re-runs the SAME search on label-shuffled
data; the best edge it finds on noise is the bar; a real candidate must beat its own
depth's bar to survive. On noise, real and shuffled are exchangeable -> nothing survives.
"""
from __future__ import annotations

import random

import numpy as np
import pytest

from crypto.research.brain.discovery import rules as R
from crypto.research.brain.discovery import scoring as S
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import (
    compute_instance_lifts, discover_entries, risk_adjusted_excursion, score_rule,
)

_W = 60_000_000_000


def _label(sym, i, mfe, mae, horizon=60, valid=True):
    return {"symbol": sym, "window_start_ns": i * _W, "horizon_min": horizon,
            "fwd_return": 0.0, "mfe": mfe, "mae": mae, "valid": valid}


# -- §5 label binding ---------------------------------------------------------

def test_risk_adjusted_excursion_is_favorable_minus_adverse():
    assert risk_adjusted_excursion(0.02, -0.005) == pytest.approx(0.015)   # mfe + mae
    assert risk_adjusted_excursion(0.01, -0.03) == pytest.approx(-0.02)
    assert risk_adjusted_excursion(None, -0.01) is None


def test_instance_lifts_are_coin_centered_and_horizon_valid_filtered():
    rows = [
        _label("BTCUSDT", 0, 0.03, -0.01),   # rae 0.02
        _label("BTCUSDT", 1, 0.01, -0.01),   # rae 0.00  -> BTC baseline 0.01
        _label("ETHUSDT", 0, 0.05, -0.01),   # rae 0.04  -> ETH baseline 0.04 (single)
        _label("BTCUSDT", 2, 0.9, -0.9, horizon=15),   # wrong horizon -> excluded
        _label("BTCUSDT", 3, 0.9, -0.9, valid=False),  # invalid -> excluded
    ]
    lifts = compute_instance_lifts(rows, horizon_min=60, side="long")
    assert lifts[("BTCUSDT", 0)] == pytest.approx(0.02 - 0.01)   # rae - coin baseline
    assert lifts[("BTCUSDT", 1 * _W)] == pytest.approx(0.00 - 0.01)
    assert lifts[("ETHUSDT", 0)] == pytest.approx(0.0)           # single instance -> centered to 0
    assert ("BTCUSDT", 2 * _W) not in lifts and ("BTCUSDT", 3 * _W) not in lifts


def test_score_rule_is_mean_lift_over_fires_with_min_firing_floor():
    eng = {("A", i * _W): {"f": float(i)} for i in range(10)}
    lifts = {("A", i * _W): (1.0 if i >= 5 else -1.0) for i in range(10)}
    rule = make_rule([Condition("f", ">", 4.5)])     # fires on i=5..9 -> all +1 lift
    edge, n = score_rule(rule, lifts, eng, min_firing=3)
    assert n == 5 and edge == pytest.approx(1.0)
    assert score_rule(rule, lifts, eng, min_firing=6) is None   # below firing floor -> unscorable


# -- §6.1 permutation null: THE noise-rejection test --------------------------

def _random_tape(n_keys, feature_ids, *, signal=False, seed=0):
    rng = random.Random(seed)
    eng, lifts = {}, {}
    for i in range(n_keys):
        key = (f"S{i % 20}", i * _W)
        fv = {fid: rng.random() for fid in feature_ids}
        eng[key] = fv
        if signal:
            # lift DETERMINED by the first feature (a real, learnable edge)
            lifts[key] = (3.0 if fv[feature_ids[0]] > 0.5 else -3.0) + rng.gauss(0, 0.1)
        else:
            lifts[key] = rng.gauss(0, 1.0)        # pure noise, independent of features
    return eng, lifts


def test_null_rejects_pure_noise():
    feats = [f"f{j}" for j in range(8)]
    eng, lifts = _random_tape(400, feats, signal=False, seed=7)
    survivors, diag = discover_entries(
        eng, lifts, feature_ids=feats, n_bins=10, n_permutations=120,
        null_quantile=1.0, min_firing=20, max_depth=3, seed=7)
    # the search DID generate and score candidates...
    assert diag[0]["n_candidates"] > 0 and diag[0]["n_scorable"] > 0
    # ...but NONE beat the noise bar: a pure-noise tape promotes nothing.
    assert survivors == []
    assert all(d["n_passed"] == 0 for d in diag)


def test_null_passes_a_planted_real_signal():
    feats = ["sig", "n1", "n2"]
    eng, lifts = _random_tape(400, feats, signal=True, seed=3)
    survivors, diag = discover_entries(
        eng, lifts, feature_ids=feats, n_bins=10, n_permutations=120,
        null_quantile=0.95, min_firing=20, max_depth=2, seed=3)
    assert survivors, "a strong planted edge must survive the null"
    best = max(survivors, key=lambda r: r.edge)
    assert any(c.feature == "sig" for c in best.rule.conditions)
    assert best.edge > best.null_bar                      # beat its own depth's bar


# -- MEMORY behaviour: firing held compact, not one N-byte bool mask per candidate --------
# The 2026-08-09 gate OOM'd at >12G because the depth search materialized and HELD a full
# N-byte bool firing mask per scorable candidate (O(n_scorable x N)). Firing is now stored as
# compact int32 index arrays (total ~ number of fires). This pins that so it cannot silently
# regress to the held-bool-mask form.

def test_scorable_firing_holds_compact_firing_not_full_masks():
    feats = [f"f{j}" for j in range(8)]
    eng, lifts = _random_tape(2000, feats, signal=False, seed=11)      # largish N, ~50% fires
    keys = sorted(k for k in lifts if k in eng)
    n = len(keys)
    atoms = R.build_atoms(eng, feats, 10)
    cols = S._labeled_feature_columns(eng, feats, keys)
    atom_bits = {a: S._atom_bits(cols[a.feature], a.op, a.threshold) for a in atoms}
    rules = R.depth1_rules(atoms)

    scorable, firing = S._scorable_firing(rules, atom_bits, n, min_firing=20)

    assert len(scorable) >= 20 and len(firing) == len(scorable)        # many candidates
    nbytes_per_packed = (n + 7) // 8
    # every candidate's firing is <= the PACKED size (N/8) — packed uint8 (dense) or int32
    # indices (sparse) — NEVER a held full N-byte bool mask.
    for f in firing:
        assert f.dtype in (np.uint8, np.int32)
        assert f.nbytes <= nbytes_per_packed
    total = sum(f.nbytes for f in firing)
    assert total < len(scorable) * n                                  # far below the held-bool-mask form


def test_fired_sum_matches_bool_mask_for_indices_and_packed():
    # both firing representations sum byte-identically to values[bool_mask].sum() (the oracle).
    n = 100
    vals = np.arange(n, dtype=np.float64) * 1.5 + 0.25
    mask = np.zeros(n, dtype=bool)
    mask[[2, 50, 51, 99]] = True
    want = float(vals[mask].sum())
    assert S._fired_sum(vals, np.packbits(mask), n) == want                        # packed path
    assert S._fired_sum(vals, np.flatnonzero(mask).astype(np.int32), n) == want    # index path


def test_discover_is_deterministic_under_seed():
    feats = ["sig", "n1"]
    eng, lifts = _random_tape(300, feats, signal=True, seed=5)
    kw = dict(feature_ids=feats, n_bins=8, n_permutations=60, null_quantile=0.95,
              min_firing=20, max_depth=2, seed=5)
    s1, _ = discover_entries(eng, lifts, **kw)
    s2, _ = discover_entries(eng, lifts, **kw)
    assert [(r.rule.canonical_id, r.edge) for r in s1] == [(r.rule.canonical_id, r.edge) for r in s2]
