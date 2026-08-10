"""Bitset/columnar Stage-1 rewrite — the LOAD-BEARING equivalence guard (§12).

The rule-scoring path is what the whole engine's trustworthiness rests on, so the rewrite
(frozenset firing-sets -> packed bitsets, dict-of-dicts engineered -> columnar) must not
change WHAT the search finds or scores. These tests pin that:

  * decision-identity vs a FROZEN scalar oracle (``_discovery_scalar_oracle``): identical
    funnel + identical survivor set (canonical_id, depth, n_fires), on the same substrate
    and seed. Edges/bars match to a tight tolerance — the only permitted deviation is
    float SUMMATION ORDER (frozenset-iteration-order Python ``sum`` vs numpy), which is
    sub-ULP and cannot flip a promotion.
  * the leaf bitset helpers reproduce ``Condition.holds`` / frozenset-intersection exactly.

The pure-noise rejection guarantee itself lives in test_brain_discovery_scoring (kept green).
"""
from __future__ import annotations

import random

import numpy as np
import pytest

from crypto.research.brain.discovery import scoring
from crypto.research.brain.discovery.rules import Condition, build_atoms, make_rule
from tests.crypto.research import _discovery_scalar_oracle as oracle

_W = 60_000_000_000


def _mixed_tape(n_keys, feature_ids, *, seed):
    """A substrate with a REAL planted edge on feature_ids[0] plus pure-noise features, so
    the search promotes at depth 1 AND generates+scores depth-2 extensions (exercises both
    depths in oracle and new). Some features are sparsely absent (exercises NaN/None)."""
    rng = random.Random(seed)
    eng, lifts = {}, {}
    for i in range(n_keys):
        key = (f"S{i % 20}", (i + 1) * _W)
        fv = {}
        for j, fid in enumerate(feature_ids):
            # feature 2 is sparsely absent on ~30% of keys -> exercises absent/NaN handling
            if j == 2 and rng.random() < 0.3:
                continue
            fv[fid] = rng.random()
        eng[key] = fv
        sig = fv.get(feature_ids[0], 0.5)
        lifts[key] = (3.0 if sig > 0.5 else -3.0) + rng.gauss(0, 0.1)
    return eng, lifts


_FEATS = [f"f{j}" for j in range(6)]
_KW = dict(feature_ids=_FEATS, n_bins=6, n_permutations=100, null_quantile=0.95,
           min_firing=20, max_depth=3, seed=13)
# The frozen oracle predates the beam cap and is beam-less by design, so decision-identity
# is asserted for the UNBOUNDED new search (beam_width=None) — this pins the
# firing/scoring/null path, which is what the oracle guards. Beam semantics are pinned
# separately in test_brain_discovery_scoring.py. On this strong-signal tape the deep passer
# count exceeds BEAM_WIDTH, so the default beam would (correctly) truncate and diverge from
# the beam-less oracle.
_NEW_KW = {**_KW, "beam_width": None}


# -- the load-bearing decision-identity guard ---------------------------------

def test_new_search_is_decision_identical_to_scalar_oracle():
    eng, lifts = _mixed_tape(500, _FEATS, seed=13)
    sur_o, diag_o = oracle.discover_entries(eng, lifts, **_KW)
    sur_n, diag_n = scoring.discover_entries(eng, lifts, **_NEW_KW)

    # the substrate is non-trivial: real survivors AND a depth-2 layer were reached
    assert sur_o, "planted signal must survive in the oracle (test would be vacuous otherwise)"
    assert any(d["depth"] == 2 for d in diag_o), "depth-2 extensions must be exercised"

    # funnel counts identical at every depth
    def _funnel(diag):
        return [(d["depth"], d["n_candidates"], d["n_scorable"], d["n_passed"]) for d in diag]
    assert _funnel(diag_n) == _funnel(diag_o)

    # null bars identical to tolerance (summation order only)
    for dn, do in zip(diag_n, diag_o):
        assert dn["null_bar"] == pytest.approx(do["null_bar"], abs=1e-9, rel=0)

    # survivor SET identical on the decision-bearing fields
    def _dec(sur):
        return sorted((s.rule.canonical_id, s.depth, s.n_fires) for s in sur)
    assert _dec(sur_n) == _dec(sur_o)

    # edges identical to tolerance, matched by rule id
    edge_o = {s.rule.canonical_id: s.edge for s in sur_o}
    max_delta = max((abs(s.edge - edge_o[s.rule.canonical_id]) for s in sur_n), default=0.0)
    assert max_delta < 1e-9, f"edge delta {max_delta:g} exceeds summation-order tolerance"


