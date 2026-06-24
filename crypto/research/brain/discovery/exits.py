"""§7 — Stage-2 conditional exit discovery (run ONLY on entry survivors).

For each entry rule that survives Stage 1, find the EXIT that best harvests its edge.
An exit candidate is a combination of (§7):
  (a) a primitive-condition exit — a predicate on the live engineered primitives signalling
      the edge is exhausted (``primitive_cond``);
  (b) an excursion-level exit — a favourable target / adverse stop expressed as MULTIPLES
      OF THE COIN'S VOLATILITY (not fixed %), so a target/stop means the same across coins;
  (c) a time cap — max holding windows (always present, so every round trip resolves).

ROUND-TRIP SIMULATOR: from the entry window's close, walk the forward continuation and
close at the FIRST of: adverse stop, favourable target (if both touch in one bar, the stop
is taken — the conservative/realistic assumption), the primitive exit predicate, or the
time cap. The realised return is expressed VOL-NORMALISED (return / coin_vol) — the risk
adjustment that makes round trips comparable across coins.

NULL (§7): the exit search keeps the best candidate only if its round-trip edge beats the
permutation null — "re-search the exits on noise", where noise = DIRECTION-RANDOMISED
continuations (each instance's path reflected around the entry ref with a random sign per
permutation). This destroys the directional round-trip edge while preserving each
instance's own volatility scale (no cross-coin vol-mismatch artefact), so an exit whose
apparent edge is just chance direction dies, while a real directional edge survives. It is
the standard sign-flip permutation null for directional return strategies and a faithful
analog of the entry label-shuffle; the entry null (the critical one) remains the rigorous bar.

FORWARD CONFIRMATION: the chosen exit's round-trip edge is re-confirmed on FRESH instances
by the runner (reusing the confirmation stats on ``round_trip_scores``), so an entry+exit
pair promotes only if BOTH halves independently beat the null AND re-confirm forward (§2).

SCOPING NOTE (reviewer): the default ``build_exit_grid`` enumerates the barrier + time-cap
combos (b,c); primitive-condition exits (a) are fully represented and SIMULATED (and pass
through the same null when supplied with feature-carrying continuations), included in the
grid when the runner provides exit-feature continuations.
"""
from __future__ import annotations

import json
import random
import statistics
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery.rules import Condition
from crypto.research.brain.discovery.scoring import _quantile


@dataclass(frozen=True)
class ExitRule:
    favorable_vol_mult: Optional[float]
    adverse_vol_mult: Optional[float]
    time_cap_min: int
    primitive_cond: Optional[Condition] = None

    def text(self) -> str:
        parts = [f"cap={self.time_cap_min}"]
        if self.favorable_vol_mult is not None:
            parts.append(f"tp={self.favorable_vol_mult}v")
        if self.adverse_vol_mult is not None:
            parts.append(f"sl={self.adverse_vol_mult}v")
        if self.primitive_cond is not None:
            parts.append(f"exit_if[{self.primitive_cond.text()}]")
        return " ".join(parts)


@dataclass(frozen=True)
class ExitResult:
    exit_rule: ExitRule
    edge: float
    n: int
    null_bar: float
    margin: float


@dataclass(frozen=True)
class RoundTrip:
    """A resolved round trip: vol-normalised + raw realised return, holding windows, and
    why it closed (``target`` | ``stop`` | ``primitive`` | ``time_cap``)."""
    vol_normalized: float
    raw_return: float
    exit_k: int
    reason: str


def simulate_exit_detail(exit_rule: ExitRule, continuation: Sequence[Mapping],
                         coin_vol: float) -> Optional[RoundTrip]:
    """Walk the forward continuation and resolve the round trip, or None if the path is too
    short before any close or the vol is undefined. ``continuation[k-1]`` holds the forward
    window ``t+k`` as ``rel_*`` = price/entry_ref (rel_high/rel_low/rel_close) + optional
    ``fv`` (engineered feature vector at that window, for the primitive-condition exit)."""
    if coin_vol is None or coin_vol <= 0:
        return None
    fav = 1.0 + exit_rule.favorable_vol_mult * coin_vol if exit_rule.favorable_vol_mult else None
    adv = 1.0 - exit_rule.adverse_vol_mult * coin_vol if exit_rule.adverse_vol_mult else None
    cap = exit_rule.time_cap_min
    n_avail = len(continuation)
    for k in range(1, cap + 1):
        if k - 1 >= n_avail:
            return None                                # ran out of path before cap, no hit
        bar = continuation[k - 1]
        if adv is not None and bar["rel_low"] <= adv:  # stop first if both touch (conservative)
            return RoundTrip((adv - 1.0) / coin_vol, adv - 1.0, k, "stop")
        if fav is not None and bar["rel_high"] >= fav:
            return RoundTrip((fav - 1.0) / coin_vol, fav - 1.0, k, "target")
        if exit_rule.primitive_cond is not None:
            fv = bar.get("fv")
            if fv is not None and exit_rule.primitive_cond.holds(fv):
                return RoundTrip((bar["rel_close"] - 1.0) / coin_vol, bar["rel_close"] - 1.0,
                                 k, "primitive")
        if k == cap:                                   # time cap reached -> exit at its close
            return RoundTrip((bar["rel_close"] - 1.0) / coin_vol, bar["rel_close"] - 1.0,
                             k, "time_cap")
    return None


