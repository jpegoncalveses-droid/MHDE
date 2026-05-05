from __future__ import annotations
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.queries import get_alerts
from dashboard.components.tables import generic_table

require_auth()
st.title("Alerts")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path)

    col1, col2, col3 = st.columns(3)
    sent = conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'sent'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'failed'").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    col1.metric("Total Alerts", total)
    col2.metric("Sent", sent)
    col3.metric("Failed", failed)

    alerts = get_alerts(conn, limit=200)
    generic_table(alerts)
    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
