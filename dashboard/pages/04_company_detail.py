from __future__ import annotations
import os
import duckdb
import streamlit as st

from dashboard.auth import require_auth

require_auth()
st.title("Company Detail")

ticker = st.text_input("Ticker", value=st.session_state.get("selected_ticker", ""))
if not ticker:
    st.stop()

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
try:
    conn = duckdb.connect(db_path, read_only=True)

    company = conn.execute(
        "SELECT * FROM companies WHERE ticker = ?", [ticker.upper()]
    ).fetchone()
    if company:
        cols = [d[0] for d in conn.description]
        cdict = dict(zip(cols, company))
        st.subheader(f"{cdict.get('company_name', ticker)} ({ticker.upper()})")
        st.json(cdict)

    tab1, tab2, tab3, tab4 = st.tabs(["Filings", "Fundamentals", "Prices", "History"])

    with tab1:
        import pandas as pd
        filings = conn.execute(
            "SELECT form_type, filing_date, description, doc_url FROM filings WHERE ticker = ? ORDER BY filing_date DESC LIMIT 20",
            [ticker.upper()],
        ).fetchall()
        if filings:
            st.dataframe(
                pd.DataFrame(filings, columns=["Type", "Date", "Description", "URL"]),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No filings.")

    with tab2:
        funds = conn.execute(
            "SELECT concept, value, unit, as_of_date FROM fundamentals_raw WHERE ticker = ? ORDER BY as_of_date DESC LIMIT 50",
            [ticker.upper()],
        ).fetchall()
        if funds:
            st.dataframe(
                pd.DataFrame(funds, columns=["Concept", "Value", "Unit", "Date"]),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No fundamentals.")

    with tab3:
        prices = conn.execute(
            "SELECT trade_date, open, high, low, close, volume FROM prices_daily WHERE ticker = ? ORDER BY trade_date DESC LIMIT 90",
            [ticker.upper()],
        ).fetchall()
        if prices:
            st.dataframe(
                pd.DataFrame(prices, columns=["Date", "Open", "High", "Low", "Close", "Volume"]),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No price data.")

    with tab4:
        hist = conn.execute(
            "SELECT run_id, as_of_date, total_score, tier FROM scores WHERE ticker = ? ORDER BY as_of_date DESC LIMIT 30",
            [ticker.upper()],
        ).fetchall()
        if hist:
            st.dataframe(
                pd.DataFrame(hist, columns=["Run ID", "Date", "Score", "Tier"]),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No historical scores.")

    conn.close()
except Exception as exc:
    st.error(f"Error: {exc}")
