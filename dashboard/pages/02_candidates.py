from __future__ import annotations
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import get_candidates
from dashboard.components.filters import candidate_filters, run_selector
from dashboard.components.tables import scores_table

require_auth()
st.title("Candidates")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path, read_only=True)
    filters = candidate_filters()
    run_id = run_selector(conn)
    candidates = get_candidates(
        conn, run_id=run_id,
        tier=filters["tier"],
        min_score=filters["min_score"],
        max_score=filters["max_score"],
        search=filters["search"],
    )
    st.caption(f"{len(candidates)} candidates")
    scores_table(candidates)

    if candidates:
        st.subheader("Select candidate for detail")
        tickers = [c["ticker"] for c in candidates]
        selected = st.selectbox("Ticker", tickers)
        if selected:
            st.page_link(
                "pages/03_candidate_detail.py",
                label=f"Open detail for {selected} →",
            )
            st.session_state["selected_ticker"] = selected
            st.session_state["selected_run_id"] = run_id

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
