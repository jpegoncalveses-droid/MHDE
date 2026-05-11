"""Knockout (triple-barrier) label for crypto entries — pure logic.

A trade entered at close ``C`` is classified by walking forward bar by bar
over up to ``horizon`` bars (the bars *after* the entry bar):

* if a bar's intraday HIGH ≥ ``C·(1 + tp)`` first  → WIN  (``"tp"``)
* if a bar's intraday LOW  ≤ ``C·(1 + sl)`` first  → LOSS (``"sl"``)  (``sl`` < 0)
* if BOTH in the same bar                          → tiebreak (``sl_first`` → ``"sl"``)
* if neither barrier is touched within ``horizon`` → ``"neither"``

``label_Nd_knockout`` (the downstream BOOLEAN) is ``outcome == "tp"``; the
caller classifies ``"neither"`` as a loss per the spec. Barriers use
intraday high/low — the realistic fill basis for a TP/SL order.

No DB I/O, no imports from ``crypto.exports`` / the dashboard. Thresholds:
``crypto/config.py`` (``KNOCKOUT_TP`` / ``KNOCKOUT_SL``). See
``crypto/ml/KNOCKOUT_LABEL_SPEC.md`` and DECISIONS.md (ADR).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

OUTCOME_TP = "tp"
OUTCOME_SL = "sl"
OUTCOME_NEITHER = "neither"


def _is_num(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def knockout_classify(
    forward_highs: Sequence[float],
    forward_lows: Sequence[float],
    entry_close: float,
    tp: float,
    sl: float,
    horizon: int,
    sl_first: bool = True,
) -> tuple[str, Optional[int]]:
    """Classify a knockout entry. See the module docstring.

    Args:
        forward_highs / forward_lows: intraday highs / lows of the bars
            *after* the entry bar, chronological. May be longer than
            ``horizon`` (only the first ``horizon`` are examined) or shorter
            (a partial / truncated window — classified on what's available).
        entry_close: the entry-bar close ``C`` (must be > 0).
        tp: positive fraction (e.g. ``0.10`` → TP barrier ``C·1.10``).
        sl: negative fraction (e.g. ``-0.05`` → SL barrier ``C·0.95``).
        horizon: maximum number of forward bars to consider.
        sl_first: same-bar tiebreak — ``True`` → ``"sl"`` (pessimistic, the default).

    Returns:
        ``(outcome, resolve_day)`` — ``outcome`` ∈ ``{"tp","sl","neither"}``,
        ``resolve_day`` the 1-indexed forward-bar number on which a barrier
        was touched (``None`` for ``"neither"``). A non-positive
        ``entry_close`` or an empty forward window yields ``("neither", None)``.
        Bars with non-numeric (NaN/None) high or low are treated as "no touch".
    """
    if not _is_num(entry_close) or entry_close <= 0:
        return (OUTCOME_NEITHER, None)
    tp_level = entry_close * (1.0 + tp)
    sl_level = entry_close * (1.0 + sl)
    n = min(int(horizon), len(forward_highs), len(forward_lows))
    for i in range(n):
        hi, lo = forward_highs[i], forward_lows[i]
        hit_tp = _is_num(hi) and float(hi) >= tp_level
        hit_sl = _is_num(lo) and float(lo) <= sl_level
        if hit_tp and hit_sl:
            return ((OUTCOME_SL if sl_first else OUTCOME_TP), i + 1)
        if hit_tp:
            return (OUTCOME_TP, i + 1)
        if hit_sl:
            return (OUTCOME_SL, i + 1)
    return (OUTCOME_NEITHER, None)
