from __future__ import annotations

import pandas as pd
import streamlit as st


def scores_table(candidates: list[dict]) -> None:
    if not candidates:
        st.info("No candidates found.")
        return
    df = pd.DataFrame(candidates)
    display_cols = [
        "ticker", "company_name", "tier", "total_score",
        "cheap_score", "quality_score", "catalyst_score",
        "momentum_score", "sentiment_score", "risk_penalty", "confidence",
    ]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(
        df[available].round(1),
        use_container_width=True,
        hide_index=True,
    )


def features_table(features: list[dict]) -> None:
    if not features:
        st.info("No features found.")
        return
    df = pd.DataFrame(features)
    st.dataframe(df.round(2), use_container_width=True, hide_index=True)


def generic_table(rows: list[dict], round_cols: list[str] | None = None) -> None:
    if not rows:
        st.info("No data found.")
        return
    df = pd.DataFrame(rows)
    if round_cols:
        for col in round_cols:
            if col in df.columns:
                df[col] = df[col].round(4)
    st.dataframe(df, use_container_width=True, hide_index=True)
