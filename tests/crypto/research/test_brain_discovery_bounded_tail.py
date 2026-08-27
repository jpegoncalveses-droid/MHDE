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
