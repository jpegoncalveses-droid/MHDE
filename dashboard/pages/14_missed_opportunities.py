"""Dashboard page 14 — Missed Opportunities."""
import json

import streamlit as st

from dashboard.auth import require_auth
from storage.db import get_connection, init_schema

require_auth()
st.set_page_config(page_title="Missed Opportunities", layout="wide")
st.title("Missed Opportunities")


@st.cache_resource
def _conn():
    c = get_connection()
    init_schema(c)
    return c


conn = _conn()


def _load_events():
    try:
        rows = conn.execute(
            """SELECT ticker, event_date, event_type, return_value, tier_before_event,
                      was_in_universe, was_scored, had_catalyst_evidence, investigation_status
               FROM missed_opportunity_events
               ORDER BY return_value DESC LIMIT 200"""
        ).fetchall()
        cols = ["ticker", "event_date", "event_type", "return_value",
                "tier_before_event", "was_in_universe", "was_scored",
                "had_catalyst_evidence", "investigation_status"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []


def _load_root_causes():
    try:
        rows = conn.execute(
            """SELECT primary_root_cause, COUNT(*) as n
               FROM missed_opportunity_investigations
               GROUP BY primary_root_cause ORDER BY n DESC"""
        ).fetchall()
        return rows
    except Exception:
        return []


def _load_text_stats():
    try:
        from missed.labels import TEXT_RELATED_ROOT_CAUSES
        total = conn.execute(
            "SELECT COUNT(*) FROM missed_opportunity_investigations"
        ).fetchone()[0]
        text = conn.execute(
            "SELECT COUNT(*) FROM missed_opportunity_investigations WHERE primary_root_cause IN ({})".format(
                ",".join(f"'{c}'" for c in TEXT_RELATED_ROOT_CAUSES)
            )
        ).fetchone()[0]
        truly_unpred = conn.execute(
            "SELECT COUNT(*) FROM missed_opportunity_investigations WHERE primary_root_cause='truly_unpredictable'"
        ).fetchone()[0]
        return total, text, truly_unpred
    except Exception:
        return 0, 0, 0


events = _load_events()
root_causes = _load_root_causes()
total_inv, text_inv, unpred = _load_text_stats()

# Summary metrics
col1, col2, col3, col4 = st.columns(4)
col1.metric("Events detected", len(events))
col2.metric("Investigated", total_inv)
col3.metric("Text-related", text_inv)
col4.metric("Truly unpredictable", unpred)

st.divider()

# Root cause breakdown
if root_causes:
    st.subheader("Root cause breakdown")
    import pandas as pd
    df_rc = pd.DataFrame(root_causes, columns=["root_cause", "count"])
    st.bar_chart(df_rc.set_index("root_cause"))

st.divider()

# Biggest missed moves
if events:
    st.subheader("Biggest missed moves")
    import pandas as pd
    st.dataframe(
        pd.DataFrame(events[:50]),
        use_container_width=True,
    )
else:
    st.info("No missed opportunity events detected yet. Run: python main.py missed detect")
