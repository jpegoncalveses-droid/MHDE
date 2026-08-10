"""§5 risk-adjusted excursion label binding + §6.1 permutation null + the Stage-1 search.

LABEL (§5): a firing instance's outcome is the RISK-ADJUSTED EXCURSION at the score
horizon, ``rae = mfe + mae`` (mae<=0) — the favourable forward excursion minus the
adverse magnitude, computed from the substrate's forward-only MFE/MAE label (NOT a
fixed-horizon return; path matters). A rule's edge is the mean, over its firing
instances, of the COIN-CENTERED rae (rae minus that coin's baseline rae), i.e.
"favourable beats adverse by more than the coin's own baseline". Slippage/fees/fills
are deliberately absent — discovery answers "is there a real directional edge"; paper
trading later answers "does it survive costs" (§5). Framed long; short is the symmetric
negation (``side`` param), not the Stage-1 default.

PERMUTATION NULL (§6.1): after scoring the real (condition->label) link at a depth, the
SAME candidate set is re-scored on data whose labels have been SHUFFLED (the lift values
permuted across instances — the real link broken, the marginal distribution preserved).
The best edge the search finds on a shuffle is one null draw at that depth; ``N``
permutations characterise the null distribution; the bar is its ``null_quantile``. A real
candidate passes only if its edge beats the bar FOR ITS OWN DEPTH. This is what makes
unbounded depth safe without a constant cap (§1): it measures the search's
ghost-generation rate at each complexity and demands real rules exceed it. On a pure-noise
tape real and shuffled are exchangeable, so nothing survives — the load-bearing test.

The search is incremental: depth-1 atoms -> keep survivors (beat the bar) -> extend each
survivor by one distinct-feature atom -> re-score+null at depth 2 -> ... until no
survivor or ``max_depth`` (a runaway safety ceiling only).
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rules as R


def risk_adjusted_excursion(mfe, mae, side: str = "long") -> Optional[float]:
    """``mfe + mae`` (long): favourable excursion minus adverse magnitude (mae<=0).
    Short is the symmetric negation. None if either leg is missing."""
    if mfe is None or mae is None:
        return None
    rae = mfe + mae
    return rae if side == "long" else -rae


def compute_instance_lifts(label_rows: Sequence[Mapping], *, horizon_min: int,
                           side: str = "long") -> dict:
    """``(symbol, window) -> coin-centered rae`` over VALID labels at ``horizon_min``.

    Per-coin baseline = mean rae over that coin's valid instances; the lift is
    ``rae - baseline[coin]`` so the edge is coin-relative ("beats the coin's baseline").
    """
    rae_by_key: dict = {}
    rae_by_coin: dict = defaultdict(list)
    for r in label_rows:
        if int(r["horizon_min"]) != horizon_min or not r["valid"]:
            continue
        rae = risk_adjusted_excursion(r["mfe"], r["mae"], side)
        if rae is None:
            continue
        key = (r["symbol"], int(r["window_start_ns"]))
        rae_by_key[key] = rae
        rae_by_coin[r["symbol"]].append(rae)
    baseline = {sym: statistics.fmean(vs) for sym, vs in rae_by_coin.items()}
    return {key: rae - baseline[key[0]] for key, rae in rae_by_key.items()}


def score_rule(rule: R.Rule, lifts: Mapping[tuple, float],
               engineered: Mapping[tuple, Mapping[str, float]],
               min_firing: int = dcfg.MIN_FIRING_INSTANCES) -> Optional[tuple]:
    """``(edge, n)`` = (mean lift over firing instances, firing count), or None below the
    firing floor (an edge off too few instances is noise — neither passed nor counted)."""
    fired = [k for k in R.fires(rule, engineered) if k in lifts]
    if len(fired) < min_firing:
        return None
    return statistics.fmean(lifts[k] for k in fired), len(fired)


@dataclass(frozen=True)
class EntryResult:
    """A Stage-1 entry candidate that beat the null at its own depth."""
    rule: R.Rule
    edge: float
    n_fires: int
    depth: int
    null_bar: float
    margin: float        # edge - null_bar


def _mean_at(values: Sequence[float], idx: Sequence[int]) -> float:
    return sum(values[i] for i in idx) / len(idx)


def _quantile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("-inf")
    if q >= 1.0:
        return s[-1]
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


# -- packed-bitset firing representation (§ memory: O(N/8) per atom, not O(firing) ints) --
# A firing SET over the N labeled instances is a packed uint8 bit-array (np.packbits); rule
# firing is a bitwise AND of its atoms' bit-arrays; the firing COUNT is a hardware popcount.
# This replaces the old ``frozenset``-of-ints atom index (whose size grew with the firing
# rate x instance count -> the discovery OOM). Absent features map to NaN, and NaN compares
# False under both ``>`` and ``<`` -> EXACTLY the old "absent feature never holds" rule.


def _labeled_feature_columns(engineered: Mapping, feature_ids: Sequence[str],
                             keys: Sequence[tuple]) -> dict:
    """``feature_id -> float64 column over ``keys`` (NaN where the feature is absent).

    Uses a tape's columnar fast path (``labeled_columns``) when the engineered layer
    provides one; otherwise reads the Mapping generically (dict inputs, tests). NaN is the
    absent sentinel — engineered values (z-scores, ranks, bounded ratios) are always finite.
    """
    fast = getattr(engineered, "labeled_columns", None)
    if fast is not None:
        return fast(feature_ids, keys)
    n = len(keys)
    cols = {fid: np.full(n, np.nan, dtype=np.float64) for fid in feature_ids}
    for i, k in enumerate(keys):
        fv = engineered.get(k)
        if fv is None:
            continue
        for fid in feature_ids:
            v = fv.get(fid)
            if v is not None:
                cols[fid][i] = v
    return cols


def _atom_bits(col: np.ndarray, op: str, threshold: float) -> np.ndarray:
    """Packed bit-array (uint8, len ceil(N/8)) of the instances where ``feature op threshold``
    holds. NaN (absent) compares False under both ops -> never fires (the §4 no-silent-fill rule)."""
    mask = col > threshold if op == ">" else col < threshold
    return np.packbits(mask)


def _bits_and(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Firing set of a conjunction: bitwise AND of the two operands' packed bit-arrays."""
    return a & b


