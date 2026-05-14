"""Post-parabolic exclusion filter — a pre-order-entry risk gate.

OR-combined two-rule risk gate:

  Rule A — post-parabolic (original, SKYAI-class, ADR-021):
    Suppresses crypto buy signals on coins that sit deep in a drawdown from
    their 90-day high while still up massively over 60 days. The model's
    probability isn't *wrong* (such coins do tag +10% — the volatility-loving
    threshold label), it's optimising the wrong objective, so the right
    response is a binary risk gate, not a probability haircut.

  Rule B — short-window momentum (added 2026-05-14, SWARMSUSDT-class, ADR-028):
    Suppresses coins exhibiting acute short-window weakness at entry-time
    (``return_5d < -0.30``). A paired backtest validated the rule as
    Sharpe-positive (6.32 → 6.51) with unchanged max DD; the live SWARMSUSDT
    incident on 2026-05-14 fits the class exactly.

Each rule fails open on its own missing/NaN inputs (the other rule still
evaluates), so a coin in the 60-day features warmup window with NULL
``return_5d`` is not blocked from being evaluated by Rule A.

Pure logic: no DB I/O, no imports from ``crypto.exports`` or the dashboard.
See ``crypto/ml/POSTPARABOLIC_FILTER_SPEC.md``.
"""
from __future__ import annotations

import logging
import math

from crypto.config import (
    POSTPARABOLIC_DD90_THRESHOLD,
    POSTPARABOLIC_RET60_THRESHOLD,
    POSTPARABOLIC_RET5_THRESHOLD,
)

logger = logging.getLogger("mhde.crypto.postparabolic_filter")

#: Canonical, stable reason tokens written to ``crypto_signal_exclusions.reason``
#: and emitted in the export log. Stay constant if the thresholds are retuned
#: (the actual values are stored in their own columns), so they group cleanly
#: in audit queries.
REASON_POST_PARABOLIC = "post_parabolic"
REASON_SHORT_MOMENTUM = "short_momentum"
REASON_BOTH = "post_parabolic_and_short_momentum"

#: Back-compat alias — the original module exported ``REASON`` for the single
#: rule. Existing call sites that imported it keep working. Prefer the
#: ``REASON_*`` constants in new code.
REASON = REASON_POST_PARABOLIC


def _is_missing(x) -> bool:
    if x is None:
        return True
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def _post_parabolic_fires(dd90, ret60) -> bool:
    if _is_missing(dd90) or _is_missing(ret60):
        return False
    return (float(dd90) < POSTPARABOLIC_DD90_THRESHOLD
            and float(ret60) > POSTPARABOLIC_RET60_THRESHOLD)


def _short_momentum_fires(ret5) -> bool:
    if _is_missing(ret5):
        return False
    return float(ret5) < POSTPARABOLIC_RET5_THRESHOLD


def should_exclude(dd90, ret60, ret5=None) -> tuple[bool, str | None]:
    """Return ``(True, reason)`` if either rule fires, else ``(False, None)``.

    Rule A (post-parabolic) — fires iff BOTH:
      * ``dd90 < POSTPARABOLIC_DD90_THRESHOLD`` (>~20% below the 90d high)
      * ``ret60 > POSTPARABOLIC_RET60_THRESHOLD`` (still up >~200% over 60d)

    Rule B (short-window momentum) — fires iff:
      * ``ret5 < POSTPARABOLIC_RET5_THRESHOLD`` (down >~30% over the last 5 days)

    Combined: a coin is excluded if Rule A OR Rule B fires. The returned reason
    token reflects which rule(s) fired:

      * Rule A only       → ``REASON_POST_PARABOLIC``
      * Rule B only       → ``REASON_SHORT_MOMENTUM``
      * Both A and B fire → ``REASON_BOTH``

    Fail-open per input: if a feature value is None or NaN, the rule that
    depends on it does not fire and a DEBUG-level log line is emitted. The
    other rule still evaluates on whatever inputs are available — e.g. a
    warmup-window coin with NULL ``ret5`` is still evaluated by Rule A.

    ``ret5`` defaults to ``None`` so legacy callers that pre-date Rule B
    continue to compile (Rule B then never fires for them); production code
    in ``crypto/exports/write_daily_predictions.py`` passes the real value.

    Args:
        dd90: ``drawdown_from_90d_high`` feature value (≤ 0; 0 = at the 90d high).
        ret60: ``return_60d`` feature value (e.g. 2.0 == +200%).
        ret5: ``return_5d`` feature value (e.g. -0.3 == -30%); optional.
    """
    a = _post_parabolic_fires(dd90, ret60)
    b = _short_momentum_fires(ret5)

    if not a and not b:
        # Helpful debug visibility when either input is missing — pinpoints
        # warmup-window suppressions during incident review.
        if _is_missing(dd90) or _is_missing(ret60) or _is_missing(ret5):
            logger.debug(
                "postparabolic_filter: fail-open / no rule fires "
                "(dd90=%r, ret60=%r, ret5=%r)",
                dd90, ret60, ret5,
            )
        return (False, None)

    if a and b:
        return (True, REASON_BOTH)
    if a:
        return (True, REASON_POST_PARABOLIC)
    return (True, REASON_SHORT_MOMENTUM)
