"""Dashboard page 15 — Shadow Models."""
import json

import streamlit as st

from dashboard.auth import require_auth
from storage.db import get_connection, init_schema

require_auth()
st.set_page_config(page_title="Shadow Models", layout="wide")
st.title("Shadow Models")

st.warning(
    "Shadow model scores are **not used for production alerts or rankings**. "
    "They are experimental only."
)


@st.cache_resource
def _conn():
    c = get_connection()
    init_schema(c)
    return c


conn = _conn()


def _load_model_runs():
    try:
        rows = conn.execute(
            """SELECT model_run_id, model_type, target, status, warning,
                      metrics_json, feature_importance_json, created_at
               FROM model_runs
               WHERE model_type LIKE '%shadow%'
               ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()
        cols = ["model_run_id", "model_type", "target", "status", "warning",
                "metrics_json", "feature_importance_json", "created_at"]
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            for f in ("metrics_json", "feature_importance_json"):
                try:
                    d[f] = json.loads(d[f]) if d[f] else {}
                except Exception:
                    d[f] = {}
            result.append(d)
        return result
    except Exception:
        return []


def _load_gate_results():
    try:
        rows = conn.execute(
            """SELECT experiment_id, gate_name, status, passed, metric_value, threshold, notes
               FROM promotion_gate_results
               ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
        cols = ["experiment_id", "gate_name", "status", "passed",
                "metric_value", "threshold", "notes"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []


runs = _load_model_runs()
gates = _load_gate_results()

# Model runs
st.subheader("Shadow model runs")
if runs:
    for run in runs[:5]:
        with st.expander(f"{run['model_run_id']} — {run['model_type']} ({run['created_at']})"):
            st.json(run["metrics_json"])
            if run["feature_importance_json"]:
                import pandas as pd
                fi = run["feature_importance_json"]
                df_fi = pd.DataFrame(
                    sorted(fi.items(), key=lambda x: -x[1]),
                    columns=["feature", "importance"],
                )
                st.bar_chart(df_fi.set_index("feature"))
else:
    st.info("No shadow model runs yet. Run: python main.py train xgboost-shadow")

st.divider()

# Promotion gates
st.subheader("Promotion gate results")
if gates:
    import pandas as pd
    df_g = pd.DataFrame(gates)
    df_g["passed"] = df_g["passed"].map({True: "✓", False: "✗", None: "?"})
    st.dataframe(df_g, use_container_width=True)
else:
    st.info("No promotion gate results yet.")
