from __future__ import annotations
import streamlit as st
from pathlib import Path

from dashboard.auth import require_auth
from governance.source_registry import get_source_registry
from governance.scorecard_registry import get_scorecard_config
from governance.prompt_registry import get_prompt_registry

require_auth()
st.title("Governance")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Source Registry", "Scorecard", "Prompts", "Known Limitations", "Decision Log"
])

with tab1:
    sources = get_source_registry()
    import pandas as pd
    if sources:
        st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True)
    else:
        st.info("No sources configured. Check config/sources.yaml.")

with tab2:
    cfg = get_scorecard_config()
    st.json(cfg)

with tab3:
    prompts = get_prompt_registry()
    if prompts:
        st.dataframe(pd.DataFrame(prompts), use_container_width=True, hide_index=True)

with tab4:
    doc = Path("docs/known_limitations.md")
    if doc.exists():
        st.markdown(doc.read_text())
    else:
        st.info("docs/known_limitations.md not found.")

with tab5:
    doc = Path("docs/decision_log.md")
    if doc.exists():
        st.markdown(doc.read_text())
    else:
        st.info("docs/decision_log.md not found.")

st.divider()
st.info(
    "MHDE does not include paper trading by design. "
    "Candidate outcome tracking is the evaluation mechanism. "
    "The XGBoost model is quarantined: experimental only, not used for alerts or rankings."
)
