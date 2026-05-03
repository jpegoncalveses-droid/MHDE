"""MHDE Dashboard — Prediction vs Actual Learning Summary page."""
from __future__ import annotations

import csv
import os
from pathlib import Path

import streamlit as st

from dashboard.auth import require_auth

st.set_page_config(page_title="Learning — MHDE", layout="wide")
require_auth()

st.title("Prediction vs Actual — Learning Summary")
st.caption("Shadow-only: no production scores were changed.")

output_dir = os.environ.get("MHDE_OUTPUT_DIR", "data/processed")

from dashboard.services.learning_stats import get_learning_stats
lstats = get_learning_stats(output_dir)

if not lstats["total"]:
    st.info(
        "No prediction report found. "
        "Run `python main.py missed prediction-vs-actual` to generate."
    )
    st.stop()

if lstats["report_date"]:
    st.caption(f"Report date: `{lstats['report_date']}`")

# ── Classification summary ────────────────────────────────────────────────────

st.subheader("Classification Summary")
cc1, cc2, cc3, cc4, cc5 = st.columns(5)
cc1.metric("Total Events",    lstats["total"])
cc2.metric("True Miss",       lstats["true_miss"])
cc3.metric("Near Threshold",  lstats["near_threshold"])
cc4.metric("Scored Missed",   lstats["scored_missed"])
cc5.metric("Scored Correct",  lstats["scored_correct"])

clf_data = {k: lstats[k] for k in
    ["true_miss", "near_threshold", "scored_missed", "scored_correct",
     "universe_miss", "unscored_mover"]}
st.dataframe(
    [{"Classification": k, "Count": v} for k, v in clf_data.items()],
    use_container_width=True,
    hide_index=True,
)

# ── Root cause breakdown ──────────────────────────────────────────────────────

st.subheader("Root Cause Breakdown")
rc_rows = [
    {"Group": k, "Count": v}
    for k, v in sorted(lstats["rc_groups"].items(), key=lambda x: -x[1])
    if v > 0
]
if rc_rows:
    st.dataframe(rc_rows, use_container_width=True, hide_index=True)
else:
    st.info("No enriched root-cause data. Run `python main.py missed enrich-root-causes`.")

# ── Top missed rows ───────────────────────────────────────────────────────────

enriched_path = Path(output_dir) / "prediction_vs_actual_enriched_rows.csv"
if enriched_path.exists():
    st.subheader("Top True Misses / Near Threshold")
    with open(enriched_path, newline="") as f:
        enriched = list(csv.DictReader(f))
    key_rows = [
        r for r in enriched
        if r.get("classification") in ("true_miss", "scored_missed", "near_threshold")
    ]
    if key_rows:
        display = [
            {
                "Ticker": r.get("ticker", ""),
                "Classification": r.get("classification", ""),
                "Score": r.get("score_before_event", ""),
                "Root Cause": r.get("enriched_root_cause", ""),
                "Suggested Fix": r.get("suggested_fix", ""),
            }
            for r in sorted(key_rows, key=lambda x: -float(x.get("priority_score") or 0))[:30]
        ]
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("No true_miss / near_threshold rows found.")

# ── Artifact downloads ────────────────────────────────────────────────────────

st.subheader("Download Artifacts")
artifact_defs = [
    ("prediction_vs_actual_report.md",        "Prediction Report (MD)"),
    ("prediction_vs_actual_rows.csv",         "Prediction Rows (CSV)"),
    ("prediction_vs_actual_enriched_rows.csv", "Enriched Rows (CSV)"),
    ("root_cause_enrichment_report.md",       "Root Cause Report (MD)"),
]
cols = st.columns(len(artifact_defs))
for col, (fname, label) in zip(cols, artifact_defs):
    fpath = Path(output_dir) / fname
    if fpath.exists():
        col.download_button(label, fpath.read_bytes(), file_name=fname)
    else:
        col.caption(f"{label} — not found")
