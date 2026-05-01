from __future__ import annotations

import os

import duckdb
import pandas as pd
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import (
    get_candidate_reviews,
    get_scorecard_experiments,
)
from learning.calibration import (
    false_positive_reasons,
    feature_coverage_vs_confidence,
    llm_confidence_vs_human_review,
    outcome_by_review_status,
    outcome_by_score_bucket,
    outcome_by_tier,
    score_component_vs_outcome,
    source_failure_impact,
)
from learning.error_taxonomy import FALSE_POSITIVE_REASONS, REVIEW_STATUSES
from learning.feedback import submit_review
from learning.insights import generate_insights

require_auth()
st.title("Learning & Calibration")
st.caption(
    "MHDE learns through outcome tracking, human review, and structured error taxonomy. "
    "Experiments are proposed here but never applied automatically."
)

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")

try:
    conn_ro = duckdb.connect(db_path, read_only=True)
except Exception as e:
    st.error(f"Cannot connect to database: {e}")
    st.stop()

review_count = conn_ro.execute("SELECT COUNT(*) FROM candidate_reviews").fetchone()[0]
outcome_count = conn_ro.execute("SELECT COUNT(*) FROM candidate_outcomes").fetchone()[0]

st.metric("Outcomes tracked", outcome_count)
st.metric("Reviews completed", review_count)

if review_count < 5 or outcome_count < 5:
    st.warning(
        "Insufficient outcome/review history for reliable calibration. "
        "Continue running daily-radar and submitting reviews below."
    )

# ── Outcome by tier ───────────────────────────────────────────────────────────
st.header("Outcome by Tier")
tier_data = outcome_by_tier(conn_ro)
if tier_data:
    df_tier = pd.DataFrame(tier_data)
    for col in ("avg_return_20d", "avg_return_60d", "avg_drawdown_20d"):
        if col in df_tier.columns:
            df_tier[col] = df_tier[col].map(lambda x: f"{x:.1%}" if x is not None else "N/A")
    st.dataframe(df_tier, use_container_width=True)
else:
    st.info("No outcome data yet.")

# ── Outcome by score bucket ───────────────────────────────────────────────────
st.header("Outcome by Score Bucket")
bucket_data = outcome_by_score_bucket(conn_ro)
if bucket_data:
    df_bucket = pd.DataFrame(bucket_data)
    for col in ("avg_return_20d", "avg_return_60d"):
        if col in df_bucket.columns:
            df_bucket[col] = df_bucket[col].map(lambda x: f"{x:.1%}" if x is not None else "N/A")
    st.dataframe(df_bucket, use_container_width=True)
else:
    st.info("No score/outcome link data yet.")

# ── Review status breakdown ───────────────────────────────────────────────────
st.header("Outcome by Review Status")
review_status_data = outcome_by_review_status(conn_ro)
if review_status_data:
    st.dataframe(pd.DataFrame(review_status_data), use_container_width=True)
else:
    st.info("No reviews submitted yet.")

# ── False-positive taxonomy ───────────────────────────────────────────────────
st.header("False-Positive Reasons")
fp_data = false_positive_reasons(conn_ro)
if fp_data:
    df_fp = pd.DataFrame(fp_data)
    st.bar_chart(df_fp.set_index("reason")["count"])
else:
    st.info("No false-positive reviews recorded.")

# ── Score components vs outcome ───────────────────────────────────────────────
st.header("Score Components vs Forward Return")
sco_data = score_component_vs_outcome(conn_ro)
if sco_data:
    st.dataframe(pd.DataFrame(sco_data), use_container_width=True)
else:
    st.info("No linked score/outcome data yet.")

# ── Feature coverage ──────────────────────────────────────────────────────────
st.header("Feature Coverage")
cov_data = feature_coverage_vs_confidence(conn_ro)
if cov_data:
    df_cov = pd.DataFrame(cov_data)
    st.dataframe(df_cov[["ticker", "coverage_pct", "total_features", "high_conf_features"]].head(20),
                 use_container_width=True)
else:
    st.info("No feature data.")

# ── Source reliability ────────────────────────────────────────────────────────
st.header("Source Reliability")
src_data = source_failure_impact(conn_ro)
if src_data:
    st.dataframe(pd.DataFrame(src_data), use_container_width=True)
