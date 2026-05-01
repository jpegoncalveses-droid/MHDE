from __future__ import annotations

import streamlit as st


def candidate_filters() -> dict:
    with st.sidebar:
        st.header("Filters")
        tier = st.selectbox("Tier", ["All", "A", "B", "C", "Reject"], index=0)
        search = st.text_input("Search ticker / company")
        score_range = st.slider("Score range", 0, 100, (0, 100))
    return {
        "tier": None if tier == "All" else tier,
        "search": search or None,
        "min_score": score_range[0],
        "max_score": score_range[1],
    }


def run_selector(conn, sidebar: bool = True) -> str | None:
    from dashboard.services.queries import get_latest_run_id
    rows = conn.execute(
        "SELECT DISTINCT run_id, MIN(created_at) FROM scores GROUP BY run_id ORDER BY MIN(created_at) DESC LIMIT 20"
    ).fetchall()
    if not rows:
        return None
    options = [r[0] for r in rows]
    ctx = st.sidebar if sidebar else st
    return ctx.selectbox("Run ID", options, index=0)
