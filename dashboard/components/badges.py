from __future__ import annotations

import streamlit as st

_TIER_COLORS = {"A": "🟢", "B": "🔵", "C": "🟡", "Reject": "🔴"}
_STATUS_COLORS = {
    "pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️",
    "ok": "✅", "error": "❌", "stub": "⚫",
}


def tier_badge(tier: str) -> str:
    return f"{_TIER_COLORS.get(tier, '⚪')} {tier}"


def status_badge(status: str) -> str:
    return f"{_STATUS_COLORS.get(status.lower(), '❓')} {status}"


def confidence_badge(conf: str) -> str:
    colors = {"high": "🟢", "medium": "🟡", "low": "🔴"}
    return f"{colors.get(conf, '❓')} {conf}"


def pct_delta(value: float | None, label: str = "") -> None:
    if value is None:
        st.caption(f"{label}: —")
        return
    color = "normal" if value >= 0 else "inverse"
    st.metric(label, f"{value:.1%}", delta=f"{value:.1%}", delta_color=color)
