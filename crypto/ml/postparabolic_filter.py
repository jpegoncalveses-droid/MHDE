"""Post-parabolic exclusion filter — a pre-order-entry risk gate.

Suppresses crypto buy signals on coins that sit deep in a drawdown from their
90-day high while still up massively over 60 days — the documented
post-parabolic re-entry bias (SKYAI). The model's probability isn't *wrong*
(such coins do tag +10% — that's the volatility-loving threshold label), it's
optimising the wrong objective, so the right response is a binary risk gate,
not a probability haircut. Applied in the prediction export step (option (b));
the raw signal in ``crypto_ml_predictions`` is left untouched.

Pure logic: no DB I/O, no imports from ``crypto.exports`` or the dashboard.
See ``crypto/ml/POSTPARABOLIC_FILTER_SPEC.md``.
"""
from __future__ import annotations

import logging
import math

from crypto.config import POSTPARABOLIC_DD90_THRESHOLD, POSTPARABOLIC_RET60_THRESHOLD

logger = logging.getLogger("mhde.crypto.postparabolic_filter")

#: Canonical, stable reason token written to ``crypto_signal_exclusions.reason``
#: and emitted in the export log. Stays constant if the thresholds are retuned
#: (the actual values are stored in their own columns), so it groups cleanly.
REASON = "post_parabolic"


def _is_missing(x) -> bool:
    if x is None:
        return True
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def should_exclude(dd90, ret60) -> tuple[bool, str | None]:
    """Return ``(True, REASON)`` if the coin is post-parabolic, else ``(False, None)``.

    A coin is excluded iff **both** strict conditions hold:
      * ``dd90 < POSTPARABOLIC_DD90_THRESHOLD``   (more than ~20% below the 90d high)
      * ``ret60 > POSTPARABOLIC_RET60_THRESHOLD`` (still up more than ~200% over 60d)

    Fail-open: if either input is ``None`` or NaN, returns ``(False, None)`` and
    logs at DEBUG — a coin we can't evaluate gets the benefit of the doubt (the
    model's other features still gate it; this is the warmup-window case).

    Args:
        dd90: ``drawdown_from_90d_high`` feature value (≤ 0; 0 = at the 90d high).
        ret60: ``return_60d`` feature value (e.g. 2.0 == +200%).
    """
    if _is_missing(dd90) or _is_missing(ret60):
        logger.debug(
            "postparabolic_filter: fail-open — missing feature (dd90=%r, ret60=%r)",
            dd90, ret60,
        )
        return (False, None)
    if float(dd90) < POSTPARABOLIC_DD90_THRESHOLD and float(ret60) > POSTPARABOLIC_RET60_THRESHOLD:
        return (True, REASON)
    return (False, None)
