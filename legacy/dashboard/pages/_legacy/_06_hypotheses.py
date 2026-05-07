from __future__ import annotations
import os
import duckdb
import streamlit as st
import pandas as pd

from dashboard.auth import require_auth
from dashboard.services.queries import get_hypotheses
from dashboard.services.actions import update_hypothesis_status

require_auth()
st.title("Hypotheses")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path)
    hypotheses = get_hypotheses(conn)
    conn.close()

    if not hypotheses:
        st.info("No hypotheses yet. Run 'python main.py run daily-radar'.")
        st.stop()

    df = pd.DataFrame(hypotheses)
    display_cols = ["ticker", "company_name", "tier", "total_score", "confidence", "status", "review_status", "created_at"]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(df[available].round(1), use_container_width=True, hide_index=True)

    st.subheader("Update Hypothesis Status")
    hyp_ids = [h["hypothesis_id"] for h in hypotheses]
    labels = [f"{h['ticker']} ({h['tier']}) — {h['hypothesis_id'][:8]}" for h in hypotheses]
    selected_idx = st.selectbox("Select hypothesis", range(len(labels)), format_func=lambda i: labels[i])
    if selected_idx is not None:
        hyp = hypotheses[selected_idx]
        st.json({"ticker": hyp["ticker"], "thesis": hyp.get("thesis", "")[:300]})
        new_status = st.selectbox("New status", ["new", "watch", "research", "rejected", "archived"])
        if st.button("Update Status"):
            if update_hypothesis_status(hyp["hypothesis_id"], new_status):
                st.success(f"Updated {hyp['ticker']} to {new_status}")
            else:
                st.error("Update failed.")

except Exception as exc:
    st.error(f"Error: {exc}")
