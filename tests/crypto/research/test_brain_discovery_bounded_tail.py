"""P2 — bound the two remaining unbounded tail allocations (relapse guard).

The Aug 21-24 OOM era died in the pass TAIL, after `pace-expiry HELD`
(`data/processed/discovery_oom_rca.md`). Once P1 restores completed passes the tail becomes
reachable again, so the last two unbounded builds must be bounded first:

  * Stage-4 trade logging (`runner.py:274-275`) called `_entry_continuations` with NO
    `max_instances` — the only unsampled continuation build left, over the PROMOTED set.
  * `breadth` (`runner.py:191`) materialised a Python set of up to n_keys `(symbol, window)`
    tuples per survivor (~1 GB transient x ~1,015 survivors) only to count distinct symbols.
"""
from __future__ import annotations

import hashlib

import numpy as np

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import engineered as E
from crypto.research.brain.discovery import rules as R
from crypto.research.brain.discovery import runner as RUN
from crypto.research.brain.discovery.rules import Condition, make_rule

_W = 60_000_000_000


def _tape(n_keys=400, n_syms=7, feature_ids=("f0", "f1")):
    """Dense EngineeredTape over (symbol, window) keys with deterministic feature values."""
    keys, rows = [], []
    for i in range(n_keys):
        keys.append((f"S{i % n_syms}USDT", (i + 1) * _W))
        rows.append([(i % 100) / 100.0, ((i * 7) % 100) / 100.0])
    key_to_row = {k: i for i, k in enumerate(keys)}
    feat_to_col = {f: j for j, f in enumerate(feature_ids)}
    return E.EngineeredTape(keys, key_to_row, list(feature_ids), feat_to_col,
                            np.array(rows, dtype=np.float64))


# -------------------------------------------------------------------- P2b: bitset breadth

def test_fires_breadth_matches_key_set_breadth_on_fixture():
    """`fires_breadth` must equal the old `len({k[0] for k in fires(...)})` exactly, across
    selective, permissive, empty-conjunction and absent-feature rules."""
    tape = _tape()
    cases = [
        make_rule([Condition("f0", ">", 0.5)]),
        make_rule([Condition("f0", "<", 0.5)]),
        make_rule([Condition("f0", ">", 0.5), Condition("f1", ">", 0.5)]),
        make_rule([Condition("f0", ">", 0.99)]),      # very selective
        make_rule([Condition("f0", ">", -1.0)]),      # everything
        make_rule([]),                                # empty conjunction: holds everywhere
        make_rule([Condition("absent", ">", 0.5)]),   # feature not on the tape
    ]
    for rule in cases:
        expected = len({k[0] for k in R.fires(rule, tape)})
        assert R.fires_breadth(rule, tape) == expected, f"breadth mismatch for {rule}"


def test_fires_breadth_does_not_materialize_the_key_set():
    """The bitset count must not go through `fires_keys` (that set is the ~1 GB transient)."""
    tape = _tape()
    rule = make_rule([Condition("f0", ">", 0.25)])
    calls = []
    orig = E.EngineeredTape.fires_keys

    def _spy(self, r):
        calls.append(r)
        return orig(self, r)

    E.EngineeredTape.fires_keys = _spy
    try:
        got = R.fires_breadth(rule, tape)
    finally:
        E.EngineeredTape.fires_keys = orig
    assert got > 0
    assert calls == [], "fires_breadth must count from the mask, not build the key set"


def test_fires_breadth_falls_back_for_plain_mapping():
    """A plain dict engineered layer (no tape fast path) must still give the right count."""
    eng = {("AUSDT", _W): {"f0": 0.9}, ("BUSDT", 2 * _W): {"f0": 0.8},
           ("AUSDT", 3 * _W): {"f0": 0.1}}
    rule = make_rule([Condition("f0", ">", 0.5)])
    assert R.fires_breadth(rule, eng) == 2


# ------------------------------------------------------------------ P2a: stage-4 bound

def test_tradelog_max_instances_config_exists_and_is_bounded():
    assert isinstance(dcfg.TRADELOG_MAX_INSTANCES, int)
    assert dcfg.MIN_FIRING_INSTANCES < dcfg.TRADELOG_MAX_INSTANCES <= 50_000


def test_stage4_continuations_are_sampled_to_the_bound(monkeypatch):
    """Stage-4 must pass a finite `max_instances` to `_entry_continuations`; unbounded
    (None) is the relapse the RCA names."""
    seen = []
    real = RUN._entry_continuations

    def _spy(entry_rule, engineered, price_index, coin_vols, **kw):
        seen.append(kw.get("max_instances"))
        return real(entry_rule, engineered, price_index, coin_vols, **kw)

    monkeypatch.setattr(RUN, "_entry_continuations", _spy)
    tape = _tape(n_keys=120)
    rule = make_rule([Condition("f0", ">", 0.1)])
    price_index = {k[0]: {} for k in tape}
    RUN._stage4_continuations(rule, tape, price_index, {}, max_cap=60, window_ns=_W)

    assert seen, "stage-4 continuation helper was not called"
    assert all(v == dcfg.TRADELOG_MAX_INSTANCES for v in seen), (
        f"stage-4 must bound instances, saw max_instances={seen}")


