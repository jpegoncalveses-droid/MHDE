"""Tests for dashboard.services.maturity.format_equity_t2_banner — the
T-2 honest copy that labels which prediction date the dashboard is
showing relative to today (KI-149 follow-up, Step 5 of the equity
resumption queue).

Contract:
  - Always names the prediction date verbatim.
  - Always names the trading-day gap explicitly.
  - Distinguishes the architecturally-expected T-2 state from
    accidental staleness (>T-2) so the operator can tell whether
    the system is healthy or behind.
  - Doesn't claim "current" / "today" / "T-0" copy when the date is
    behind — the whole point is honesty about cadence.
"""
from __future__ import annotations

from datetime import date

from dashboard.services.maturity import format_equity_t2_banner


# 2026-05-14 is a Thursday; weekday math below uses this anchor.
THU = date(2026, 5, 14)
WED = date(2026, 5, 13)
TUE = date(2026, 5, 12)
MON = date(2026, 5, 11)
FRI_PRIOR = date(2026, 5, 8)


def test_banner_names_prediction_date_verbatim():
    text = format_equity_t2_banner(prediction_date=TUE, today=THU)
    assert "2026-05-12" in text


def test_banner_names_today_for_context():
    text = format_equity_t2_banner(prediction_date=TUE, today=THU)
    assert "2026-05-14" in text


def test_banner_at_t_2_says_t_2_cadence():
    """The architecturally-expected case: 2 trading days behind."""
    text = format_equity_t2_banner(prediction_date=TUE, today=THU)
    assert "T-2" in text


def test_banner_at_t_2_does_not_imply_current_or_today():
    text = format_equity_t2_banner(prediction_date=TUE, today=THU)
    lower = text.lower()
    assert "today's predictions" not in lower
    assert "current predictions" not in lower


def test_banner_at_t_1_does_not_claim_t_2():
    """Edge case: prediction is one trading day behind. Should not
    falsely advertise T-2; should name 1 trading day gap."""
    text = format_equity_t2_banner(prediction_date=WED, today=THU)
    assert "1 trading day" in text
    assert "T-2" not in text


def test_banner_at_t_0_does_not_claim_t_2():
    """Edge case: same-day prediction. Should not claim T-2; should
    say current / T-0."""
    text = format_equity_t2_banner(prediction_date=THU, today=THU)
    assert "T-0" in text or "current" in text.lower()
    assert "T-2" not in text


def test_banner_beyond_t_2_flags_stale():
    """When the prediction is older than the architectural T-2 target
    (e.g. 4+ trading days behind), the banner must surface the
    staleness — the operator can't see "T-2 cadence" copy and assume
    everything is on schedule."""
    text = format_equity_t2_banner(prediction_date=FRI_PRIOR, today=THU)
    # Fri 2026-05-08 → Thu 2026-05-14: skip weekend → 4 trading days.
    assert "4 trading days" in text
    assert "stale" in text.lower()
    assert "T-2" not in text


def test_banner_uses_markdown_bold_for_dates():
    """Streamlit-rendered banner should bold the prediction date so
    the operator sees the actual scoring date at a glance."""
    text = format_equity_t2_banner(prediction_date=TUE, today=THU)
    assert "**2026-05-12**" in text
