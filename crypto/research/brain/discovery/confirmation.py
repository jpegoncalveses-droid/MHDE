"""§6.2 — forward confirmation: the final gate, by construction un-gameable by the search.

A rule that passed the null on settled labels up to its discovery frontier T must then be
re-evaluated ONLY on instances whose entry window settled AFTER T — data that did not
exist during the search. ``fresh_instances`` enforces exactly that filter
(``window_start_ns > discovery_window_ns``), so no instance the search could have fit on
ever counts toward confirmation (§13c).

Promotion (§6.2) requires the fresh-instance edge to:
  * have accumulated at least ``M`` fresh instances (an INSTANCE count, not calendar time,
    so rare and common rules are judged fairly; ``M`` is a CONSERVATIVE, operator-tunable
    default, explicitly not final — see ``config.CONFIRM_M``), AND
  * stay POSITIVE and DISTINGUISHABLE FROM ZERO (mean / (std/sqrt(n)) >= z), AND
  * stay PAST the in-sample null bar.

A confirming rule with >= M fresh instances that does not meet all three is REJECTED (it
had its chance and did not confirm — most will, by design, §11). A PROMOTED rule with
>= M fresh whose forward edge DECAYS below the bar is rejected too (§8.1); one whose
fresh recount falls below M - CONFIRM_DEMOTE_HYSTERESIS is DEMOTED to confirming
(2026-08-14 — recounts are non-monotonic; the sample shrank, the rule did not fail);
the band [M-H, M) HOLDS promoted (decay-quiet jitter band, ADR-041). NOTE the deliberate asymmetry:
staying promoted is judged edge-only (decay), while RETURNING from a demotion re-runs the
full promotion gauntlet including the t-stat >= z significance test — the same bar the
rule passed at first promotion.

The per-coin baseline used to centre fresh lifts is computed upstream over the current
settled tape (a slowly-varying reference, not a label leak); the decisive lookahead-free
property is the post-discovery INSTANCE filter, not the centring.
"""
from __future__ import annotations

import math
import statistics
from typing import Mapping, Optional, Sequence

import numpy as np

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import rules as R


def fresh_instances(rule, engineered: Mapping[tuple, Mapping[str, float]],
                    lifts: Mapping[tuple, float], *, discovery_window_ns: int) -> list:
    """Firing keys that settled AFTER the discovery frontier and have a label."""
    return [k for k in R.fires(rule, engineered)
            if k[1] > discovery_window_ns and k in lifts]


def fresh_stats(values: Sequence[float]) -> tuple:
    """``(n, edge, tstat)`` for the fresh lifts. tstat = mean / (std/sqrt(n)); None when
    undefined (n<2); +/-inf when std==0 (a perfectly consistent edge IS distinguishable)."""
    n = len(values)
    if n == 0:
        return 0, None, None
    edge = statistics.fmean(values)
    if n < 2:
        return n, edge, None
    sd = statistics.stdev(values)
    if sd == 0:
        return n, edge, math.inf if edge > 0 else (-math.inf if edge < 0 else 0.0)
    return n, edge, edge / (sd / math.sqrt(n))


def confirmation_decision(n: int, edge: Optional[float], tstat: Optional[float], *,
                          null_bar: float, M: int, z: float) -> str:
    """``"wait"`` (< M fresh), ``"promote"`` (>= M and edge positive, distinguishable
    from zero past the bar), else ``"reject"``."""
    if n < M:
        return "wait"
    if (edge is not None and edge > 0 and edge > null_bar
            and tstat is not None and tstat >= z):
        return "promote"
    return "reject"


def _decayed(n: int, edge: Optional[float], *, null_bar: float, M: int) -> bool:
    """A promoted rule has decayed once it has >= M fresh instances and its forward edge
    is no longer positive AND past the bar."""
    return n >= M and not (edge is not None and edge > 0 and edge > null_bar)


#: Bounded per-feature column cache for the shared-compute walk: 8 columns x ~75MB at
#: full scale ~= 600MB. The walk is FAMILY-ORDERED so members hit the cache; an unbounded
#: cache (35 features x 75MB ~= 2.6G) would breach the pass memory budget.
_CONFIRM_COLUMN_CACHE_MAX = 8