def test_new_search_is_deterministic_and_matches_oracle_across_seeds():
    for seed in (1, 4, 99):
        eng, lifts = _mixed_tape(300, _FEATS, seed=seed)
        kw = {**_KW, "seed": seed, "n_permutations": 60}
        sur_o, _ = oracle.discover_entries(eng, lifts, **kw)
        sur_n, _ = scoring.discover_entries(eng, lifts, beam_width=None, **kw)
        assert sorted(s.rule.canonical_id for s in sur_n) == sorted(s.rule.canonical_id for s in sur_o)


# -- leaf bitset helpers: exact reproduction of the scalar primitives ----------

def test_atom_bits_reproduces_condition_holds_over_keys():
    eng, lifts = _mixed_tape(120, _FEATS, seed=5)
    keys = sorted(lifts.keys())
    n = len(keys)
    cols = scoring._labeled_feature_columns(eng, _FEATS, keys)
    for fid in (_FEATS[0], _FEATS[2]):        # a dense and a sparsely-absent feature
        for op, thr in ((">", 0.5), ("<", 0.3)):
            bits = scoring._atom_bits(cols[fid], op, thr)
            assert bits.dtype == np.uint8 and len(bits) == (n + 7) // 8   # PACKED, O(N/8)
            got = np.unpackbits(bits)[:n].astype(bool)
            want = np.array([Condition(fid, op, thr).holds(eng[k]) for k in keys])
            assert np.array_equal(got, want)


def test_popcount_matches_bit_count_reference():
    # _popcount must be numpy-version-agnostic (no np.bitwise_count / numpy>=2.0 hard dep).
    rng = np.random.default_rng(0)
    bits = rng.integers(0, 256, size=1000, dtype=np.uint8)
    expected = sum(bin(int(b)).count("1") for b in bits.tolist())
    assert scoring._popcount(bits) == expected
    assert scoring._popcount(np.zeros(10, np.uint8)) == 0
    assert scoring._popcount(np.full(4, 255, np.uint8)) == 32


def test_lift_only_keys_are_excluded_not_diluting_the_null():
    # A labeled instance absent from engineered (e.g. a skipped/corrupt primitive fragment at
    # discovery-read time) can fire no rule; it must be EXCLUDED from the search universe -- not
    # crash the pass (as the scalar oracle did) and not pollute the permutation-null value pool.
    eng, lifts = _mixed_tape(300, _FEATS, seed=21)
    kw = {**_NEW_KW, "seed": 21, "n_permutations": 60}
    base_sur, base_diag = scoring.discover_entries(eng, lifts, **kw)

    lifts_extra = dict(lifts)
    for j in range(50):                                  # 50 EXTREME lift-only keys, none in eng
        lifts_extra[("ZZZ", (99000 + j) * _W)] = 100.0   # would wreck the null bar if not excluded
    ext_sur, ext_diag = scoring.discover_entries(eng, lifts_extra, **kw)   # must not raise

    def _f(diag):
        return [(d["depth"], d["n_candidates"], d["n_scorable"], d["n_passed"]) for d in diag]
    assert _f(ext_diag) == _f(base_diag)                 # excluded -> identical funnel
    assert sorted((s.rule.canonical_id, s.n_fires) for s in ext_sur) == \
           sorted((s.rule.canonical_id, s.n_fires) for s in base_sur)


def test_rule_bits_and_popcount_match_frozenset_intersection():
    eng, lifts = _mixed_tape(160, _FEATS, seed=8)
    keys = sorted(lifts.keys())
    n = len(keys)
    atoms = build_atoms(eng, _FEATS, 6)
    cols = scoring._labeled_feature_columns(eng, _FEATS, keys)
    bits_by_atom = {a: scoring._atom_bits(cols[a.feature], a.op, a.threshold) for a in atoms}
    # a two-condition rule over distinct features
    a0 = next(a for a in atoms if a.feature == _FEATS[0] and a.op == ">")
    a1 = next(a for a in atoms if a.feature == _FEATS[1] and a.op == "<")
    rule = make_rule([a0, a1])
    rule_bits = scoring._bits_and(bits_by_atom[a0], bits_by_atom[a1])
    got_idx = set(np.flatnonzero(np.unpackbits(rule_bits)[:n]))
    want_idx = {i for i, k in enumerate(keys) if rule.holds(eng[k])}
    assert got_idx == want_idx
    assert scoring._popcount(rule_bits) == len(want_idx)
