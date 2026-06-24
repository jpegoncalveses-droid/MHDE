"""Component 2 (§4) — conjunction rule representation + depth-extensible generation."""
from __future__ import annotations

import pytest

from crypto.research.brain.discovery.rules import (
    Condition, Rule, build_atoms, depth1_rules, extend_rule, fires, make_rule,
    quantile_thresholds,
)

_W = 60_000_000_000


def _eng(rows):
    # rows: list of (symbol, i, {feature: value})
    return {(sym, i * _W): feats for sym, i, feats in rows}


# -- conditions ---------------------------------------------------------------

def test_condition_holds_gt_lt_and_missing_feature():
    gt = Condition("f", ">", 1.0)
    lt = Condition("f", "<", 1.0)
    assert gt.holds({"f": 2.0}) and not gt.holds({"f": 0.0})
    assert lt.holds({"f": 0.0}) and not lt.holds({"f": 2.0})
    assert not gt.holds({}) and not lt.holds({})        # missing feature never holds


def test_rule_is_conjunction_and_missing_any_feature_fails():
    r = make_rule([Condition("a", ">", 0.0), Condition("b", "<", 5.0)])
    assert r.holds({"a": 1.0, "b": 1.0})
    assert not r.holds({"a": 1.0})                       # b absent -> no fire
    assert not r.holds({"a": -1.0, "b": 1.0})


def test_rule_canonical_and_commutative():
    a, b = Condition("a", ">", 0.0), Condition("b", "<", 5.0)
    assert make_rule([a, b]) == make_rule([b, a])        # AND is order-independent
    assert make_rule([a, b, a]).depth == 2               # dedup identical conditions
    assert make_rule([a, b]).canonical_id == make_rule([b, a]).canonical_id


# -- thresholds / atoms -------------------------------------------------------

def test_quantile_thresholds_are_interior_deciles_unique_sorted():
    th = quantile_thresholds([float(i) for i in range(1, 101)], n_bins=10)
    assert len(th) == 9 and th == sorted(th) and len(set(th)) == 9
    assert all(1.0 < t < 100.0 for t in th)


def test_quantile_thresholds_dedup_on_heavy_ties():
    th = quantile_thresholds([0.0] * 50 + [1.0] * 50, n_bins=10)
    assert th == sorted(set(th))                         # ties collapse, still sorted-unique


def test_build_atoms_covers_each_feature_threshold_and_both_ops():
    eng = _eng([("A", i, {"f": float(i)}) for i in range(10)])
    atoms = build_atoms(eng, ["f"], n_bins=5)
    th = quantile_thresholds([float(i) for i in range(10)], n_bins=5)
    assert len(atoms) == len(th) * 2                     # each threshold, both > and <
    assert {a.op for a in atoms} == {">", "<"}
    assert all(a.feature == "f" for a in atoms)


def test_build_atoms_skips_feature_absent_from_tape():
    eng = _eng([("A", i, {"f": float(i)}) for i in range(10)])  # 'f' has spread; 'ghost' absent
    atoms = build_atoms(eng, ["f", "ghost"], n_bins=5)
    assert atoms                                          # 'f' contributes; 'ghost' is a no-op
    assert all(a.feature == "f" for a in atoms)           # no atom for the absent feature


# -- depth extension ----------------------------------------------------------

def test_extend_adds_one_distinct_feature_and_excludes_same_feature():
    atoms = [Condition("a", ">", 0.0), Condition("a", "<", 9.0), Condition("b", ">", 0.0)]
    r1 = make_rule([Condition("a", ">", 0.0)])
    ext = extend_rule(r1, atoms)
    # only the 'b' atom is a valid extension (a-feature atoms excluded: one threshold/feature)
    assert all(r.depth == 2 for r in ext)
    assert {c.feature for r in ext for c in r.conditions if c.feature != "a"} == {"b"}
    assert all("a" in r.features for r in ext)


def test_extend_dedups_and_depth1_rules():
    atoms = [Condition("a", ">", 0.0), Condition("b", "<", 9.0)]
    d1 = depth1_rules(atoms)
    assert len(d1) == 2 and all(r.depth == 1 for r in d1)
    # extending each depth-1 rule by the other atom yields the SAME canonical depth-2 rule
    e0 = extend_rule(d1[0], atoms)
    e1 = extend_rule(d1[1], atoms)
    assert e0[0] == e1[0]


# -- firing -------------------------------------------------------------------

def test_fires_returns_exact_keys_where_rule_holds():
    eng = _eng([
        ("BTCUSDT", 0, {"f": 2.0, "g": 0.1}),     # f>1 and g<0.5 -> fires
        ("BTCUSDT", 1, {"f": 2.0, "g": 0.9}),     # g not < 0.5 -> no
        ("ETHUSDT", 0, {"f": 0.0, "g": 0.1}),     # f not > 1 -> no
        ("ETHUSDT", 1, {"f": 5.0}),               # g missing -> no
    ])
    r = make_rule([Condition("f", ">", 1.0), Condition("g", "<", 0.5)])
    assert fires(r, eng) == {("BTCUSDT", 0)}