else:
    st.info("No source run data.")

# ── LLM vs human review ───────────────────────────────────────────────────────
st.header("LLM vs Human Review")
llm_data = llm_confidence_vs_human_review(conn_ro)
if llm_data:
    st.dataframe(pd.DataFrame(llm_data), use_container_width=True)
else:
    st.info("No linked LLM/review data yet.")

# ── Insights ──────────────────────────────────────────────────────────────────
st.header("Suggested Experiments & Insights")
insights = generate_insights(conn_ro)
if insights:
    for ins in insights:
        color = "🔴" if ins["severity"] == "high" else "🟡" if ins["severity"] == "medium" else "🟢"
        with st.expander(f"{color} [{ins['category']}] {ins['message'][:80]}..."):
            st.write(ins["message"])
            exp = ins.get("suggested_experiment")
            if exp:
                st.markdown(f"**Suggested change:** {exp['hypothesis']}")
                st.markdown(f"**Expected effect:** {exp['expected_effect']}")
                st.markdown(f"**Affected:** {', '.join(exp['affected_components'])}")
                st.info("Status: proposed — not applied. Requires human approval.")
else:
    st.info("No insights generated yet (insufficient review data or no patterns detected).")

# ── Experiment history ────────────────────────────────────────────────────────
st.header("Experiment History")
experiments = get_scorecard_experiments(conn_ro)
if experiments:
    df_exp = pd.DataFrame(experiments)[["experiment_id", "status", "hypothesis", "expected_effect",
                                        "approved_by", "created_at"]]
    st.dataframe(df_exp, use_container_width=True)
else:
    st.info("No experiments recorded yet.")

# ── Submit review form ────────────────────────────────────────────────────────
st.header("Submit Candidate Review")
st.caption("Review a candidate's quality regardless of its price outcome.")

with st.form("review_form"):
    col1, col2 = st.columns(2)
    with col1:
        review_run_id = st.text_input("Run ID")
        review_ticker = st.text_input("Ticker").upper()
        review_status = st.selectbox("Review Status", REVIEW_STATUSES, index=0)
    with col2:
        usefulness = st.selectbox("Usefulness (1-5)", [None, 1, 2, 3, 4, 5], index=0)
        thesis_q = st.selectbox("Thesis Quality (1-5)", [None, 1, 2, 3, 4, 5], index=0)
        evidence_q = st.selectbox("Evidence Quality (1-5)", [None, 1, 2, 3, 4, 5], index=0)
    fp_reason = st.selectbox("False Positive Reason", [None] + FALSE_POSITIVE_REASONS, index=0)
    missed_risk = st.text_input("Missed Risk (optional)")
    missing_evidence = st.text_input("Missing Evidence (optional)")
    review_notes = st.text_area("Review Notes (optional)")
    reviewed_by = st.text_input("Reviewed By (optional)")
    submitted = st.form_submit_button("Submit Review")

if submitted:
    if not review_run_id or not review_ticker:
        st.error("Run ID and Ticker are required.")
    elif review_status not in REVIEW_STATUSES:
        st.error(f"Invalid review status: {review_status}")
    else:
        try:
            conn_rw = duckdb.connect(db_path, read_only=False)
            submit_review(
                conn_rw,
                run_id=review_run_id,
                ticker=review_ticker,
                review_status=review_status,
                usefulness_score=usefulness,
                thesis_quality_score=thesis_q,
                evidence_quality_score=evidence_q,
                false_positive_reason=fp_reason,
                missed_risk=missed_risk or None,
                missing_evidence=missing_evidence or None,
                review_notes=review_notes or None,
                reviewed_by=reviewed_by or None,
            )
            conn_rw.close()
            st.success(f"Review submitted for {review_ticker} (run_id={review_run_id})")
            st.rerun()
        except Exception as e:
            st.error(f"Failed to submit review: {e}")

# ── Recent reviews table ──────────────────────────────────────────────────────
st.header("Recent Reviews")
reviews = get_candidate_reviews(conn_ro, limit=50)
if reviews:
    st.dataframe(pd.DataFrame(reviews), use_container_width=True)
else:
    st.info("No reviews submitted yet. Use the form above.")

conn_ro.close()
