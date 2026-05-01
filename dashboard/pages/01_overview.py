from __future__ import annotations
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import get_overview_stats, get_candidates, get_source_health, get_health_checks
from dashboard.components.charts import score_distribution, tier_pie
from dashboard.components.tables import generic_table

require_auth()
st.title("Overview")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path, read_only=True)
    stats = get_overview_stats(conn)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Universe", stats["universe_size"])
    c2.metric("Scored", stats["candidates_scored"])
    c3.metric("A-Tier", stats["tier_a"])
    c4.metric("B-Tier", stats["tier_b"])
    c5.metric("C-Tier", stats["tier_c"])
    c6.metric("Rejected", stats["rejected"])

    st.subheader("Score & Tier Distribution")
    candidates = get_candidates(conn, run_id=stats["run_id"])
    col_a, col_b = st.columns(2)
    with col_a:
        score_distribution(candidates)
    with col_b:
        tier_pie({
            "A": stats["tier_a"], "B": stats["tier_b"],
            "C": stats["tier_c"], "Reject": stats["rejected"],
        })

    st.subheader("Top 10 Candidates")
    top10 = candidates[:10]
    for c in top10:
        from dashboard.components.candidate_cards import candidate_card
        candidate_card(c)

    st.subheader("Source Status")
    sources = get_source_health(conn)
    generic_table(sources)

    st.subheader("Recent Health Checks")
    checks = get_health_checks(conn)
    generic_table(checks)

    if stats.get("feature_coverage_pct") is not None:
        st.metric("Feature Coverage", f"{stats['feature_coverage_pct']:.0f}%")

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