#: per-byte set-bit count, so ``_popcount`` needs no ``np.bitwise_count`` (numpy>=2.0 only) and
#: runs on any numpy: a 256-entry gather + sum over the packed bytes.
_POPCOUNT8 = np.array([bin(b).count("1") for b in range(256)], dtype=np.int64)


def _popcount(bits: np.ndarray) -> int:
    """Firing count = total set bits, via a 256-entry per-byte lookup (numpy-version-agnostic)."""
    return int(_POPCOUNT8[bits].sum())


def _rule_bits(rule: R.Rule, atom_bits: Mapping) -> Optional[np.ndarray]:
    """AND the packed bit-arrays of a rule's conditions (None for the empty rule)."""
    bits = None
    for c in rule.conditions:
        cb = atom_bits[c]
        bits = cb if bits is None else (bits & cb)
    return bits


def _scorable_firing(rules: Sequence, atom_bits: Mapping, n: int, min_firing: int):
    """For each rule firing ``>= min_firing`` times: ``(rule, count)`` and a COMPACT firing set —
    per candidate, whichever is SMALLER: the packed bitset (uint8, ceil(N/8)) or an int32 index
    array of the set positions.

    The 2026-08-09 gate held a full N-byte bool mask per candidate — O(n_scorable x N), >12 GiB.
    Dense rules (the depth-1 quantile atoms, firing on ~half the tape) keep PACKED (int32 indices
    would be larger); the many sparse deep conjunctions use INDICES (packed would be N/8 EACH and
    would need a transient unpack per null permutation — the per-perm-unpack churn). Storage is
    thus <= N/8 per candidate, and the sparse majority need no unpack in the null loop at all."""
    scorable: list = []
    firing: list = []
    for rule in rules:
        bits = _rule_bits(rule, atom_bits)
        count = _popcount(bits) if bits is not None else 0
        if count >= min_firing:
            scorable.append((rule, count))
            if count * 4 < bits.nbytes:              # int32 indices smaller than the packed bitset
                firing.append(np.flatnonzero(np.unpackbits(bits)[:n]).astype(np.int32))
            else:
                firing.append(bits)                  # dense -> keep packed (indices would be larger)
    return scorable, firing


def _fired_sum(values: np.ndarray, firing: np.ndarray, n: int) -> float:
    """Sum of ``values`` at a candidate's fired instances. ``firing`` is either an int32 INDEX
    array (fancy-index — no unpack) or a packed uint8 bitset (unpacked transiently). Byte-identical
    to ``values[bool_mask].sum()`` either way — the same elements in the same ascending order."""
    if firing.dtype == np.int32:
        return float(values[firing].sum())
    return float(values[np.unpackbits(firing)[:n].view(bool)].sum())


