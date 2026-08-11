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


# -- Beam cap + final-depth guard + per-feature atom bits (discovery-scale PR) ------------
# Measured basis (data/processed/stage1_breadth_cap_measurement.md, 2026-08-10): the null
# goes permeable at depth>=3 (54%/46% pass on the 300-sym proxy; 45% at true scale) and the
# survivor flood is a flat redundant tail — a top-K beam keeps >=98.8% of the top-10k
# depth-4 survivors at K=500 while bounding both the extension pool and the retained set.


def _signal_tape_with_many_d1_passers(seed=3):
    feats = ["sig", "n1", "n2"]
    eng, lifts = _random_tape(400, feats, signal=True, seed=seed)
    return feats, eng, lifts


def test_beam_keeps_topk_by_lift_retained_and_extended():
    feats, eng, lifts = _signal_tape_with_many_d1_passers()
    kw = dict(feature_ids=feats, n_bins=10, n_permutations=120, null_quantile=0.95,
              min_firing=20, max_depth=2, seed=3)
    all_surv, all_diag = discover_entries(eng, lifts, beam_width=None, **kw)
    d1_all = [r for r in all_surv if r.depth == 1]
    assert len(d1_all) > 2, "precondition: need >2 depth-1 passers for the beam to bite"

    K = 2
    beam_surv, beam_diag = discover_entries(eng, lifts, beam_width=K, **kw)
    d1_beam = [r for r in beam_surv if r.depth == 1]
    d2_beam = [r for r in beam_surv if r.depth == 2]

    # RETAINED: exactly the top-K depth-1 passers by edge survive (deterministic tie-break).
    want = sorted(d1_all, key=lambda r: (-r.edge, r.rule.canonical_id))[:K]
    assert {(r.rule.canonical_id, r.edge) for r in d1_beam} \
        == {(r.rule.canonical_id, r.edge) for r in want}
    assert len(d1_beam) == K

    # depth-1 pool and bar are identical (beam filters AFTER the null), so n_passed matches;
    # n_kept records the truncation.
    assert beam_diag[0]["n_passed"] == all_diag[0]["n_passed"] > K
    assert beam_diag[0]["n_kept"] == K
    assert all_diag[0]["n_kept"] == all_diag[0]["n_passed"]   # unbounded -> kept == passed

    # EXTENDED only from kept: fewer depth-2 candidates than the unbeamed run, and every
    # beamed depth-2 survivor extends one of the K kept depth-1 rules.
    assert beam_diag[1]["n_candidates"] < all_diag[1]["n_candidates"]
    kept_cond_sets = [set(r.rule.conditions) for r in d1_beam]
    for r in d2_beam:
        conds = set(r.rule.conditions)
        assert any(kc <= conds for kc in kept_cond_sets)

    # DEPTH-GENERIC: the beam truncates at depth 2 as well (this fixture passes >K there
    # too). Pins that the cap is not depth-1-only — a depth-conditional regression would
    # leave exactly the permeable deep depths (measured 45% pass at depth 3) unbeamed.
    assert beam_diag[1]["n_passed"] > K
    assert beam_diag[1]["n_kept"] == K
    assert len(d2_beam) == K


def test_beam_default_is_config_width_and_noop_when_under_it():
    from crypto.research.brain.discovery import config as dcfg
    assert dcfg.BEAM_WIDTH == 500
    feats, eng, lifts = _signal_tape_with_many_d1_passers()
    kw = dict(feature_ids=feats, n_bins=10, n_permutations=120, null_quantile=0.95,
              min_firing=20, max_depth=2, seed=3)
    default_surv, default_diag = discover_entries(eng, lifts, **kw)            # default beam
    unbounded_surv, _ = discover_entries(eng, lifts, beam_width=None, **kw)
    # small tape passes << 500 per depth -> the default beam is a no-op
    assert [(r.rule.canonical_id, r.edge) for r in default_surv] \
        == [(r.rule.canonical_id, r.edge) for r in unbounded_surv]
    assert all(d["n_kept"] == d["n_passed"] for d in default_diag)


def test_beam_width_default_is_wired_from_config():
    # The production path (systemd unit -> brain-discover-run -> run_discovery ->
    # run_discovery_pass -> discover_entries) reaches the beam ONLY through these two
    # signature defaults; a None (or literal) default ships an unbeamed production pass
    # with every other test green. `is` pins identity to the config object: 500 is outside
    # CPython's small-int cache, so a re-typed literal fails too.
    import inspect

    from crypto.research.brain.discovery import config as dcfg
    from crypto.research.brain.discovery import runner

    assert inspect.signature(S.discover_entries).parameters["beam_width"].default \
        is dcfg.BEAM_WIDTH
    assert inspect.signature(runner.run_discovery_pass).parameters["beam_width"].default \
        is dcfg.BEAM_WIDTH


