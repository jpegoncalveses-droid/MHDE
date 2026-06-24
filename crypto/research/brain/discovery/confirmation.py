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
had its chance and did not confirm — most will, by design, §11). A PROMOTED rule whose
forward edge later DECAYS below the bar is rejected too (§8.1).

The per-coin baseline used to centre fresh lifts is computed upstream over the current
settled tape (a slowly-varying reference, not a label leak); the decisive lookahead-free
property is the post-discovery INSTANCE filter, not the centring.
"""
from __future__ import annotations

import math
import statistics
from typing import Mapping, Optional, Sequence

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


def run_confirmation(conn, engineered: Mapping[tuple, Mapping[str, float]],
                     lifts: Mapping[tuple, float], *, m: int = dcfg.CONFIRM_M,
                     z: float = dcfg.CONFIRM_Z, now_ns: int = 0) -> dict:
    """Advance every live rule against the current settled tape. Returns a small summary
    (counts of promoted / rejected / still-confirming this pass)."""
    summary = {"advanced": 0, "promoted": 0, "rejected": 0, "confirming": 0}
    for state in (RS.DISCOVERED, RS.CONFIRMING, RS.PROMOTED):
        for row in RS.list_rules(conn, state=state):
            rid = row["rule_id"]
            rule = RS.deserialize_rule(row["entry_def"])
            bar = row["null_bar"]
            fresh = fresh_instances(rule, engineered, lifts,
                                    discovery_window_ns=row["discovery_window_ns"])
            n, edge, tstat = fresh_stats([lifts[k] for k in fresh])
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
                if _decayed(n, edge, null_bar=bar, M=m):
                    RS.set_state(conn, rid, RS.REJECTED,
                                 reject_reason="forward edge decayed below bar", now_ns=now_ns)
                    summary["rejected"] += 1
    return summary
