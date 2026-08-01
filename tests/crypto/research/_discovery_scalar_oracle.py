"""FROZEN reference implementation of the Stage-1 search (the pre-bitset scalar path).

This is a VERBATIM copy of ``scoring.discover_entries`` as it stood before the
bitset/columnar rewrite (commit b7ceb81). It is the ORACLE the equivalence test pins the
new implementation against: same substrate + same seed must yield decision-identical
survivors/diagnostics. It is TEST-ONLY code — never imported by production — and is
deliberately not kept in sync with future scoring changes; it freezes THIS refactor's
"no behaviour change" contract.

If you are here because scoring semantics legitimately changed, regenerate/retire this
oracle DELIBERATELY (and update the equivalence test), never edit it to make a failing
equivalence test pass.
"""
from __future__ import annotations

import random
from typing import Mapping, Sequence

from crypto.research.brain.discovery import rules as R
from crypto.research.brain.discovery.scoring import EntryResult


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
    n_bins: int,
    n_permutations: int,
    null_quantile: float,
    min_firing: int,
    max_depth: int,
    seed: int = 0,
) -> tuple[list, list]:
    keys = sorted(lifts.keys())
    values = [lifts[k] for k in keys]
    atoms = R.build_atoms(engineered, feature_ids, n_bins)
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
        scorable = []
        for rule in current:
            idx = _rule_idx(rule)
            if len(idx) >= min_firing:
                scorable.append((rule, tuple(idx)))
        real = [(rule, _mean_at(values, idx)) for rule, idx in scorable]

        null_bests = []
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
        nxt: dict = {}
        for rule, _, _ in passed:
            for ext in R.extend_rule(rule, atoms):
                nxt[ext.canonical_id] = ext
        current = list(nxt.values())
        depth += 1
    return survivors, diagnostics