def test_no_extension_built_past_final_depth(monkeypatch):
    # The md4 mirror OOM'd building a ~144M-rule extension pool for a depth that would
    # never be scored. Pin: extend_rule is NEVER called once depth == max_depth.
    feats, eng, lifts = _signal_tape_with_many_d1_passers()

    def _boom(*a, **k):
        raise AssertionError("extend_rule called at final depth")

    monkeypatch.setattr(S.R, "extend_rule", _boom)
    survivors, diag = discover_entries(
        eng, lifts, feature_ids=feats, n_bins=10, n_permutations=120,
        null_quantile=0.95, min_firing=20, max_depth=1, seed=3)
    assert survivors and diag[0]["depth"] == 1     # ran, scored, survived — no extension


def test_atom_bits_per_feature_matches_all_at_once():
    # Oracle-preservation for the prep-transient fix: bits built one feature at a time are
    # byte-identical to bits built from the all-features column dict.
    feats = [f"f{j}" for j in range(6)]
    eng, lifts = _random_tape(500, feats, signal=False, seed=13)
    keys = sorted(k for k in lifts if k in eng)
    atoms = R.build_atoms(eng, feats, 10)

    cols = S._labeled_feature_columns(eng, feats, keys)
    want = {a: S._atom_bits(cols[a.feature], a.op, a.threshold) for a in atoms}

    got = S._atom_bits_per_feature(eng, feats, keys, atoms)

    assert set(got) == set(want)
    for a in atoms:
        assert got[a].dtype == want[a].dtype
        assert np.array_equal(got[a], want[a])


# -- Columnar label load (Option A, PR #88): kill the ~3G label-dict transient ------------
# The label read was the LAST list-of-dicts load path (runner read ~6M rows as Python
# dicts, ~3G transient — the measured 2026-08-11 gate OOM term). The columnar twin must be
# BYTE-IDENTICAL: same dict out, including duplicate-key last-wins (with every occurrence
# still counted in the coin baseline), None mfe/mae exclusion (NOT NaN-coerced), row-order
# fmean baselines, and horizon/valid filtering.

def _lifts_tape_rows():
    rows = [
        _label("BTCUSDT", 0, 0.03, -0.01),                  # rae 0.02
        _label("BTCUSDT", 1, 0.01, -0.01),                  # rae 0.00
        _label("ETHUSDT", 0, 0.05, -0.01),                  # rae 0.04 (single-instance coin)
        _label("BTCUSDT", 2, 0.9, -0.9, horizon=15),        # wrong horizon -> excluded
        _label("BTCUSDT", 3, 0.9, -0.9, valid=False),       # invalid -> excluded
        _label("SOLUSDT", 5, None, -0.01),                  # None mfe -> rae None -> excluded
        _label("BTCUSDT", 1, 0.05, -0.01),                  # DUPLICATE key: last wins for the
                                                            # key, BOTH occurrences in baseline
        _label("ADAUSDT", 7, 0.02, -0.03),                  # negative-rae coin
    ]
    return rows


def _rows_to_label_table(rows):
    import pyarrow as pa
    cols = {
        "symbol": pa.array([r["symbol"] for r in rows], type=pa.string()),
        "window_start_ns": pa.array([r["window_start_ns"] for r in rows], type=pa.int64()),
        "window_end_ns": pa.array([r["window_start_ns"] + _W for r in rows], type=pa.int64()),
        "horizon_min": pa.array([r["horizon_min"] for r in rows], type=pa.int64()),
        "valid": pa.array([r["valid"] for r in rows], type=pa.bool_()),
        "mfe": pa.array([r["mfe"] for r in rows], type=pa.float64()),
        "mae": pa.array([r["mae"] for r in rows], type=pa.float64()),
    }
    return pa.table(cols)


def test_instance_lifts_columnar_is_byte_identical_to_dict_path():
    rows = _lifts_tape_rows()
    tbl = _rows_to_label_table(rows)
    want = compute_instance_lifts(rows, horizon_min=60, side="long")
    got = S.compute_instance_lifts_columnar(tbl, horizon_min=60, side="long")
    assert got == want                       # dict equality on floats == byte identity
    assert set(map(type, got.values())) == {float}
    # the tricky rows really exercised the semantics:
    assert ("BTCUSDT", 1 * _W) in got                        # duplicate key present (last wins)
    assert ("SOLUSDT", 5 * _W) not in got                    # None mfe excluded, not NaN
    assert ("BTCUSDT", 2 * _W) not in got and ("BTCUSDT", 3 * _W) not in got


def test_instance_lifts_columnar_empty_and_all_filtered():
    import pyarrow as pa
    rows = [_label("BTCUSDT", 0, 0.9, -0.9, horizon=15)]    # everything filtered out
    assert S.compute_instance_lifts_columnar(_rows_to_label_table(rows), horizon_min=60) == {}
    empty = _rows_to_label_table([]) if False else pa.table(
        {c: pa.array([], type=t) for c, t in [
            ("symbol", pa.string()), ("window_start_ns", pa.int64()),
            ("window_end_ns", pa.int64()), ("horizon_min", pa.int64()),
            ("valid", pa.bool_()), ("mfe", pa.float64()), ("mae", pa.float64())]})
    assert S.compute_instance_lifts_columnar(empty, horizon_min=60) == {}