def discover_entries(
    engineered: Mapping[tuple, Mapping[str, float]],
    lifts: Mapping[tuple, float],
    *,
    feature_ids: Sequence[str],
    n_bins: int = dcfg.QUANTILE_BINS,
    n_permutations: int = dcfg.N_PERMUTATIONS,
    null_quantile: float = dcfg.NULL_QUANTILE,
    min_firing: int = dcfg.MIN_FIRING_INSTANCES,
    max_depth: int = dcfg.MAX_DEPTH,
    seed: int = 0,
) -> tuple[list, list]:
    """Run the depth-extensible Stage-1 search under the permutation null.

    Returns ``(survivors, diagnostics)``: survivors are :class:`EntryResult` (every
    candidate that beat its depth's null bar), diagnostics a per-depth dict
    (n_candidates, n_scorable, null_bar, n_passed) — the activity the dashboard surfaces
    (huge candidate counts, almost all dying at the null, is correct, §11).

    Atom firing is precomputed as PACKED BITSETS (N/8 each, ~one per atom), and each depth's
    scorable candidates carry their firing as compact int32 INDEX arrays (total ~ number of
    fires) via :func:`_scorable_firing` — never a held N-byte bool mask per candidate (that
    O(n_scorable x N) form OOM-killed the 2026-08-09 gate at >12 GiB). The engineered layer is
    read columnar. The search order, the
    permutation-null RNG draws, and every promotion decision are byte-for-byte the scalar
    path's (edges differ only in float summation order — sub-ULP, decision-preserving);
    ``_discovery_scalar_oracle`` pins that equivalence.
    """
    # Searchable instances = labeled AND present in the engineered tape. A labeled instance with
    # no engineered features (e.g. a skipped/corrupt primitive fragment at discovery-read time)
    # can fire no rule; excluding it keeps it out of BOTH the firing sets and the permutation-null
    # value pool (a never-firing lift must not shift the noise bar). The scalar path CRASHED on
    # such a key (engineered[k] KeyError); this graceful exclusion is a deliberate robustness
    # improvement, a no-op whenever lifts.keys() ⊆ engineered (the steady-state case).
    keys = sorted(k for k in lifts.keys() if k in engineered)  # deterministic
    n = len(keys)
    values = np.array([lifts[k] for k in keys], dtype=np.float64)
    atoms = R.build_atoms(engineered, feature_ids, n_bins)
    # firing is label-INDEPENDENT -> precompute each atom's packed firing bitset over keys ONCE
    cols = _labeled_feature_columns(engineered, feature_ids, keys)
    atom_bits: dict = {a: _atom_bits(cols[a.feature], a.op, a.threshold) for a in atoms}
    del cols                                          # free the float columns before the null pass

    rng = random.Random(seed)
    survivors: list = []
    diagnostics: list = []
    current = R.depth1_rules(atoms)
    depth = 1
    base_idx = list(range(n))
    while current and depth <= max_depth:
        # Firing carried COMPACT per candidate (packed bitset or int32 indices, whichever is
        # smaller), NOT a held N-byte bool mask (the >12G 2026-08-09 stage1 OOM). Each sum is
        # byte-identical to the old ``values[bool_mask].sum()``.
        scorable, firing = _scorable_firing(current, atom_bits, n, min_firing)
        real = [(rule, _fired_sum(values, f, n) / count)
                for (rule, count), f in zip(scorable, firing)]

        null_bests = []                              # one best-on-noise edge per permutation
        for _ in range(n_permutations):
            perm = base_idx[:]
            rng.shuffle(perm)                        # SAME draws as scalar ``shuffle(values)``
            shuffled = values[perm]
            best = max((_fired_sum(shuffled, f, n) / count
                        for (_, count), f in zip(scorable, firing)), default=float("-inf"))
            null_bests.append(best)
        bar = _quantile(null_bests, null_quantile) if scorable else float("inf")

        passed = [(rule, edge, count) for (rule, edge), (_, count) in zip(real, scorable)
                  if edge > bar]
        diagnostics.append({"depth": depth, "n_candidates": len(current),
                            "n_scorable": len(scorable), "null_bar": bar,
                            "n_passed": len(passed)})
        for rule, edge, count in passed:
            survivors.append(EntryResult(rule=rule, edge=edge, n_fires=count,
                                         depth=depth, null_bar=bar, margin=edge - bar))
        # extend only the survivors (a small set) to the next depth
        nxt: dict = {}
        for rule, _, _ in passed:
            for ext in R.extend_rule(rule, atoms):
                nxt[ext.canonical_id] = ext
        current = list(nxt.values())
        depth += 1
    return survivors, diagnostics
