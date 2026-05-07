from __future__ import annotations
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import get_backtest_runs
from dashboard.components.tables import generic_table

require_auth()
st.title("Backtests")
st.warning(
    "Experimental. Not validated for decision use. "
    "Accumulate several weeks of daily runs before interpreting results."
)

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path)
    runs = get_backtest_runs(conn)

    if not runs:
        st.info("No backtest runs yet. Run 'python main.py backtest smoke'.")
    else:
        generic_table(runs, round_cols=["hit_rate", "avg_return"])
        if runs[0].get("warning"):
            st.caption(runs[0]["warning"])

    model_runs = conn.execute(
        "SELECT model_run_id, model_type, status, warning, metrics_json, created_at FROM model_runs ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    if model_runs:
        st.subheader("XGBoost Model Runs")
        import json, pandas as pd
        rows = []
        for r in model_runs:
            metrics = json.loads(r[4]) if r[4] else {}
            rows.append({
                "model_run_id": r[0], "type": r[1], "status": r[2],
                "accuracy": metrics.get("accuracy"), "auc": metrics.get("auc"),
                "created_at": r[5],
            })
        st.dataframe(pd.DataFrame(rows).round(4), use_container_width=True, hide_index=True)

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
