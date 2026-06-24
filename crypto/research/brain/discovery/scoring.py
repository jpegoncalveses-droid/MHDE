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
    """
    keys = sorted(lifts.keys())                      # labeled instances, deterministic
    values = [lifts[k] for k in keys]
    atoms = R.build_atoms(engineered, feature_ids, n_bins)
    # firing is label-INDEPENDENT -> precompute each atom's index set over labeled keys ONCE
    atom_idx: dict = {
        a: frozenset(i for i, k in enumerate(keys) if a.holds(engineered[k]))
        for a in atoms
    }

    def _rule_idx(rule: R.Rule) -> frozenset:
        sets = [atom_idx[c] for c in rule.conditions]
        return frozenset.intersection(*sets) if sets else frozenset()

    rng = random.Random(seed)
    survivors: list = []
    diagnostics: list = []
    current = R.depth1_rules(atoms)
    depth = 1
    while current and depth <= max_depth:
        scorable = []                                # (rule, idx_tuple)
        for rule in current:
            idx = _rule_idx(rule)
            if len(idx) >= min_firing:
                scorable.append((rule, tuple(idx)))
        real = [(rule, _mean_at(values, idx)) for rule, idx in scorable]

        null_bests = []                              # one best-on-noise edge per permutation
        for _ in range(n_permutations):
            shuffled = values[:]
            rng.shuffle(shuffled)
            best = max((_mean_at(shuffled, idx) for _, idx in scorable), default=float("-inf"))
            null_bests.append(best)
        bar = _quantile(null_bests, null_quantile) if scorable else float("inf")

        passed = [(rule, edge, idx) for (rule, edge), (_, idx) in zip(real, scorable) if edge > bar]
        diagnostics.append({"depth": depth, "n_candidates": len(current),
                            "n_scorable": len(scorable), "null_bar": bar,
                            "n_passed": len(passed)})
        for rule, edge, idx in passed:
            survivors.append(EntryResult(rule=rule, edge=edge, n_fires=len(idx),
                                         depth=depth, null_bar=bar, margin=edge - bar))
        # extend only the survivors (a small set) to the next depth
        nxt: dict = {}
        for rule, _, _ in passed:
            for ext in R.extend_rule(rule, atoms):
                nxt[ext.canonical_id] = ext
        current = list(nxt.values())
        depth += 1
    return survivors, diagnostics
