from __future__ import annotations
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import get_candidate_detail, get_latest_run_id
from dashboard.components.badges import tier_badge, confidence_badge
from dashboard.components.charts import price_chart
from dashboard.components.tables import features_table

require_auth()
st.title("Candidate Detail")

ticker = st.session_state.get("selected_ticker") or st.query_params.get("ticker")
run_id = st.session_state.get("selected_run_id")

if not ticker:
    ticker = st.text_input("Enter ticker")

if not ticker:
    st.info("Select a candidate from the Candidates page, or enter a ticker above.")
    st.stop()

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path)
    if not run_id:
        run_id = get_latest_run_id(conn)
    detail = get_candidate_detail(conn, ticker, run_id)
    conn.close()

    if not detail:
        st.warning(f"No data found for {ticker} in run {run_id}.")
        st.stop()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Ticker", ticker)
    col2.markdown(f"**Tier:** {tier_badge(detail.get('tier', '?'))}")
    col3.metric("Total Score", f"{detail.get('total_score', 0):.0f}/100")
    col4.markdown(f"**Confidence:** {confidence_badge(detail.get('confidence', 'low'))}")

    st.subheader(detail.get("company_name", ticker))

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Thesis", "Evidence", "LLM Brief", "Prices", "Outcomes"])

    with tab1:
        st.write("**Why ranked:**", detail.get("why_ranked", "—"))
        st.write("**Thesis:**", detail.get("thesis", "—"))
        st.write("**Why now:**", detail.get("why_now", "—"))

    with tab2:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Cheap Evidence**")
            for e in detail.get("cheap_evidence", []):
                st.write(f"• {e}")
            st.markdown("**Quality Evidence**")
            for e in detail.get("quality_evidence", []):
                st.write(f"• {e}")
        with c2:
            st.markdown("**Catalyst Evidence**")
            for e in detail.get("catalyst_evidence", []):
                st.write(f"• {e}")
            st.markdown("**Risks**")
            for r in detail.get("risks", []):
                st.write(f"• {r}")
        st.markdown("**Missing Evidence**")
        for m in detail.get("missing_evidence", []):
            st.write(f"• {m}")

        st.subheader("Features")
        features_table(detail.get("features", []))

    with tab3:
        st.write("**Provider:**", detail.get("llm_provider", "—"))
        st.write("**Model:**", detail.get("llm_model", "—"))
        st.write("**LLM Thesis:**", detail.get("llm_thesis", "—"))
        st.write("**Confidence:**", detail.get("llm_confidence", "—"))
        st.write("**Action:**", detail.get("llm_action", "—"))
        if detail.get("llm_error"):
            st.error(f"LLM error: {detail['llm_error']}")

    with tab4:
        price_chart(detail.get("prices", []), ticker)

    with tab5:
        outcome = detail.get("outcome", {})
        if outcome:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("20d Return", f"{outcome.get('forward_return_20d', 0) or 0:.1%}")
            c2.metric("60d Return", f"{outcome.get('forward_return_60d', 0) or 0:.1%}")
            c3.metric("Max Drawdown 20d", f"{outcome.get('max_drawdown_20d', 0) or 0:.1%}")
            c4.metric("Max Runup 20d", f"{outcome.get('max_runup_20d', 0) or 0:.1%}")
            st.write("**Review status:**", outcome.get("review_status", "pending"))
            st.write("**Notes:**", outcome.get("review_notes", "—"))
        else:
            st.info("No outcome data yet. Returns are computed once price data accumulates.")

except Exception as exc:
    st.error(f"Error: {exc}")
