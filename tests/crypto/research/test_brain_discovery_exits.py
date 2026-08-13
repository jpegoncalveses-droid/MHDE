"""Component 6 (§7) — conditional exit discovery: round-trip simulator + null.

The round-trip simulator walks an entry instance's forward continuation and closes the
trade at the FIRST of: a favourable target / adverse stop (as multiples of the COIN'S
volatility), a live engineered-primitive exit predicate, or a time cap. The exit search
scores each candidate's vol-normalised round-trip return and keeps the best ONLY if it
beats the same permutation null (re-search exits on shuffled continuations). Exits that
only work in-sample die exactly as entry ghosts do (§7).
"""
from __future__ import annotations

import random

import pytest

from crypto.research.brain.discovery import exits as X
from crypto.research.brain.discovery.rules import Condition

_W = 60_000_000_000


def _bar(h, l, c, fv=None):
    return {"rel_high": h, "rel_low": l, "rel_close": c, "fv": fv}


# -- the round-trip simulator -------------------------------------------------

def test_simulate_favorable_target_hit():
    cont = [_bar(1.005, 0.999, 1.004), _bar(1.03, 1.0, 1.02)]  # k=2 high 1.03 >= 1+2*0.01
    er = X.ExitRule(favorable_vol_mult=2.0, adverse_vol_mult=None, time_cap_min=5)
    assert X.simulate_exit(er, cont, coin_vol=0.01) == pytest.approx(2.0)   # +2 vol units


def test_simulate_adverse_stop_hit():
    cont = [_bar(1.001, 0.985, 0.99)]                          # low 0.985 <= 1-1*0.01
    er = X.ExitRule(favorable_vol_mult=None, adverse_vol_mult=1.0, time_cap_min=5)
    assert X.simulate_exit(er, cont, coin_vol=0.01) == pytest.approx(-1.0)


def test_simulate_stop_taken_before_target_when_both_hit_same_bar():
    cont = [_bar(1.05, 0.95, 1.0)]                             # both hit in one bar
    er = X.ExitRule(favorable_vol_mult=1.0, adverse_vol_mult=1.0, time_cap_min=5)
    assert X.simulate_exit(er, cont, coin_vol=0.01) == pytest.approx(-1.0)  # conservative: stop


def test_simulate_time_cap_close():
    cont = [_bar(1.001, 0.999, 1.0005), _bar(1.001, 0.999, 1.002)]
    er = X.ExitRule(favorable_vol_mult=None, adverse_vol_mult=None, time_cap_min=2)
    # neither barrier set -> exit at cap (k=2) close; return = (1.002-1)/0.01 = 0.2
    assert X.simulate_exit(er, cont, coin_vol=0.01) == pytest.approx(0.2)


def test_simulate_incomplete_path_is_unscorable():
    er = X.ExitRule(favorable_vol_mult=None, adverse_vol_mult=None, time_cap_min=5)
    assert X.simulate_exit(er, [_bar(1.0, 1.0, 1.0)], coin_vol=0.01) is None   # path < cap


def test_simulate_primitive_condition_exit():
    cont = [_bar(1.001, 0.999, 1.0005, fv={"m": 0.9}),
            _bar(1.001, 0.999, 1.003, fv={"m": 0.1})]          # m<0.5 at k=2 -> exit there
    er = X.ExitRule(favorable_vol_mult=None, adverse_vol_mult=None, time_cap_min=5,
                    primitive_cond=Condition("m", "<", 0.5))
    assert X.simulate_exit(er, cont, coin_vol=0.01) == pytest.approx(0.3)      # (1.003-1)/0.01


def test_build_exit_grid_count():
    grid = X.build_exit_grid((1.0, 2.0), (1.0,), (5, 15))
    # (1 + |fav|) * (1 + |adv|) * |caps| = 3 * 2 * 2 = 12 barrier/time-cap combos
    assert len(grid) == 12
    assert all(isinstance(e, X.ExitRule) for e in grid)


# -- the exit search under the null -------------------------------------------

def _instances(n):
    return [(f"S{i}", _W) for i in range(n)]


def _planted(n, seed=0):
    """Each instance rises by exactly its own coin vol -> a vol-1.0 target fits the REAL
    pairing; mis-pairing (the null) mostly misses."""
    rng = random.Random(seed)
    inst = _instances(n)
    conts, vols = {}, {}
    for i, k in enumerate(inst):
        vol = 0.004 + 0.0002 * i
        vols[k] = vol
        conts[k] = [_bar(1.0 + vol + 1e-6, 1.0 - 0.5 * vol, 1.0 + vol)] * 5  # rises ~vol
    return inst, conts, vols


def test_discover_exit_finds_a_real_vol_matched_edge():
    inst, conts, vols = _planted(40, seed=1)
    grid = X.build_exit_grid((1.0,), (1.0,), (3,))
    res = X.discover_exit(inst, conts, vols, exit_grid=grid, n_permutations=80,
                          null_quantile=0.95, min_firing=20, seed=1)
    assert res is not None and res.edge > res.null_bar


