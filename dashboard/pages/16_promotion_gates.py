"""Dashboard page 16 — Promotion Gates."""
import json

import streamlit as st

from dashboard.auth import require_auth
from storage.db import get_connection, init_schema

require_auth()
st.set_page_config(page_title="Promotion Gates", layout="wide")
st.title("Promotion Gates")

st.info(
    "**auto_apply_enabled = False** — no experiment may be applied automatically. "
    "All gates must pass before an experiment is eligible for human review and promotion."
)


@st.cache_resource
def _conn():
    c = get_connection()
    init_schema(c)
    return c


conn = _conn()


def _load_experiments_with_gates():
    try:
        exps = conn.execute(
            """SELECT e.experiment_id, e.hypothesis, e.status, e.approved_by,
                      e.applied_by, e.applied_at,
                      COUNT(g.gate_result_id) as gate_count,
                      SUM(CASE WHEN g.passed THEN 1 ELSE 0 END) as gates_passed
               FROM scorecard_experiments e
               LEFT JOIN promotion_gate_results g ON e.experiment_id = g.experiment_id
               GROUP BY e.experiment_id, e.hypothesis, e.status, e.approved_by,
                        e.applied_by, e.applied_at
               ORDER BY e.created_at DESC LIMIT 30"""
        ).fetchall()
        cols = ["experiment_id", "hypothesis", "status", "approved_by",
                "applied_by", "applied_at", "gate_count", "gates_passed"]
        return [dict(zip(cols, r)) for r in exps]
    except Exception:
        return []


def _load_gate_detail(experiment_id: str):
    try:
        rows = conn.execute(
            """SELECT gate_name, status, passed, metric_value, threshold, notes
               FROM promotion_gate_results WHERE experiment_id=?
               ORDER BY gate_name""",
            [experiment_id],
        ).fetchall()
        cols = ["gate_name", "status", "passed", "metric_value", "threshold", "notes"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []


experiments = _load_experiments_with_gates()

if experiments:
    import pandas as pd
    for exp in experiments:
        gate_str = f"{exp['gates_passed']}/{exp['gate_count']}" if exp["gate_count"] else "not run"
        header = f"[{exp['status']}] {exp['experiment_id'][:12]}… — gates: {gate_str}"
        with st.expander(header):
            st.markdown(f"**Hypothesis:** {exp['hypothesis']}")
            st.markdown(f"**Status:** `{exp['status']}` | **Approved by:** {exp.get('approved_by') or '—'}")
            if exp.get("applied_at"):
                st.markdown(f"**Applied at:** {exp['applied_at']} by {exp.get('applied_by')}")

            gates = _load_gate_detail(exp["experiment_id"])
            if gates:
                df_g = pd.DataFrame(gates)
                df_g["passed"] = df_g["passed"].map({True: "✓ pass", False: "✗ fail", None: "— skip"})
                st.dataframe(df_g, use_container_width=True)
            else:
                st.caption("No gate results. Run: python main.py (gates not yet implemented in CLI)")
else:
    st.info("No experiments yet. Experiments are proposed via the learning loop or missed-opportunity attribution.")

st.divider()
st.subheader("Gate definitions")
from models.promotion_gates import GATES, AUTO_APPLY_ENABLED
st.markdown(f"**auto_apply_enabled:** `{AUTO_APPLY_ENABLED}`")
for g in GATES:
    st.markdown(f"- `{g}`")
