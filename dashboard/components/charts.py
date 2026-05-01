from __future__ import annotations

import streamlit as st


def score_distribution(candidates: list[dict]) -> None:
    try:
        import pandas as pd
        import altair as alt
        if not candidates:
            return
        df = pd.DataFrame(candidates)[["ticker", "total_score", "tier"]]
        chart = (
            alt.Chart(df)
            .mark_bar()
            .encode(
                x=alt.X("total_score:Q", bin=alt.Bin(step=5), title="Total Score"),
                y=alt.Y("count()", title="Count"),
                color=alt.Color("tier:N", scale=alt.Scale(
                    domain=["A", "B", "C", "Reject"],
                    range=["#2ecc71", "#3498db", "#f1c40f", "#e74c3c"],
                )),
                tooltip=["tier", "count()"],
            )
            .properties(title="Score Distribution", height=250)
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        st.caption("Install altair for charts: pip install altair")


def price_chart(prices: list[dict], ticker: str) -> None:
    try:
        import pandas as pd
        import altair as alt
        if not prices:
            st.caption("No price data available.")
            return
        df = pd.DataFrame(prices)
        df["date"] = pd.to_datetime(df["date"])
        chart = (
            alt.Chart(df)
            .mark_line()
            .encode(
                x=alt.X("date:T", title="Date"),
                y=alt.Y("close:Q", title="Close"),
                tooltip=["date:T", "close:Q", "volume:Q"],
            )
            .properties(title=f"{ticker} — Price (90 days)", height=200)
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        st.caption("Install altair for charts.")


def tier_pie(tier_counts: dict) -> None:
    try:
        import pandas as pd
        import altair as alt
        if not tier_counts:
            return
        df = pd.DataFrame(
            [{"tier": k, "count": v} for k, v in tier_counts.items()]
        )
        chart = (
            alt.Chart(df)
            .mark_arc()
            .encode(
                theta=alt.Theta("count:Q"),
                color=alt.Color("tier:N"),
                tooltip=["tier", "count"],
            )
            .properties(title="Tier Distribution", height=200)
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        pass