def test_sampled_fires_respects_the_bound_deterministically():
    """The sampler must cap the instance count and be reproducible for a given seed."""
    tape = _tape(n_keys=500)
    rule = make_rule([Condition("f0", ">", -1.0)])            # fires everywhere
    a = RUN._sampled_fires(rule, tape, max_instances=25, seed=7)
    b = RUN._sampled_fires(rule, tape, max_instances=25, seed=7)
    assert len(a) == 25
    assert list(a) == list(b)


def test_stage4_sample_is_independent_of_the_stage2_sample():
    """Stage-4 must NOT re-draw stage-2's exact sample.

    Both use the same deterministic rule-seeded sampler and the same cap (5000), so an
    unsalted stage-4 draw is bit-identical to the stage-2 exit-discovery draw whenever the
    firing set is unchanged. That would make `simulated_trades` a 100% in-sample echo of the
    instances the exit was fitted on — destroying the trade log's value as an independent
    signal (it backs rule_aggregates / equity_points and the promote-to-paper decision).
    """
    tape = _tape(n_keys=4000)
    rule = make_rule([Condition("f0", ">", -1.0)])            # fires on every key
    stage2 = RUN._sampled_fires(rule, tape, max_instances=100, seed=0)
    stage4 = RUN._sampled_fires(rule, tape, max_instances=100, seed=0,
                                salt=RUN._STAGE4_SAMPLE_SALT)
    assert len(stage2) == len(stage4) == 100
    assert stage2 != stage4, "stage-4 must draw independently of stage-2"


def test_stage4_sample_is_deterministic():
    tape = _tape(n_keys=4000)
    rule = make_rule([Condition("f0", ">", -1.0)])
    a = RUN._sampled_fires(rule, tape, max_instances=100, seed=0,
                           salt=RUN._STAGE4_SAMPLE_SALT)
    b = RUN._sampled_fires(rule, tape, max_instances=100, seed=0,
                           salt=RUN._STAGE4_SAMPLE_SALT)
    assert a == b


def test_unsalted_sample_is_unchanged_by_the_salt_parameter():
    """The stage-2 draw must be BYTE-IDENTICAL to the pre-salt algorithm — the attempt-
    stability guarantee ('a re-run discovers the same exit') depends on it."""
    tape = _tape(n_keys=4000)
    rule = make_rule([Condition("f0", ">", -1.0)])
    fired = sorted(R.fires(rule, tape))
    digest = hashlib.sha256(rule.canonical_id.encode("utf-8")).digest()
    rule_seed = int.from_bytes(digest[:8], "big") ^ 0
    idx = np.random.default_rng(rule_seed).choice(len(fired), size=100, replace=False)
    idx.sort()
    expected = [fired[i] for i in idx]

    assert RUN._sampled_fires(rule, tape, max_instances=100, seed=0) == expected
    assert RUN._sampled_fires(rule, tape, max_instances=100, seed=0, salt="") == expected


def test_fires_breadth_matches_on_a_nan_laden_tape():
    """NaN compares False, so an absent feature never holds — breadth must agree there too."""
    keys = [("AUSDT", _W), ("AUSDT", 2 * _W), ("BUSDT", _W), ("CUSDT", _W)]
    vals = np.array([[np.nan, 0.9], [0.8, np.nan], [np.nan, np.nan], [0.7, 0.7]])
    tape = E.EngineeredTape(keys, {k: i for i, k in enumerate(keys)}, ["f0", "f1"],
                            {"f0": 0, "f1": 1}, vals)
    for rule in (make_rule([Condition("f0", ">", 0.5)]),
                 make_rule([Condition("f0", "<", 0.5)]),
                 make_rule([Condition("f0", ">", 0.5), Condition("f1", ">", 0.5)]),
                 make_rule([]),
                 make_rule([Condition("zz", ">", 0.0)])):
        assert R.fires_breadth(rule, tape) == len({k[0] for k in R.fires(rule, tape)})


def test_fires_breadth_on_an_empty_tape():
    tape = E.EngineeredTape([], {}, ["f0"], {"f0": 0}, np.zeros((0, 1)))
    for rule in (make_rule([]), make_rule([Condition("f0", ">", 0.5)]),
                 make_rule([Condition("zz", ">", 0.5)])):
        assert R.fires_breadth(rule, tape) == 0


def test_stage4_none_max_instances_means_unbounded_not_the_config_default(monkeypatch):
    """`None` must mean UNBOUNDED, matching stage-2's `exit_max_instances` convention and
    what KI-167 documents as the escape hatch. Falling back to the configured cap would make
    the documented unbounded A/B run silently still-capped."""
    seen = []
    real = RUN._entry_continuations

    def _spy(entry_rule, engineered, price_index, coin_vols, **kw):
        seen.append(kw.get("max_instances"))
        return real(entry_rule, engineered, price_index, coin_vols, **kw)

    monkeypatch.setattr(RUN, "_entry_continuations", _spy)
    tape = _tape(n_keys=80)
    rule = make_rule([Condition("f0", ">", 0.1)])
    price_index = {k[0]: {} for k in tape}

    RUN._stage4_continuations(rule, tape, price_index, {}, max_cap=60, window_ns=_W,
                              max_instances=None)
    assert seen == [None], f"None must pass through as unbounded, got {seen}"

    seen.clear()
    RUN._stage4_continuations(rule, tape, price_index, {}, max_cap=60, window_ns=_W)
    assert seen == [dcfg.TRADELOG_MAX_INSTANCES], (
        f"omitting the arg must use the configured cap, got {seen}")
