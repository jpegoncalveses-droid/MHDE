"""Period alignment utilities for growth feature guards."""
from __future__ import annotations

from datetime import date

# Day ranges for detecting period types from the gap between two period-end dates.
_ANNUAL_GAP = (335, 395)     # ~365 ± 30
_QUARTERLY_GAP = (75, 105)   # ~90 ± 15

# Forms that indicate annual filings
_ANNUAL_FORMS = {"10-K", "10-KSB", "20-F", "40-F", "10-KT"}
# Forms that indicate quarterly filings
_QUARTERLY_FORMS = {"10-Q", "10-QSB", "6-K", "10-QT"}


def _form_period_type(form: str | None) -> str | None:
    if not form:
        return None
    f = form.strip().upper()
    if f in _ANNUAL_FORMS:
        return "annual"
    if f in _QUARTERLY_FORMS:
        return "quarterly"
    return None


def _gap_period_type(gap_days: int) -> str | None:
    if _ANNUAL_GAP[0] <= gap_days <= _ANNUAL_GAP[1]:
        return "annual"
    if _QUARTERLY_GAP[0] <= gap_days <= _QUARTERLY_GAP[1]:
        return "quarterly"
    return None


def check_period_alignment(
    current_end: date,
    current_form: str | None,
    prior_end: date,
    prior_form: str | None,
) -> dict:
    """Return period alignment metadata for a growth comparison.

    Returns a dict with:
        period_alignment_status  — 'aligned' | 'mismatched'
        period_type              — 'annual' | 'quarterly' | None
        detection_method         — 'form' | 'gap'
        current_period_end       — ISO date string
        prior_period_end         — ISO date string
        gap_days                 — int
        current_form             — str | None
        prior_form               — str | None
    """
    gap_days = abs((current_end - prior_end).days)

    current_type = _form_period_type(current_form)
    prior_type = _form_period_type(prior_form)

    if current_type and prior_type:
        aligned = current_type == prior_type
        period_type = current_type if aligned else None
        detection = "form"
    else:
        period_type = _gap_period_type(gap_days)
        aligned = period_type is not None
        detection = "gap"

    return {
        "period_alignment_status": "aligned" if aligned else "mismatched",
        "period_type": period_type,
        "detection_method": detection,
        "current_period_end": current_end.isoformat(),
        "prior_period_end": prior_end.isoformat(),
        "gap_days": gap_days,
        "current_form": current_form,
        "prior_form": prior_form,
    }
