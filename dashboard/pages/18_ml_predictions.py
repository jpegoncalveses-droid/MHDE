"""ML Predictions page — shows model predictions with regime and correlation analysis."""
from __future__ import annotations

import os

import duckdb
import pandas as pd
import streamlit as st

from dashboard.auth import require_auth

require_auth()

st.title("ML Predictions")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")

try:
    conn = duckdb.connect(db_path)

    # --- Model Health Banner ---
    model_info = conn.execute("""
        SELECT model_id, horizon, created_at, train_start, train_end,
               auc_roc, lift_over_base, precision_at_threshold, base_rate
        FROM ml_model_runs WHERE is_active = true
        ORDER BY horizon
    """).fetchdf()

    if model_info.empty:
        st.error("No active ML models found. Run `python main.py ml train` first.")
        st.stop()

    with st.expander("Model Health", expanded=False):
        cols = st.columns(len(model_info))
        for i, (_, row) in enumerate(model_info.iterrows()):
            with cols[i]:
                st.metric(f"{row['horizon']} model", f"AUC {row['auc_roc']:.3f}")
                st.caption(f"Lift: {row['lift_over_base']:.2f}x | "
                           f"Train: {row['train_start']} → {row['train_end']}")

    # --- Date selector ---
    available_dates = conn.execute("""
        SELECT DISTINCT prediction_date FROM ml_predictions
        ORDER BY prediction_date DESC LIMIT 30
    """).fetchdf()

    if available_dates.empty:
        st.warning("No predictions yet. Run `python main.py ml predict` to generate predictions.")
        st.stop()

    selected_date = st.selectbox(
        "Prediction date",
        available_dates["prediction_date"].tolist(),
        format_func=lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
    )

    # --- Load predictions for selected date ---
    preds_df = conn.execute("""
        SELECT ticker, horizon, predicted_probability, prediction_threshold,
               sector, market_cap_bucket, actual_max_return, actual_max_drawdown, actual_hit
        FROM ml_predictions
        WHERE prediction_date = ?
        ORDER BY horizon, predicted_probability DESC
    """, [selected_date]).fetchdf()

    # --- Regime Indicator ---
    total_universe = conn.execute("""
        SELECT COUNT(DISTINCT ticker) FROM ml_features
        WHERE trade_date = ?
    """, [selected_date]).fetchone()[0]

    n_above_60 = len(preds_df[preds_df["predicted_probability"] >= 0.60])
    pct_above_60 = n_above_60 / total_universe * 100 if total_universe > 0 else 0

    if pct_above_60 > 30:
        regime_label = "HIGH ACTIVITY"
        regime_color = "red"
        regime_desc = "Broad opportunity — many stocks showing signal. Higher correlation risk."
    elif pct_above_60 > 10:
        regime_label = "NORMAL"
        regime_color = "blue"
        regime_desc = "Normal conditions — selective opportunities."
    else:
        regime_label = "LOW ACTIVITY"
        regime_color = "gray"
        regime_desc = "Few signals — market may be range-bound or signals concentrated."

    st.markdown(f"### Regime: :{regime_color}[{regime_label}]")
    st.caption(f"{regime_desc} — {n_above_60} of {total_universe} tickers above 0.60 ({pct_above_60:.1f}%)")

    # --- Correlation Warning ---
    sector_counts = preds_df.groupby("sector").size().reset_index(name="count")
    sector_counts["pct"] = sector_counts["count"] / len(preds_df) * 100
    concentrated = sector_counts[sector_counts["pct"] > 30]

    if not concentrated.empty:
        for _, row in concentrated.iterrows():
            st.warning(
                f"**Correlation risk:** {row['sector']} has {row['count']} predictions "
                f"({row['pct']:.0f}% of total). Correlated positions increase drawdown risk."
            )

    # --- Horizon filter ---
    horizons = sorted(preds_df["horizon"].unique())
    selected_horizons = st.multiselect("Horizons", horizons, default=horizons)

    filtered = preds_df[preds_df["horizon"].isin(selected_horizons)].copy()

    # --- Sidebar filters ---
    with st.sidebar:
        st.subheader("Filters")
        min_prob = st.slider("Min probability", 0.40, 0.95, 0.50, 0.05)
        filtered = filtered[filtered["predicted_probability"] >= min_prob]

        sectors = ["All"] + sorted(filtered["sector"].dropna().unique().tolist())
        sel_sector = st.selectbox("Sector", sectors)
        if sel_sector != "All":
            filtered = filtered[filtered["sector"] == sel_sector]

        caps = ["All"] + sorted(filtered["market_cap_bucket"].dropna().unique().tolist())
        sel_cap = st.selectbox("Market Cap", caps)
        if sel_cap != "All":
            filtered = filtered[filtered["market_cap_bucket"] == sel_cap]

    # --- Main predictions table ---
    st.subheader(f"Predictions ({len(filtered)} shown)")

    if filtered.empty:
        st.info("No predictions match current filters.")
    else:
        display_df = filtered.copy()
        display_df["prob"] = display_df["predicted_probability"].apply(lambda x: f"{x:.0%}")
        display_df["confidence"] = display_df["predicted_probability"].apply(
            lambda x: "High" if x >= 0.60 else "Lower"
        )

        if display_df["actual_hit"].notna().any():
            display_df["outcome"] = display_df.apply(
                lambda r: f"{'HIT' if r['actual_hit'] else 'miss'} ({r['actual_max_return']*100:+.1f}%)"
                if pd.notna(r["actual_hit"]) else "pending",
                axis=1,
            )
        else:
            display_df["outcome"] = "pending"

        show_cols = ["ticker", "horizon", "prob", "confidence", "sector",
                     "market_cap_bucket", "outcome"]
        st.dataframe(
            display_df[show_cols].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
            column_config={
                "ticker": st.column_config.TextColumn("Ticker", width="small"),
                "horizon": st.column_config.TextColumn("Horizon", width="small"),
                "prob": st.column_config.TextColumn("Probability", width="small"),
                "confidence": st.column_config.TextColumn("Conf", width="small"),
                "sector": st.column_config.TextColumn("Sector"),
                "market_cap_bucket": st.column_config.TextColumn("Cap", width="small"),
                "outcome": st.column_config.TextColumn("Outcome", width="medium"),
            },
        )

    # --- Sector Breakdown ---
    st.subheader("Sector Concentration")
    sector_summary = preds_df.groupby("sector").agg(
        count=("ticker", "size"),
        avg_prob=("predicted_probability", "mean"),
    ).reset_index()
    sector_summary["pct"] = sector_summary["count"] / len(preds_df) * 100
    sector_summary = sector_summary.sort_values("count", ascending=False)
    sector_summary["risk"] = sector_summary["pct"].apply(lambda x: "CORRELATED" if x > 30 else "")

    st.dataframe(
        sector_summary.rename(columns={
            "sector": "Sector", "count": "Predictions", "avg_prob": "Avg Prob",
            "pct": "% of Total", "risk": "Risk"
        }),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Avg Prob": st.column_config.NumberColumn(format="%.0%%"),
            "% of Total": st.column_config.NumberColumn(format="%.0f%%"),
        },
    )

    # --- Historical Accuracy ---
    accuracy_df = conn.execute("""
        SELECT
            horizon,
            COUNT(*) AS n,
            SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS hits,
            AVG(actual_max_return) * 100 AS avg_max_return_pct,
            AVG(actual_max_drawdown) * 100 AS avg_max_drawdown_pct,
            AVG(predicted_probability) AS avg_prob
        FROM ml_predictions
        WHERE outcome_filled_at IS NOT NULL
        GROUP BY horizon
    """).fetchdf()

    if not accuracy_df.empty:
        st.subheader("Historical Accuracy (all dates with outcomes)")
        accuracy_df["precision"] = accuracy_df["hits"] / accuracy_df["n"]
        accuracy_df["precision_pct"] = accuracy_df["precision"].apply(lambda x: f"{x:.0%}")

        col1, col2, col3 = st.columns(3)
        for i, (_, row) in enumerate(accuracy_df.iterrows()):
            with [col1, col2, col3][i % 3]:
                st.metric(
                    f"{row['horizon']} precision",
                    row["precision_pct"],
                    f"n={int(row['n'])}, avg ret={row['avg_max_return_pct']:+.1f}%",
                )

        # Detailed outcome table
        with st.expander("Recent outcomes detail", expanded=False):
            recent_outcomes = conn.execute("""
                SELECT ticker, prediction_date, horizon, predicted_probability,
                       actual_max_return, actual_max_drawdown, actual_hit
                FROM ml_predictions
                WHERE outcome_filled_at IS NOT NULL
                ORDER BY prediction_date DESC, predicted_probability DESC
                LIMIT 50
            """).fetchdf()
            if not recent_outcomes.empty:
                recent_outcomes["result"] = recent_outcomes.apply(
                    lambda r: f"{'HIT' if r['actual_hit'] else 'miss'} "
                              f"(max: {r['actual_max_return']*100:+.1f}%, "
                              f"dd: {r['actual_max_drawdown']*100:+.1f}%)",
                    axis=1,
                )
                st.dataframe(
                    recent_outcomes[["ticker", "prediction_date", "horizon",
                                     "predicted_probability", "result"]],
                    use_container_width=True,
                    hide_index=True,
                )

    conn.close()

except Exception as e:
    st.error(f"Database error: {e}")