class _SharedWalk:
    """MECHANISM 1 (2026-08-15, ADR-042): one shared-COMPUTE confirmation walk.

    Row-aligned ``window``/``lift`` arrays are built ONCE per pass and per-feature
    columns are extracted once (LRU-bounded); every rule's fresh set derives from ITS
    OWN vectorized mask over those shared arrays. Evidence is never shared — only
    compute — so per-rule fresh_count/forward_edge stay individually correct and a ghost
    rule in a real family fails on its own numbers (both pinned by tests). Semantics are
    the scalar path's exactly: absent feature / NaN never holds, strict comparisons,
    fresh = fired AND post-discovery AND labeled, sample-stdev t-stat with the
    fresh_stats edge cases. Replaces a per-rule ``fires()`` SET materialization that cost
    ~0.6s/rule (~1h40m of the 2026-08-14 observation pass at 9.6k rules; the dominant
    saturation term at +1,000 rules/pass growth)."""

    def __init__(self, engineered, lifts):
        from crypto.research.brain.discovery import scoring as _S
        self._eng = engineered
        self._S = _S
        self._keys = list(engineered)
        n = len(self._keys)
        self._win = np.fromiter((k[1] for k in self._keys), dtype=np.int64, count=n)
        key_to_row = getattr(engineered, "_key_to_row", None)   # tape: reuse (a second
        if key_to_row is None:                                  # 9.4M map would cost ~1.2G)
            key_to_row = {k: i for i, k in enumerate(self._keys)}
        self._lift = np.full(n, np.nan, dtype=np.float64)
        for k, v in lifts.items():
            i = key_to_row.get(k)
            if i is not None:
                self._lift[i] = v
        self._has_label = ~np.isnan(self._lift)
        self._cols: dict = {}                                   # tiny LRU

    def _col(self, feature):
        col = self._cols.pop(feature, None)
        if col is None:
            col = self._S._labeled_feature_columns(self._eng, [feature], self._keys)[feature]
        self._cols[feature] = col                               # (re-)append = most recent
        if len(self._cols) > _CONFIRM_COLUMN_CACHE_MAX:
            self._cols.pop(next(iter(self._cols)))              # evict least recent
        return col

    def fresh_stats_for(self, rule, discovery_window_ns):
        """(n, edge, tstat) for the rule's fresh instances — fresh_stats semantics."""
        mask = None
        for c in rule.conditions:
            col = self._col(c.feature)
            cm = col > c.threshold if c.op == ">" else col < c.threshold
            mask = cm if mask is None else (mask & cm)          # NaN compares False
        fresh = (self._win > discovery_window_ns) & self._has_label
        if mask is not None:
            fresh &= mask
        n = int(fresh.sum())
        if n == 0:
            return 0, None, None
        vals = self._lift[fresh]
        edge = float(vals.mean())
        if n < 2:
            return n, edge, None
        sd = float(vals.std(ddof=1))
        if sd == 0:
            return n, edge, math.inf if edge > 0 else (-math.inf if edge < 0 else 0.0)
        return n, edge, edge / (sd / math.sqrt(n))


def run_confirmation(conn, engineered: Mapping[tuple, Mapping[str, float]],
                     lifts: Mapping[tuple, float], *, m: int = dcfg.CONFIRM_M,
                     z: float = dcfg.CONFIRM_Z,
                     hysteresis: int = dcfg.CONFIRM_DEMOTE_HYSTERESIS,
                     now_ns: int = 0) -> dict:
    """Advance every live rule against the current settled tape. Returns a small summary
    (counts of advanced / promoted / rejected / demoted / still-confirming this pass)."""
    if not 0 <= hysteresis < m:
        # m is an explicitly to-be-retuned default; a band as wide as m would make the
        # demotion branch dead code (n < 0) and silently reopen the sub-M immunity.
        raise ValueError(f"hysteresis must satisfy 0 <= h < m (got h={hysteresis}, m={m})")
    summary = {"advanced": 0, "promoted": 0, "rejected": 0, "confirming": 0, "demoted": 0}
    walk = _SharedWalk(engineered, lifts)
    for state in (RS.DISCOVERED, RS.CONFIRMING, RS.PROMOTED):
        # FAMILY-ORDERED so same-family members hit the bounded column cache
        for row in sorted(RS.list_rules(conn, state=state),
                          key=lambda r: r.get("family_key") or ""):
            rid = row["rule_id"]
            rule = RS.deserialize_rule(row["entry_def"])
            bar = row["null_bar"]
            n, edge, tstat = walk.fresh_stats_for(
                rule, discovery_window_ns=row["discovery_window_ns"])
            RS.update_forward(conn, rid, fresh_count=n, forward_edge=edge, now_ns=now_ns)

            cur = state
            if cur == RS.DISCOVERED:
                RS.set_state(conn, rid, RS.CONFIRMING, now_ns=now_ns)
                summary["advanced"] += 1
                cur = RS.CONFIRMING

            if cur == RS.CONFIRMING:
                decision = confirmation_decision(n, edge, tstat, null_bar=bar, M=m, z=z)
                if decision == "promote":
                    RS.set_state(conn, rid, RS.PROMOTED, now_ns=now_ns)
                    summary["promoted"] += 1
                elif decision == "reject":
                    RS.set_state(conn, rid, RS.REJECTED,
                                 reject_reason="forward edge not confirmed", now_ns=now_ns)
                    summary["rejected"] += 1
                else:
                    summary["confirming"] += 1
            elif cur == RS.PROMOTED:
                if n < m - hysteresis:
                    # Evidence-shrink demotion with HYSTERESIS (2026-08-14): recounts
                    # are non-monotonic and most dips are jitter within [M-H, M) — that
                    # band HOLDS promoted (no demote, no decay, no flap; ADR-041). Only a
                    # recount below M-H demotes. Original rationale: recounts non-monotonic
                    # (features re-derive each pass; instances cross thresholds both
                    # ways). The old gate required n >= M before ANY decay check, so a
                    # promoted rule recounting below M became decay-IMMUNE (30/110 live
                    # promoted sat at 24-29 fresh). Below the decision floor the rule
                    # goes back to CONFIRMING — not rejected: its sample shrank, it did
                    # not fail — and re-promotes when the count returns.
                    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=now_ns)
                    summary["demoted"] += 1
                elif _decayed(n, edge, null_bar=bar, M=m):
                    RS.set_state(conn, rid, RS.REJECTED,
                                 reject_reason="forward edge decayed below bar", now_ns=now_ns)
                    summary["rejected"] += 1
    return summary
