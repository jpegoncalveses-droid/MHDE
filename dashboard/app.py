"""MHDE Dashboard — ML Predictions with health checks."""
from __future__ import annotations

import os

import duckdb
import pandas as pd
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.maturity import (
    format_pct_move,
    format_time_remaining,
    pct_move_equity_or_crypto,
    pct_move_fx,
    time_remaining_days,
    time_remaining_hours,
)
from dashboard.services.queries import (
    get_crypto_predictions,
    get_crypto_recent_outcomes,
    get_distinct_prediction_dates,
    get_equity_predictions,
    get_equity_recent_outcomes,
    get_fx_recent_predictions,
)

st.set_page_config(
    page_title="MHDE — ML Predictions",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_auth()

db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")


def _open_conn():
    """Fresh read-only DuckDB connection. Opened per section so each render
    sees the latest committed state from the writer processes and never holds
    a write lock that would block them."""
    try:
        return duckdb.connect(db_path, read_only=True)
    except Exception as e:
        st.error(f"Database error: {e}")
        st.stop()


# --- System Health: Data Freshness (all engines) ---
from pipelines.freshness import check_all as _check_all_freshness

conn = _open_conn()
_freshness_reports = _check_all_freshness(conn)
with st.expander(
    "System Health — Data Freshness",
    expanded=any(not r.is_fresh for r in _freshness_reports.values()),
):
    cols = st.columns(len(_freshness_reports))
    for col, (engine, report) in zip(cols, _freshness_reports.items()):
        with col:
            icon = "🟢" if report.is_fresh else "🔴"
            label = {"equity": "Equity", "crypto": "Crypto", "fx": "FX"}[engine]
            latest_str = str(report.latest) if report.latest is not None else "—"
            st.metric(f"{icon} {label}", latest_str, delta=f"age: {report.age_str}")
            st.caption(f"Threshold: {report.threshold}")
            if not report.is_fresh:
                st.warning(report.message)
conn.close()

tab_equities, tab_crypto, tab_fx = st.tabs(["Equities", "Crypto", "FX"])

# ===============================================================================
# EQUITIES TAB
# ===============================================================================
conn = _open_conn()
with tab_equities:
    st.title("ML Predictions")

    # --- Health Checks Banner ---
    with st.expander("System Health", expanded=False):
        from health.ml_checks import (
            check_trained_models,
            check_last_prediction,
            check_rolling_precision,
            check_ml_tables_freshness,
        )
        checks = [
            check_trained_models(),
            check_last_prediction(conn),
            check_rolling_precision(conn),
            *check_ml_tables_freshness(conn),
        ]
        for c in checks:
            icon = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}.get(c["status"], "❓")
            st.markdown(f"{icon} **{c['check_name']}** — {c['message']}")

    # --- Model Health Banner ---
    model_info = conn.execute("""
        SELECT model_id, horizon, created_at, train_start, train_end,
               auc_roc, lift_over_base, precision_at_threshold, base_rate
        FROM ml_model_runs WHERE is_active = true
        ORDER BY horizon
    """).fetchdf()

    if model_info.empty:
        st.error("No active ML models found. Run `python main.py ml train` first.")
    else:
        with st.expander("Model Health", expanded=False):
            cols = st.columns(len(model_info))
            for i, (_, row) in enumerate(model_info.iterrows()):
                with cols[i]:
                    st.metric(f"{row['horizon']} model", f"AUC {row['auc_roc']:.3f}")
                    st.caption(f"Lift: {row['lift_over_base']:.2f}x | "
                               f"Train: {row['train_start']} → {row['train_end']}")

        # --- Date selector ---
        available_dates = get_distinct_prediction_dates(
            conn, "ml_predictions", "prediction_date", limit=30
        )

        if not available_dates:
            st.warning("No predictions yet. Run `python main.py ml predict` to generate predictions.")
        else:
            selected_date = st.selectbox(
                "Prediction date",
                available_dates,
                format_func=lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
            )

            # --- Load predictions for selected date ---
            preds_df = get_equity_predictions(conn, selected_date)

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

                display_df["pct_move_str"] = display_df.apply(
                    lambda r: format_pct_move(pct_move_equity_or_crypto(
                        r.get("actual_max_return"),
                        r.get("price_at_prediction"),
                        r.get("current_price"),
                        pd.notna(r.get("outcome_filled_at")),
                    )),
                    axis=1,
                )
                display_df["time_remaining_str"] = display_df.apply(
                    lambda r: format_time_remaining(time_remaining_days(
                        r.get("maturity_date"),
                        outcome_filled=pd.notna(r.get("outcome_filled_at")),
                    )),
                    axis=1,
                )

                show_cols = ["ticker", "horizon", "prob", "confidence", "sector",
                             "market_cap_bucket", "price_at_prediction",
                             "maturity_date", "price_at_maturity",
                             "pct_move_str", "time_remaining_str", "outcome"]
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
                        "price_at_prediction": st.column_config.NumberColumn(
                            "Price @ Pred", format="$%.2f", width="small"
                        ),
                        "maturity_date": st.column_config.DateColumn(
                            "Maturity", width="small"
                        ),
                        "price_at_maturity": st.column_config.NumberColumn(
                            "Price @ Maturity", format="$%.2f", width="small"
                        ),
                        "pct_move_str": st.column_config.TextColumn(
                            "% Move", width="small"
                        ),
                        "time_remaining_str": st.column_config.TextColumn(
                            "Time Left", width="small"
                        ),
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

                with st.expander("Recent outcomes detail", expanded=False):
                    recent_outcomes = get_equity_recent_outcomes(conn, limit=50)
                    if not recent_outcomes.empty:
                        recent_outcomes["result"] = recent_outcomes.apply(
                            lambda r: f"{'HIT' if r['actual_hit'] else 'miss'} "
                                      f"(max: {r['actual_max_return']*100:+.1f}%, "
                                      f"dd: {r['actual_max_drawdown']*100:+.1f}%)",
                            axis=1,
                        )
                        recent_outcomes["pct_move_str"] = recent_outcomes.apply(
                            lambda r: format_pct_move(pct_move_equity_or_crypto(
                                r.get("actual_max_return"),
                                r.get("price_at_prediction"),
                                None,
                                outcome_filled=True,
                            )),
                            axis=1,
                        )
                        st.dataframe(
                            recent_outcomes[["ticker", "prediction_date", "horizon",
                                             "predicted_probability",
                                             "price_at_prediction",
                                             "maturity_date", "price_at_maturity",
                                             "pct_move_str", "result"]],
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "price_at_prediction": st.column_config.NumberColumn(
                                    "Price @ Pred", format="$%.2f", width="small"
                                ),
                                "maturity_date": st.column_config.DateColumn(
                                    "Maturity", width="small"
                                ),
                                "price_at_maturity": st.column_config.NumberColumn(
                                    "Price @ Maturity", format="$%.2f", width="small"
                                ),
                                "pct_move_str": st.column_config.TextColumn(
                                    "% Move", width="small"
                                ),
                            },
                        )

