"""MHDE Dashboard — main entry point for streamlit."""
from __future__ import annotations

import os

import streamlit as st

from dashboard.auth import require_auth

st.set_page_config(
    page_title="MHDE — Market Hypothesis Discovery Engine",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_auth()

st.title("📊 MHDE — Market Hypothesis Discovery Engine")
st.caption("Discover, explain, track, and review market hypotheses.")

import duckdb

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")

try:
    conn = duckdb.connect(db_path, read_only=True)
    from dashboard.services.queries import get_overview_stats
    stats = get_overview_stats(conn)
    conn.close()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Universe", stats["universe_size"])
    col2.metric("Candidates Scored", stats["candidates_scored"])
    col3.metric("A-Tier", stats["tier_a"])
    col4.metric("Alerts Sent", stats["alerts_sent"])
    col5.metric("Health Warnings", stats["health_warnings"])

    if stats["run_id"]:
        st.caption(f"Latest run: `{stats['run_id']}`")
    else:
        st.info("No runs yet. Run `python main.py run daily-radar` to populate the database.")

except Exception as exc:
    st.error(f"Could not connect to database: {exc}")
    st.info(f"DB path: `{db_path}`")

st.markdown("""
---
**Navigation** — use the sidebar pages to explore:

- **Overview** — pipeline summary, score distribution, source health
- **Candidates** — filterable candidate table
- **Candidate Detail** — evidence, LLM thesis, prices, outcome
- **Company Detail** — filings, fundamentals, history
- **Scores & Features** — feature coverage and values
- **Hypotheses** — all hypotheses with review actions
- **Sources & Health** — source run history and health checks
- **LLM Audit** — all LLM calls with inputs/outputs
- **Alerts** — notification history
- **Outcomes** — forward return tracking
- **Backtests** — smoke backtest results
- **Governance** — source registry, scorecard, prompt versions

---
*MHDE is a research tool. It is not financial advice. All outputs are research leads requiring human review.*
""")
