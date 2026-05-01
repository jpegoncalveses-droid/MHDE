from __future__ import annotations
import os
import duckdb
import streamlit as st
import pandas as pd

from dashboard.auth import require_auth
from dashboard.components.filters import run_selector

require_auth()
st.title("Scores & Features")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path, read_only=True)
    run_id = run_selector(conn)

    st.subheader("Feature Coverage")
    coverage = conn.execute(
        """
        SELECT feature_group,
               COUNT(*) as total,
               COUNT(feature_value) as non_null,
               COUNT(feature_value) * 100.0 / COUNT(*) as coverage_pct
        FROM features WHERE run_id = ?
        GROUP BY feature_group ORDER BY feature_group
        """,
        [run_id or ""],
    ).fetchall()
    if coverage:
        st.dataframe(
            pd.DataFrame(coverage, columns=["Group", "Total", "Non-Null", "Coverage %"]).round(1),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No feature data. Run 'python main.py score' first.")

    st.subheader("Feature Values by Ticker")
    group = st.selectbox("Feature group", [
        "valuation", "quality", "catalyst", "momentum", "sentiment", "risk", "macro"
    ])
    rows = conn.execute(
        """
        SELECT ticker, feature_name, feature_value, feature_score, confidence
        FROM features WHERE run_id = ? AND feature_group = ?
        ORDER BY ticker, feature_name
        LIMIT 500
        """,
        [run_id or "", group],
    ).fetchall()
    if rows:
        st.dataframe(
            pd.DataFrame(rows, columns=["Ticker", "Feature", "Value", "Score", "Confidence"]).round(3),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info(f"No {group} features found.")

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
