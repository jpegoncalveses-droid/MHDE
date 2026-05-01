from __future__ import annotations
import os
import duckdb
import streamlit as st
import pandas as pd

from dashboard.auth import require_auth
from dashboard.services.queries import get_outcomes
from dashboard.services.actions import update_outcome_review
from dashboard.components.tables import generic_table

require_auth()
st.title("Candidate Outcomes")
st.caption("Tracks what happened after MHDE surfaced each candidate. Not paper trading.")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path, read_only=True)
    outcomes = get_outcomes(conn, limit=500)

    if not outcomes:
        st.info("No outcome records yet. Outcomes are created when candidates are scored and updated as price data accumulates.")
        conn.close()
        st.stop()

    df = pd.DataFrame(outcomes)
    numeric_cols = ["total_score", "reference_price", "forward_return_20d", "forward_return_60d",
                    "max_drawdown_20d", "max_runup_20d"]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = df[c].round(4)

    st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("Outcome Statistics by Tier")
    stats = conn.execute(
        """
        SELECT tier,
               COUNT(*) as count,
               AVG(forward_return_20d) as avg_20d_return,
               AVG(forward_return_60d) as avg_60d_return,
               AVG(max_drawdown_20d) as avg_drawdown,
               COUNT(CASE WHEN forward_return_20d > 0 THEN 1 END) * 100.0 / COUNT(*) as hit_rate_pct
        FROM candidate_outcomes
        WHERE forward_return_20d IS NOT NULL
        GROUP BY tier ORDER BY tier
        """
    ).fetchall()
    if stats:
        st.dataframe(
            pd.DataFrame(stats, columns=["Tier", "Count", "Avg 20d Return", "Avg 60d Return",
                                          "Avg Drawdown", "Hit Rate %"]).round(3),
            use_container_width=True, hide_index=True,
        )

    st.subheader("Review Outcome")
    if outcomes:
        labels = [f"{o['ticker']} ({o['as_of_date']}) — {o['candidate_id'][:8]}" for o in outcomes]
        idx = st.selectbox("Select outcome", range(len(labels)), format_func=lambda i: labels[i])
        if idx is not None:
            o = outcomes[idx]
            new_status = st.selectbox("Review status", [
                "pending", "validated", "false_positive",
                "needs_more_time", "invalid_due_to_data_issue", "archived",
            ])
            notes = st.text_area("Notes")
            if st.button("Save Review"):
                if update_outcome_review(o["candidate_id"], new_status, notes):
                    st.success("Saved.")
                else:
                    st.error("Failed.")

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
