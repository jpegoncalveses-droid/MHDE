from __future__ import annotations

import streamlit as st

from dashboard.components.badges import tier_badge, confidence_badge


def candidate_card(candidate: dict) -> None:
    ticker = candidate.get("ticker", "?")
    company = candidate.get("company_name", ticker)
    tier = candidate.get("tier", "?")
    score = candidate.get("total_score", 0)
    conf = candidate.get("confidence", "low")
    why = candidate.get("why_ranked", "")

    with st.container():
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            st.markdown(f"**{ticker}** — {company}")
        with col2:
            st.markdown(tier_badge(tier))
        with col3:
            st.metric("Score", f"{score:.0f}/100")

        if why:
            st.caption(why[:200])

        sub1, sub2, sub3, sub4, sub5 = st.columns(5)
        sub1.metric("Cheap", f"{candidate.get('cheap_score', 0):.0f}")
        sub2.metric("Quality", f"{candidate.get('quality_score', 0):.0f}")
        sub3.metric("Catalyst", f"{candidate.get('catalyst_score', 0):.0f}")
        sub4.metric("Momentum", f"{candidate.get('momentum_score', 0):.0f}")
        sub5.metric("Risk", f"-{candidate.get('risk_penalty', 0):.0f}")

        st.divider()