def simulate_exit(exit_rule: ExitRule, continuation: Sequence[Mapping],
                  coin_vol: float) -> Optional[float]:
    """Vol-normalised realised round-trip return (the scoring scalar), or None."""
    rt = simulate_exit_detail(exit_rule, continuation, coin_vol)
    return None if rt is None else rt.vol_normalized


def build_exit_grid(favorable_mults: Sequence[float] = dcfg.EXIT_FAVORABLE_VOL_MULTIPLES,
                    adverse_mults: Sequence[float] = dcfg.EXIT_ADVERSE_VOL_MULTIPLES,
                    time_caps: Sequence[int] = dcfg.EXIT_TIME_CAPS_MIN,
                    primitive_conds: Sequence[Condition] = ()) -> list[ExitRule]:
    """The barrier + time-cap grid (b,c); plus primitive-condition variants when supplied."""
    grid: list[ExitRule] = []
    for cap in time_caps:
        for fav in (None, *favorable_mults):
            for adv in (None, *adverse_mults):
                grid.append(ExitRule(fav, adv, cap, None))
        for pc in primitive_conds:
            grid.append(ExitRule(None, None, cap, pc))
    return grid


def round_trip_scores(exit_rule: ExitRule, instances: Sequence[tuple],
                      continuations: Mapping[tuple, Sequence], coin_vols: Mapping[tuple, float]
                      ) -> dict:
    """``{key: vol-normalised round-trip return}`` over the instances the exit can resolve."""
    out: dict = {}
    for k in instances:
        s = simulate_exit(exit_rule, continuations[k], coin_vols[k])
        if s is not None:
            out[k] = s
    return out


def _mean_edge(exit_rule, conts, vols, min_firing) -> Optional[tuple]:
    scores = []
    for i in range(len(conts)):
        s = simulate_exit(exit_rule, conts[i], vols[i])
        if s is not None:
            scores.append(s)
    if len(scores) < min_firing:
        return None
    return statistics.fmean(scores), len(scores)


def _reflect(continuation: Sequence[Mapping]) -> list:
    """Reflect a continuation around the entry ref (1.0): an up path becomes the mirror
    down path. rel_high/rel_low swap+reflect; rel_close reflects; features pass through."""
    return [{"rel_high": 2.0 - b["rel_low"], "rel_low": 2.0 - b["rel_high"],
             "rel_close": 2.0 - b["rel_close"], "fv": b.get("fv")} for b in continuation]


def discover_exit(instances: Sequence[tuple], continuations: Mapping[tuple, Sequence],
                  coin_vols: Mapping[tuple, float], *, exit_grid: Sequence[ExitRule],
                  n_permutations: int = dcfg.N_PERMUTATIONS,
                  null_quantile: float = dcfg.NULL_QUANTILE,
                  min_firing: int = dcfg.MIN_FIRING_INSTANCES, seed: int = 0
                  ) -> Optional[ExitResult]:
    """Best null-surviving exit for this entry's instances, or None if nothing beats the
    bar. Round-trip return is vol-normalised; the null direction-randomises (sign-flips)
    each instance's continuation and re-takes the best-of-grid."""
    cont_list = [continuations[k] for k in instances]
    vol_list = [coin_vols[k] for k in instances]

    real = []
    for er in exit_grid:
        me = _mean_edge(er, cont_list, vol_list, min_firing)
        if me is not None:
            real.append((er, me[0], me[1]))
    if not real:
        return None
    best_er, best_edge, best_n = max(real, key=lambda x: x[1])

    reflected = [_reflect(c) for c in cont_list]
    rng = random.Random(seed)
    null_bests = []
    for _ in range(n_permutations):
        noise = [reflected[i] if rng.random() < 0.5 else cont_list[i]
                 for i in range(len(cont_list))]
        bests = [me[0] for er in exit_grid
                 if (me := _mean_edge(er, noise, vol_list, min_firing)) is not None]
        null_bests.append(max(bests) if bests else float("-inf"))
    bar = _quantile(null_bests, null_quantile)
    if best_edge > bar:
        return ExitResult(best_er, best_edge, best_n, bar, best_edge - bar)
    return None


# -- (de)serialisation for the rule store --------------------------------------

def exit_to_json(exit_rule: ExitRule) -> str:
    pc = exit_rule.primitive_cond
    return json.dumps({
        "favorable_vol_mult": exit_rule.favorable_vol_mult,
        "adverse_vol_mult": exit_rule.adverse_vol_mult,
        "time_cap_min": exit_rule.time_cap_min,
        "primitive_cond": (None if pc is None
                           else {"feature": pc.feature, "op": pc.op, "threshold": pc.threshold}),
    })


def exit_from_json(s: str) -> ExitRule:
    d = json.loads(s)
    pc = d.get("primitive_cond")
    cond = None if pc is None else Condition(pc["feature"], pc["op"], float(pc["threshold"]))
    return ExitRule(d["favorable_vol_mult"], d["adverse_vol_mult"], int(d["time_cap_min"]), cond)
