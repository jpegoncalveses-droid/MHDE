"""Crypto ML Predictions — shows crypto predictions with BTC regime and correlation analysis."""
from __future__ import annotations

import os

import duckdb
import pandas as pd
import streamlit as st

from dashboard.auth import require_auth

require_auth()

st.title("Crypto Predictions")

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")

try:
    conn = duckdb.connect(db_path)

    # --- Model Health Banner ---
    model_info = conn.execute("""
        SELECT model_id, horizon, created_at, train_start, train_end,
               auc_roc, lift_over_base, precision_at_threshold, base_rate
        FROM crypto_ml_model_runs WHERE is_active = true
        ORDER BY horizon
    """).fetchdf()

    if model_info.empty:
        st.error("No active crypto ML models. Run `python main.py crypto train` first.")
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
        SELECT DISTINCT prediction_date FROM crypto_ml_predictions
        ORDER BY prediction_date DESC LIMIT 30
    """).fetchdf()

    if available_dates.empty:
        st.warning("No predictions yet. Run `python main.py crypto predict` to generate.")
        st.stop()

    selected_date = st.selectbox(
        "Prediction date",
        available_dates["prediction_date"].tolist(),
        format_func=lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
    )

    # --- Load predictions ---
    preds_df = conn.execute("""
        SELECT symbol, horizon, predicted_probability, prediction_threshold,
               market_cap_bucket, actual_max_return, actual_max_drawdown, actual_hit
        FROM crypto_ml_predictions
        WHERE prediction_date = ?
        ORDER BY horizon, predicted_probability DESC
    """, [selected_date]).fetchdf()

    # --- BTC Regime Indicator ---
    btc_return_7d = conn.execute("""
        WITH btc_prices AS (
            SELECT trade_date, close,
                   LAG(close, 7) OVER (ORDER BY trade_date) AS close_7ago
            FROM crypto_prices_daily
            WHERE symbol = 'BTCUSDT'
            ORDER BY trade_date DESC
            LIMIT 10
        )
        SELECT close / NULLIF(close_7ago, 0) - 1 AS ret_7d
        FROM btc_prices
        WHERE close_7ago IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    btc_ret = float(btc_return_7d[0]) if btc_return_7d and btc_return_7d[0] else 0

    if btc_ret > 0.03:
        regime_label = "BULLISH"
        regime_color = "green"
        regime_desc = "BTC trending up — altcoin predictions likely correlated to BTC"
    elif btc_ret < -0.03:
        regime_label = "BEARISH"
        regime_color = "red"
        regime_desc = "BTC trending down — high correlation risk, predictions less reliable"
    else:
        regime_label = "NEUTRAL"
        regime_color = "blue"
        regime_desc = "BTC flat — coin-specific signals dominate"

    st.markdown(f"### BTC Regime: :{regime_color}[{regime_label}]")
    st.caption(f"{regime_desc} (BTC 7d return: {btc_ret:+.1%})")

    # --- Correlation Warning ---
    btc_betas = conn.execute("""
        SELECT symbol, beta_to_btc_30d
        FROM crypto_ml_features
        WHERE trade_date = ? AND beta_to_btc_30d IS NOT NULL
    """, [selected_date]).fetchdf()

    if not btc_betas.empty and not preds_df.empty:
        pred_with_beta = preds_df.merge(btc_betas, on="symbol", how="left")
        high_beta = pred_with_beta[pred_with_beta["beta_to_btc_30d"] > 0.8]
        if len(high_beta) > len(preds_df) * 0.5:
            st.warning(
                f"**Correlation risk:** {len(high_beta)} of {len(preds_df)} predictions have "
                f"BTC beta > 0.8. Effectively one directional BTC bet."
            )

    # --- Sidebar filters ---
    with st.sidebar:
        st.header("Filters")
        min_prob = st.slider("Min probability", 0.40, 0.90, 0.50, 0.05)
        horizon_filter = st.multiselect(
            "Horizons",
            preds_df["horizon"].unique().tolist(),
            default=preds_df["horizon"].unique().tolist(),
        )
        cap_options = preds_df["market_cap_bucket"].dropna().unique().tolist()
        cap_filter = st.multiselect(
            "Market Cap",
            cap_options,
            default=cap_options,
        )

    filtered = preds_df[
        (preds_df["predicted_probability"] >= min_prob)
        & (preds_df["horizon"].isin(horizon_filter))
        & (preds_df["market_cap_bucket"].isin(cap_filter))
    ].copy()

    # --- Predictions Table ---
    st.subheader(f"Predictions ({len(filtered)} coins)")

    if filtered.empty:
        st.info("No predictions match filters.")
    else:
        display = filtered.copy()
        display["Prob"] = display["predicted_probability"].map(lambda p: f"{p:.1%}")
        display["Confidence"] = display["predicted_probability"].map(
            lambda p: "High" if p >= 0.60 else "Lower")

        if "actual_hit" in display.columns:
            display["Outcome"] = display.apply(
                lambda r: f"{'HIT' if r['actual_hit'] else 'miss'} ({r['actual_max_return']:+.1%})"
                if pd.notna(r.get("actual_hit")) else "pending",
                axis=1,
            )

        show_cols = ["symbol", "horizon", "Prob", "Confidence", "market_cap_bucket"]
        if "Outcome" in display.columns:
            show_cols.append("Outcome")
        st.dataframe(display[show_cols], use_container_width=True, hide_index=True)

    # --- Historical Accuracy ---
    accuracy = conn.execute("""
        SELECT horizon, COUNT(*) AS n,
               SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS hits,
               AVG(actual_max_return) AS avg_ret,
               AVG(actual_max_drawdown) AS avg_dd
        FROM crypto_ml_predictions
        WHERE outcome_filled_at IS NOT NULL
        GROUP BY horizon
    """).fetchdf()

    if not accuracy.empty:
        st.subheader("Historical Accuracy")
        accuracy["Precision"] = (accuracy["hits"] / accuracy["n"] * 100).round(1)
        accuracy["Avg MaxRet"] = (accuracy["avg_ret"] * 100).round(2)
        accuracy["Avg MaxDD"] = (accuracy["avg_dd"] * 100).round(2)
        st.dataframe(
            accuracy[["horizon", "n", "hits", "Precision", "Avg MaxRet", "Avg MaxDD"]],
            use_container_width=True,
            hide_index=True,
        )

    conn.close()

except Exception as e:
    st.error(f"Database error: {e}")