conn.close()

# ===============================================================================
# CRYPTO TAB
# ===============================================================================
conn = _open_conn()
with tab_crypto:
    st.title("Crypto Predictions")

    # --- Model Health Banner ---
    crypto_model_info = conn.execute("""
        SELECT model_id, horizon, created_at, train_start, train_end,
               auc_roc, lift_over_base, precision_at_threshold, base_rate
        FROM crypto_ml_model_runs WHERE is_active = true
        ORDER BY horizon
    """).fetchdf()

    if crypto_model_info.empty:
        st.error("No active crypto ML models. Run `python main.py crypto train` first.")
    else:
        with st.expander("Model Health", expanded=False):
            cols = st.columns(len(crypto_model_info))
            for i, (_, row) in enumerate(crypto_model_info.iterrows()):
                with cols[i]:
                    st.metric(f"{row['horizon']} model", f"AUC {row['auc_roc']:.3f}")
                    st.caption(f"Lift: {row['lift_over_base']:.2f}x | "
                               f"Train: {row['train_start']} → {row['train_end']}")

        # --- Date selector ---
        crypto_dates = get_distinct_prediction_dates(
            conn, "crypto_ml_predictions", "prediction_date", limit=30
        )

        if not crypto_dates:
            st.warning("No predictions yet. Run `python main.py crypto predict` to generate.")
        else:
            crypto_selected_date = st.selectbox(
                "Prediction date",
                crypto_dates,
                format_func=lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
                key="crypto_date",
            )

            # --- Load predictions ---
            crypto_preds = get_crypto_predictions(conn, crypto_selected_date)

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
            """, [crypto_selected_date]).fetchdf()

            if not btc_betas.empty and not crypto_preds.empty:
                pred_with_beta = crypto_preds.merge(btc_betas, on="symbol", how="left")
                high_beta = pred_with_beta[pred_with_beta["beta_to_btc_30d"] > 0.8]
                if len(high_beta) > len(crypto_preds) * 0.5:
                    st.warning(
                        f"**Correlation risk:** {len(high_beta)} of {len(crypto_preds)} predictions have "
                        f"BTC beta > 0.8. Effectively one directional BTC bet."
                    )

            # --- Sidebar filters ---
            with st.sidebar:
                st.header("Crypto Filters")
                crypto_min_prob = st.slider("Min probability", 0.40, 0.90, 0.50, 0.05, key="crypto_prob")
                crypto_horizons = st.multiselect(
                    "Horizons",
                    crypto_preds["horizon"].unique().tolist(),
                    default=crypto_preds["horizon"].unique().tolist(),
                    key="crypto_hz",
                )

            crypto_filtered = crypto_preds[
                (crypto_preds["predicted_probability"] >= crypto_min_prob)
                & (crypto_preds["horizon"].isin(crypto_horizons))
            ].copy()

            # --- Predictions Table ---
            st.subheader(f"Predictions ({len(crypto_filtered)} coins)")

            if crypto_filtered.empty:
                st.info("No predictions match filters.")
            else:
                display = crypto_filtered.copy()
                display["Prob"] = display["predicted_probability"].map(lambda p: f"{p:.1%}")
                display["Confidence"] = display["predicted_probability"].map(
                    lambda p: "High" if p >= 0.60 else "Lower")

                if "actual_hit" in display.columns:
                    display["Outcome"] = display.apply(
                        lambda r: f"{'HIT' if r['actual_hit'] else 'miss'} ({r['actual_max_return']:+.1%})"
                        if pd.notna(r.get("actual_hit")) else "pending",
                        axis=1,
                    )

                display["pct_move_str"] = display.apply(
                    lambda r: format_pct_move(pct_move_equity_or_crypto(
                        r.get("actual_max_return"),
                        r.get("price_at_prediction"),
                        r.get("current_price"),
                        pd.notna(r.get("outcome_filled_at")),
                    )),
                    axis=1,
                )
                display["time_remaining_str"] = display.apply(
                    lambda r: format_time_remaining(time_remaining_days(
                        r.get("maturity_date"),
                        outcome_filled=pd.notna(r.get("outcome_filled_at")),
                    )),
                    axis=1,
                )

                show_cols = ["symbol", "horizon", "Prob", "Confidence",
                             "market_cap_bucket", "price_at_prediction",
                             "maturity_date", "price_at_maturity",
                             "pct_move_str", "time_remaining_str"]
                if "Outcome" in display.columns:
                    show_cols.append("Outcome")
                st.dataframe(
                    display[show_cols],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "price_at_prediction": st.column_config.NumberColumn(
                            "Price @ Pred", format="$%.4f", width="small"
                        ),
                        "maturity_date": st.column_config.DateColumn(
                            "Maturity", width="small"
                        ),
                        "price_at_maturity": st.column_config.NumberColumn(
                            "Price @ Maturity", format="$%.4f", width="small"
                        ),
                        "pct_move_str": st.column_config.TextColumn(
                            "% Move", width="small"
                        ),
                        "time_remaining_str": st.column_config.TextColumn(
                            "Time Left", width="small"
                        ),
                    },
                )

            # --- Historical Accuracy ---
            crypto_accuracy = conn.execute("""
                SELECT horizon, COUNT(*) AS n,
                       SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS hits,
                       AVG(actual_max_return) AS avg_ret,
                       AVG(actual_max_drawdown) AS avg_dd
                FROM crypto_ml_predictions
                WHERE outcome_filled_at IS NOT NULL
                GROUP BY horizon
            """).fetchdf()

            st.subheader("Historical Accuracy")
            if crypto_accuracy.empty:
                st.info("No outcomes filled yet — accuracy will appear once prediction horizons elapse.")
            else:
                crypto_accuracy["Precision"] = (crypto_accuracy["hits"] / crypto_accuracy["n"] * 100).round(1)
                crypto_accuracy["Avg MaxRet"] = (crypto_accuracy["avg_ret"] * 100).round(2)
                crypto_accuracy["Avg MaxDD"] = (crypto_accuracy["avg_dd"] * 100).round(2)
                st.dataframe(
                    crypto_accuracy[["horizon", "n", "hits", "Precision", "Avg MaxRet", "Avg MaxDD"]],
                    use_container_width=True,
                    hide_index=True,
                )

            with st.expander("Recent outcomes detail", expanded=False):
                crypto_recent = get_crypto_recent_outcomes(conn, limit=50)
                if crypto_recent.empty:
                    st.info("No outcomes yet — predictions need time for their horizons to elapse.")
                else:
                    crypto_recent["result"] = crypto_recent.apply(
                        lambda r: f"{'HIT' if r['actual_hit'] else 'miss'} "
                                  f"(max: {r['actual_max_return']*100:+.1f}%, "
                                  f"dd: {r['actual_max_drawdown']*100:+.1f}%)",
                        axis=1,
                    )
                    crypto_recent["pct_move_str"] = crypto_recent.apply(
                        lambda r: format_pct_move(pct_move_equity_or_crypto(
                            r.get("actual_max_return"),
                            r.get("price_at_prediction"),
                            None,
                            outcome_filled=True,
                        )),
                        axis=1,
                    )
                    st.dataframe(
                        crypto_recent[["symbol", "prediction_date", "horizon",
                                       "predicted_probability",
                                       "price_at_prediction",
                                       "maturity_date", "price_at_maturity",
                                       "pct_move_str", "result"]],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "price_at_prediction": st.column_config.NumberColumn(
                                "Price @ Pred", format="$%.4f", width="small"
                            ),
                            "maturity_date": st.column_config.DateColumn(
                                "Maturity", width="small"
                            ),
                            "price_at_maturity": st.column_config.NumberColumn(
                                "Price @ Maturity", format="$%.4f", width="small"
                            ),
                            "pct_move_str": st.column_config.TextColumn(
                                "% Move", width="small"
                            ),
                        },
                    )

conn.close()

# ===============================================================================
#  TAB 3 — FX (GBP/EUR)
# ===============================================================================
conn = _open_conn()
with tab_fx:
    header_col, refresh_col = st.columns([5, 1])
    header_col.header("GBP/EUR FX Predictions")
    if refresh_col.button("↻ Refresh", key="fx_refresh", use_container_width=True):
        st.rerun()

    try:
        fx_data_as_of = conn.execute(
            "SELECT MAX(datetime_utc) FROM fx_prices_hourly"
        ).fetchone()[0]
    except Exception:
        fx_data_as_of = None
    if fx_data_as_of is not None:
        st.caption(f"Data as of bar: {fx_data_as_of} UTC")

    # --- Current Position ---
    try:
        fx_position_row = conn.execute("""
            SELECT position, entry_rate, entry_date, updated_at
            FROM fx_position ORDER BY updated_at DESC LIMIT 1
        """).fetchone()
    except Exception:
        fx_position_row = None

    if fx_position_row is None:
        st.info(
            "No FX position recorded. Set with: "
            "`python main.py fx set-position --holding EUR --rate 1.1602 --date 2026-04-30`"
        )
        fx_position = None
    else:
        fx_position, fx_entry_rate, fx_entry_date, fx_pos_updated = fx_position_row
        currency = "EUR" if fx_position == "HOLDING_EUR" else "GBP"
        pos_col1, pos_col2, pos_col3 = st.columns(3)
        pos_col1.metric("Current Position", f"Holding {currency}")
        pos_col2.metric("Entry Rate", f"{fx_entry_rate:.5f}")
        pos_col3.metric("Entry Date", str(fx_entry_date)[:10])

    # --- Model Health ---
    try:
        fx_models = conn.execute("""
            SELECT model_id, direction, horizon, created_at, train_start, train_end,
                   auc_roc, lift_over_base, precision_at_threshold, base_rate
            FROM fx_ml_model_runs WHERE is_active = true
            ORDER BY direction, horizon
        """).fetchdf()
    except Exception:
        fx_models = pd.DataFrame()

    if fx_models.empty:
        st.warning("No active FX models. Run `python main.py fx train` first.")
    else:
        with st.expander("Model Health", expanded=False):
            cols = st.columns(len(fx_models))
            for i, (_, row) in enumerate(fx_models.iterrows()):
                with cols[i]:
                    label = f"{row['direction']} {row['horizon']}"
                    st.metric(label, f"AUC {row['auc_roc']:.3f}")
                    st.caption(f"Lift: {row['lift_over_base']:.2f}x | Base: {row['base_rate']*100:.0f}%")

        # --- Latest prediction ---
        try:
            fx_latest_dt = conn.execute("""
                SELECT MAX(datetime_utc) FROM fx_ml_predictions
            """).fetchone()[0]
        except Exception:
            fx_latest_dt = None

        if fx_latest_dt is None:
            st.info("No predictions yet. Run `python main.py fx predict` to generate.")
        else:
            # Current signal display
            fx_preds = conn.execute("""
                SELECT direction, horizon, predicted_probability
                FROM fx_ml_predictions
                WHERE datetime_utc = ?
                ORDER BY direction, horizon
            """, [fx_latest_dt]).fetchdf()

            fx_price_row = conn.execute("""
                SELECT gbpeur_close FROM fx_prices_hourly WHERE datetime_utc = ?
            """, [fx_latest_dt]).fetchone()
            fx_price = float(fx_price_row[0]) if fx_price_row else None

            prob_up_24h = 0
            prob_down_24h = 0
            prob_up_48h = 0
            prob_down_48h = 0
            for _, row in fx_preds.iterrows():
                key = f"{row['direction']}_{row['horizon']}"
                if key == "up_24h":
                    prob_up_24h = row["predicted_probability"]
                elif key == "down_24h":
                    prob_down_24h = row["predicted_probability"]
                elif key == "up_48h":
                    prob_up_48h = row["predicted_probability"]
                elif key == "down_48h":
                    prob_down_48h = row["predicted_probability"]

            # Signal determination
            if prob_up_24h >= 0.65 and prob_down_24h < 0.40:
                fx_signal = "BUY_GBP"
                signal_color = "green"
            elif prob_down_24h >= 0.65 and prob_up_24h < 0.40:
                fx_signal = "SELL_GBP"
                signal_color = "red"
            else:
                fx_signal = "WAIT"
                signal_color = "blue"

            st.markdown(f"### Signal: :{signal_color}[{fx_signal}]")
            if fx_price:
                st.caption(f"GBP/EUR: {fx_price:.5f} | Bar: {fx_latest_dt}")

            # --- Position-aware recommendation ---
            if fx_position is not None and fx_signal != "WAIT":
                if fx_position == "HOLDING_EUR" and fx_signal == "SELL_GBP":
                    st.success(
                        "**Hold position.** GBP/EUR expected to drop "
                        "(favorable for your EUR position)."
                    )
                elif fx_position == "HOLDING_EUR" and fx_signal == "BUY_GBP":
                    st.warning(
                        "**Consider converting back to GBP.** GBP/EUR expected to rise "
                        "(unfavorable for EUR position)."
                    )
                elif fx_position == "HOLDING_GBP" and fx_signal == "SELL_GBP":
                    st.warning("**Consider converting to EUR now.**")
                elif fx_position == "HOLDING_GBP" and fx_signal == "BUY_GBP":
                    st.success(
                        "**Hold position.** GBP/EUR expected to rise."
                    )

            # Probabilities display
            col1, col2 = st.columns(2)
            with col1:
                st.metric("P(Up 20pip 24h)", f"{prob_up_24h:.1%}")
                st.metric("P(Up 20pip 48h)", f"{prob_up_48h:.1%}")
            with col2:
                st.metric("P(Down 20pip 24h)", f"{prob_down_24h:.1%}")
                st.metric("P(Down 20pip 48h)", f"{prob_down_48h:.1%}")

            # --- Recent predictions detail ---
            with st.expander("Recent predictions detail", expanded=False):
                fx_recent_preds = get_fx_recent_predictions(conn, limit=30)
                if fx_recent_preds.empty:
                    st.info("No predictions yet.")
                else:
                    fx_recent_preds["Outcome"] = fx_recent_preds.apply(
                        lambda r: (
                            f"{'HIT' if r['actual_hit'] else 'miss'} "
                            f"({r['actual_max_pips']:+.1f} pips)"
                        ) if pd.notna(r["actual_hit"]) else "pending",
                        axis=1,
                    )
                    fx_recent_preds["pct_move_str"] = fx_recent_preds.apply(
                        lambda r: format_pct_move(pct_move_fx(
                            r.get("direction"),
                            r.get("actual_max_pips"),
                            r.get("price_at_prediction"),
                            r.get("current_price"),
                            pd.notna(r.get("outcome_filled_at")),
                        )),
                        axis=1,
                    )
                    fx_recent_preds["time_remaining_str"] = fx_recent_preds.apply(
                        lambda r: format_time_remaining(time_remaining_hours(
                            r.get("maturity_datetime"),
                            outcome_filled=pd.notna(r.get("outcome_filled_at")),
                        )),
                        axis=1,
                    )
                    st.dataframe(
                        fx_recent_preds[
                            ["datetime_utc", "direction", "horizon",
                             "predicted_probability", "price_at_prediction",
                             "maturity_datetime", "price_at_maturity",
                             "pct_move_str", "time_remaining_str", "Outcome"]
                        ],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "predicted_probability": st.column_config.NumberColumn(
                                "Probability", format="%.1f%%"
                            ),
                            "price_at_prediction": st.column_config.NumberColumn(
                                "Price @ Pred", format="%.5f", width="small"
                            ),
                            "maturity_datetime": st.column_config.DatetimeColumn(
                                "Maturity (UTC)", format="YYYY-MM-DD HH:mm",
                                width="medium"
                            ),
                            "price_at_maturity": st.column_config.NumberColumn(
                                "Price @ Maturity", format="%.5f", width="small"
                            ),
                            "pct_move_str": st.column_config.TextColumn(
                                "% Move", width="small"
                            ),
                            "time_remaining_str": st.column_config.TextColumn(
                                "Time Left", width="small"
                            ),
                        },
                    )

        # --- Historical Accuracy ---
        st.subheader("Historical Accuracy")
        try:
            fx_accuracy = conn.execute("""
                SELECT direction, horizon, COUNT(*) AS n,
                       SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS hits,
                       AVG(actual_max_pips) AS avg_pips
                FROM fx_ml_predictions
                WHERE outcome_filled_at IS NOT NULL
                GROUP BY direction, horizon
                ORDER BY direction, horizon
            """).fetchdf()
        except Exception:
            fx_accuracy = pd.DataFrame()

        if fx_accuracy.empty:
            st.info("No outcomes yet -- predictions need time for their horizons to elapse.")
        else:
            fx_accuracy["Precision"] = (fx_accuracy["hits"] / fx_accuracy["n"] * 100).round(1)
            fx_accuracy["Avg MaxPips"] = fx_accuracy["avg_pips"].round(1)
            st.dataframe(
                fx_accuracy[["direction", "horizon", "n", "hits", "Precision", "Avg MaxPips"]],
                use_container_width=True, hide_index=True,
            )

        # --- Recent Signals ---
        st.subheader("Recent Signals")
        try:
            fx_signals = conn.execute("""
                SELECT datetime_utc, signal_type, gbpeur_price,
                       prob_up_24h, prob_down_24h, telegram_sent,
                       outcome_pips_24h
                FROM fx_signals
                ORDER BY datetime_utc DESC
                LIMIT 20
            """).fetchdf()
        except Exception:
            fx_signals = pd.DataFrame()

        if fx_signals.empty:
            st.info("No signals generated yet.")
        else:
            st.dataframe(fx_signals, use_container_width=True, hide_index=True)

conn.close()