def test_discover_exit_rejects_noise():
    rng = random.Random(9)
    inst = _instances(40)
    conts, vols = {}, {}
    for i, k in enumerate(inst):
        vols[k] = 0.004 + 0.0002 * i
        r = rng.gauss(0, 0.01)                       # rise UNRELATED to the coin's vol
        conts[k] = [_bar(1.0 + max(r, 0) + 1e-9, 1.0 + min(r, 0) - 1e-9, 1.0 + r)] * 5
    grid = X.build_exit_grid((1.0, 2.0), (1.0, 2.0), (3,))
    res = X.discover_exit(inst, conts, vols, exit_grid=grid, n_permutations=120,
                          null_quantile=1.0, min_firing=20, seed=9)
    assert res is None                               # no exit beats the noise bar


def test_round_trip_scores_and_serialize():
    inst, conts, vols = _planted(5, seed=2)
    er = X.ExitRule(favorable_vol_mult=1.0, adverse_vol_mult=None, time_cap_min=3)
    scores = X.round_trip_scores(er, inst, conts, vols)
    assert set(scores) == set(inst) and all(v == pytest.approx(1.0) for v in scores.values())
    assert X.exit_from_json(X.exit_to_json(er)) == er


# -- Stage-2 instance sampling (Option S, PR #88): bound the continuation build -----------
# Six gate attempts died to host-level OOM with stage-2's unbounded per-rule continuation
# build lifting the pass to ~11.3-11.6G. Sampling <=N fired instances per rule bounds the
# stage-2 increment to ~0.3-0.5G. Conditions (operator dispatch): the sample must be
# DETERMINISTIC per rule (seeded from canonical_id — attempt-stable, so a re-run discovers
# the same exit) and rules firing <=N are UNTOUCHED (byte-identical continuations).

from crypto.research.brain.discovery import runner as RN
from crypto.research.brain.discovery.rules import Condition as _C, make_rule as _mk


def _eng_tape(n, order_seed=None):
    keys = [(f"S{i % 40}", (i + 1) * _W) for i in range(n)]
    if order_seed is not None:
        random.Random(order_seed).shuffle(keys)      # insertion order must not matter
    return {k: {"f": 1.0} for k in keys}


def test_sampled_fires_identity_below_cap():
    eng = _eng_tape(300)
    rule = _mk([_C("f", ">", 0.5)])                  # fires on all 300
    got = RN._sampled_fires(rule, eng, max_instances=300, seed=0)
    assert got == sorted(eng.keys())                 # full population, canonical order
    assert RN._sampled_fires(rule, eng, max_instances=None, seed=0) == sorted(eng.keys())


def test_sampled_fires_deterministic_and_insertion_order_independent():
    rule = _mk([_C("f", ">", 0.5)])
    a = RN._sampled_fires(rule, _eng_tape(500), max_instances=100, seed=3)
    b = RN._sampled_fires(rule, _eng_tape(500), max_instances=100, seed=3)
    c = RN._sampled_fires(rule, _eng_tape(500, order_seed=99), max_instances=100, seed=3)
    assert a == b == c                               # attempt-stable AND order-independent
    assert len(a) == 100 and a == sorted(a)          # exact cap, canonical order
    assert set(a) <= set(_eng_tape(500).keys())


def test_sampled_fires_seed_derives_from_rule_identity():
    eng = _eng_tape(500)
    r1 = _mk([_C("f", ">", 0.5)])
    r2 = _mk([_C("f", ">", 0.25)])                   # different canonical_id, same firing set
    s1 = RN._sampled_fires(r1, eng, max_instances=100, seed=3)
    s2 = RN._sampled_fires(r2, eng, max_instances=100, seed=3)
    assert s1 != s2                                  # rule-derived seed, not shared


def test_sampled_exit_discovery_recovers_full_population_exit():
    # STATISTICAL ADEQUACY: on the planted vol-matched dataset the full population and a
    # <=100-of-400 sample must select the SAME exit rule.
    inst, conts, vols = _planted(400, seed=5)
    grid = X.build_exit_grid((1.0, 2.0), (1.0, 2.0), (3, 5))
    full = X.discover_exit(inst, conts, vols, exit_grid=grid, n_permutations=80,
                           null_quantile=0.95, min_firing=20, seed=5)
    assert full is not None

    sub_keys = RN._sampled_fires(_mk([_C("f", ">", 0.5)]),
                                 {k: {"f": 1.0} for k in inst}, max_instances=100, seed=5)
    sub = X.discover_exit(sub_keys, {k: conts[k] for k in sub_keys},
                          {k: vols[k] for k in sub_keys}, exit_grid=grid,
                          n_permutations=80, null_quantile=0.95, min_firing=20, seed=5)
    assert sub is not None
    assert (sub.exit_rule.favorable_vol_mult, sub.exit_rule.adverse_vol_mult,
            sub.exit_rule.time_cap_min) == \
           (full.exit_rule.favorable_vol_mult, full.exit_rule.adverse_vol_mult,
            full.exit_rule.time_cap_min)


def test_exit_sampling_wired_from_config():
    import inspect

    from crypto.research.brain.discovery import config as dcfg
    assert dcfg.EXIT_DISCOVERY_MAX_INSTANCES == 5000
    assert inspect.signature(RN.run_discovery_pass).parameters["exit_max_instances"].default \
        is dcfg.EXIT_DISCOVERY_MAX_INSTANCES
