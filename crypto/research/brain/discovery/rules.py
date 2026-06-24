"""§4 — what a rule is, and depth-extensible generation.

A candidate ENTRY rule is a CONJUNCTION (AND) of one or more conditions. A condition is
an engineered primitive (per-coin z, cross-universe rank, or allowed raw — §3) compared
to a threshold. The conjunction holds for a ``(symbol, window)`` when EVERY condition
holds; a condition whose feature is ABSENT for that key never holds (so the rule fires
only where all its features are present and satisfied — no silent fill).

Thresholds are drawn from a per-feature QUANTILE-BIN discretisation across the tape
(§4): a large-but-enumerable grid where every threshold has equal support (balanced
firing rates, no empty conditions).

Generation is DEPTH-EXTENSIBLE and incremental (§1): start shallow (depth-1 atoms),
and the search (component 3) extends a SURVIVING rule by one atom only when the deeper
rule beats the null at its own depth. This module supplies the pieces; the null-guided
loop lives with the scorer. Depth is unbounded in principle, disciplined by the null in
practice (``config.MAX_DEPTH`` is a runaway SAFETY ceiling only).

Design choice (documented): a conjunction uses DISTINCT features — ``extend_rule`` will
not add a second condition on a feature the rule already constrains. This keeps the grid
clean (no contradictory ``f>5 AND f<3`` that can never fire, no redundant double-bounds)
and is a reasonable first discretisation; cross-feature comparisons / multi-bound boxes
are a later relaxation, not removed from the representation's generality.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from crypto.research.brain.discovery import config as dcfg

_OPS = (">", "<")


@dataclass(frozen=True)
class Condition:
    """``feature <op> threshold``; op in {'>','<'}. Absent feature -> never holds."""
    feature: str
    op: str
    threshold: float

    def holds(self, fv: Mapping[str, float]) -> bool:
        v = fv.get(self.feature)
        if v is None:
            return False
        return v > self.threshold if self.op == ">" else v < self.threshold

    def text(self) -> str:
        return f"{self.feature}{self.op}{self.threshold:.6g}"


def _sort_key(c: Condition):
    return (c.feature, c.op, c.threshold)


@dataclass(frozen=True)
class Rule:
    """A conjunction (AND) of conditions, canonicalised (sorted, deduped) so that AND's
    commutativity makes equal rules compare/hash equal."""
    conditions: tuple = field(default_factory=tuple)

    @property
    def depth(self) -> int:
        return len(self.conditions)

    @property
    def features(self) -> frozenset:
        return frozenset(c.feature for c in self.conditions)

    @property
    def canonical_id(self) -> str:
        return " AND ".join(c.text() for c in self.conditions)

    def holds(self, fv: Mapping[str, float]) -> bool:
        return all(c.holds(fv) for c in self.conditions)


def make_rule(conditions: Sequence[Condition]) -> Rule:
    """Canonical rule: deduped + sorted, so ``make_rule([a,b]) == make_rule([b,a])``."""
    uniq = sorted(set(conditions), key=_sort_key)
    return Rule(tuple(uniq))


def quantile_thresholds(values: Sequence[float], n_bins: int = dcfg.QUANTILE_BINS) -> list[float]:
    """Interior quantile cut-points (n_bins-1 of them) across ``values``, unique+sorted.

    Heavy ties collapse to fewer unique thresholds (a feature with little spread yields a
    smaller grid — correct, not a bug). Fewer than 2 distinct values -> no thresholds.
    """
    vals = [float(v) for v in values if v is not None]
    if len(set(vals)) < 2:
        return []
    cuts = statistics.quantiles(vals, n=n_bins, method="exclusive")
    return sorted({round(c, 12) for c in cuts})


def build_atoms(engineered: Mapping[tuple, Mapping[str, float]],
                feature_ids: Sequence[str], n_bins: int = dcfg.QUANTILE_BINS) -> list[Condition]:
    """Every depth-1 condition: for each feature present on the tape, each quantile
    threshold crossed both ways (> and <). Features absent from the tape contribute none.
    """
    by_feature: dict[str, list[float]] = {}
    for fv in engineered.values():
        for fid, v in fv.items():
            by_feature.setdefault(fid, []).append(v)
    atoms: list[Condition] = []
    for fid in feature_ids:
        for thr in quantile_thresholds(by_feature.get(fid, []), n_bins):
            atoms.extend(Condition(fid, op, thr) for op in _OPS)
    return atoms


def depth1_rules(atoms: Sequence[Condition]) -> list[Rule]:
    return [make_rule([a]) for a in atoms]


def extend_rule(rule: Rule, atoms: Sequence[Condition]) -> list[Rule]:
    """One-atom extensions of ``rule`` over DISTINCT features (no second condition on a
    feature the rule already constrains). Canonicalised + deduped."""
    seen: set = set()
    out: list[Rule] = []
    for a in atoms:
        if a.feature in rule.features:
            continue
        ext = make_rule(rule.conditions + (a,))
        if ext.canonical_id not in seen:
            seen.add(ext.canonical_id)
            out.append(ext)
    return out


def fires(rule: Rule, engineered: Mapping[tuple, Mapping[str, float]]) -> set:
    """The set of ``(symbol, window)`` keys where ``rule`` holds."""
    return {key for key, fv in engineered.items() if rule.holds(fv)}
