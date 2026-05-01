from __future__ import annotations
import json
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import get_llm_runs
from dashboard.components.tables import generic_table

require_auth()
st.title("LLM Audit")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path, read_only=True)
    runs = get_llm_runs(conn, limit=200)
    generic_table(runs)

    if runs:
        st.subheader("Inspect LLM call")
        labels = [f"{r['ticker']} — {r['provider']}/{r['model']} ({r['llm_run_id'][:8]})" for r in runs]
        selected_idx = st.selectbox("Select call", range(len(labels)), format_func=lambda i: labels[i])
        if selected_idx is not None:
            r = runs[selected_idx]
            detail = conn.execute(
                "SELECT input_json, output_json, error_message FROM llm_runs WHERE llm_run_id = ?",
                [r["llm_run_id"]],
            ).fetchone()
            if detail:
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**Input**")
                    try:
                        st.json(json.loads(detail[0]) if detail[0] else {})
                    except Exception:
                        st.text(detail[0])
                with col2:
                    st.markdown("**Output**")
                    try:
                        st.json(json.loads(detail[1]) if detail[1] else {})
                    except Exception:
                        st.text(detail[1])
                if detail[2]:
                    st.error(f"Error: {detail[2]}")

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
