from __future__ import annotations
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import get_source_health, get_health_checks
from dashboard.components.tables import generic_table

require_auth()
st.title("Sources & Health")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path, read_only=True)

    st.subheader("Source Run History")
    sources = get_source_health(conn)
    generic_table(sources)

    st.subheader("Health Checks")
    checks = get_health_checks(conn)
    generic_table(checks)

    st.subheader("Source Run Detail")
    import pandas as pd
    runs = conn.execute(
        """
        SELECT source_name, status, records_inserted, records_failed,
               started_at, finished_at, error_message
        FROM source_runs ORDER BY started_at DESC LIMIT 50
        """
    ).fetchall()
    if runs:
        st.dataframe(
            pd.DataFrame(runs, columns=[
                "Source", "Status", "Inserted", "Failed", "Started", "Finished", "Error"
            ]),
            use_container_width=True, hide_index=True,
        )

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
