"""MHDE Dashboard — ML Predictions with health checks."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import duckdb
import pandas as pd
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services.maturity import (
    crypto_feature_date_to_trading_date,
    crypto_trading_date_to_feature_date,
    format_crypto_exclusion_badge,
    format_crypto_predictions_summary,
    format_crypto_trading_date_banner,
    format_equity_t2_banner,
    format_pct_move,
    format_time_remaining,
    pct_move_equity_or_crypto,
    pct_move_fx,
    time_remaining_days,
    time_remaining_hours,
)
from dashboard.services.queries import (
    engine_db_path,
    get_crypto_predictions,
    get_crypto_recent_outcomes,
    get_daily_balance_since_baseline,
    get_distinct_prediction_dates,
    get_equity_predictions,
    get_equity_recent_outcomes,
    get_fx_recent_predictions,
    build_position_chart_frame,
    get_paper_engine_runs_summary,
    get_pre_baseline_open_summary,
    get_paper_failed_entries,
    get_paper_position_snapshots,
    get_paper_today_cohort,
    load_signal_probe_snapshot,
    paper_baseline_date,
    position_is_armed,
    signal_probe_db_path,
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


def _open_engine_conn():
    """Fresh read-only connection to the crypto-trading-engine DuckDB
    (ADR-020). Acquired per render (KI-105) so the Paper Trading tab never
    caches a stale handle or holds a write lock against the engine."""
    return duckdb.connect(engine_db_path(), read_only=True)


# --- System Health: Data Freshness (all engines) ---
from pipelines.freshness import check_all as _check_all_freshness

conn = _open_conn()
try:
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
except Exception as exc:  # noqa: BLE001 — non-critical banner; never crash the page
    # The freshness banner is best-effort observability and must never crash
    # the page, so catch broadly: a missing/renamed table raises
    # duckdb.CatalogException, but any other failure here is equally non-fatal.
    # Degrade to a visible notice and let every tab below still render.
    # freshness.py is deliberately left to keep hard-failing for the health
    # check (health/checks.py) — this guard is the dashboard call site only.
    logging.getLogger("dashboard.app").warning(
        "System Health freshness banner unavailable: %s", exc
    )
    st.warning(
        "⚠️ System Health — Data Freshness banner unavailable "
        "(a freshness check failed; see logs). Dashboard tabs below are "
        "unaffected."
    )
finally:
    conn.close()

# Paper Trading first so it is the default tab on open (paper-tab-overhaul).
tab_paper, tab_equities, tab_crypto, tab_fx, tab_probe = st.tabs(
    ["Paper Trading", "Equities", "Crypto", "FX", "Signal Probe"]
)

# ===============================================================================
# EQUITIES TAB
# ===============================================================================
conn = _open_conn()
with tab_equities:
    try:
        st.title("ML Predictions")
        st.caption(
            "Equity predictions run on a **T-2 cadence**: free-tier Polygon "
            "delays the current-day grouped-daily endpoint by ≥2 trading days, "
            "so each scoring run uses the most recent fully-covered feature "
            "snapshot. See `docs/EQUITY_WORKSTREAM_PAUSED.md` for the "
            "architectural decision and the path to T-0 if/when paper-trading "
            "calibration justifies a paid data tier."
        )

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
                    help=(
                        "Dates available in ml_predictions. Equity cadence is "
                        "T-2 (see banner below)."
                    ),
                )

                st.markdown(format_equity_t2_banner(
                    prediction_date=selected_date,
                    today=datetime.now(timezone.utc).date(),
                ))

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
                st.subheader(
                    f"Predictions for {selected_date} ({len(filtered)} shown)"
                )

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
    except Exception as exc:  # noqa: BLE001 — dormant equity tables must not crash the page
        logging.getLogger("dashboard.app").warning("Equity tab unavailable: %s", exc)
        st.warning(
            "⚠️ Equity data unavailable — the equity engine is dormant and its "
            "`ml_*` tables are absent. Other tabs are unaffected."
        )

conn.close()

# ===============================================================================
# CRYPTO TAB
# ===============================================================================
conn = _open_conn()
with tab_crypto:
    try:
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
            # Backend `prediction_date` is the feature-snapshot date (T-1 =
            # the just-completed daily bar). Operationally the predictions
            # are generated for the FOLLOWING calendar day's trading; the
            # selector relabels accordingly. Backend column semantics
            # unchanged — translation is presentation-only.
            crypto_dates = get_distinct_prediction_dates(
                conn, "crypto_ml_predictions", "prediction_date", limit=30
            )

            if not crypto_dates:
                st.warning("No predictions yet. Run `python main.py crypto predict` to generate.")
            else:
                crypto_trading_dates = [
                    crypto_feature_date_to_trading_date(d) for d in crypto_dates
                ]
                crypto_selected_trading_date = st.selectbox(
                    "Trading date",
                    crypto_trading_dates,
                    format_func=lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
                    key="crypto_trading_date",
                    help=(
                        "Date predictions drive trading on. The backend "
                        "`crypto_ml_predictions.prediction_date` is the "
                        "feature-snapshot date (T-1); this label is "
                        "trading-date (T) for operator clarity."
                    ),
                )
                crypto_selected_date = crypto_trading_date_to_feature_date(
                    crypto_selected_trading_date
                )

                # --- Banner: trading date, features-as-of date, generation time ---
                _crypto_predicted_at = conn.execute(
                    "SELECT MAX(predicted_at) FROM crypto_ml_predictions "
                    "WHERE prediction_date = ?",
                    [crypto_selected_date],
                ).fetchone()
                _crypto_predicted_at_val = (
                    _crypto_predicted_at[0] if _crypto_predicted_at else None
                )
                st.markdown(format_crypto_trading_date_banner(
                    prediction_date=crypto_selected_date,
                    predicted_at=_crypto_predicted_at_val,
                    today=datetime.now(timezone.utc).date(),
                ))

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
                # feat-dashboard-crypto-exclusion-overlay: surface which
                # raw predictions actually reach the trading engine.
                _n_total = len(crypto_filtered)
                _excluded_mask = (
                    crypto_filtered["is_excluded"].fillna(False).astype(bool)
                    if "is_excluded" in crypto_filtered.columns
                    else pd.Series([False] * _n_total, index=crypto_filtered.index)
                )
                _n_excluded = int(_excluded_mask.sum())
                _excl_reasons = set(
                    crypto_filtered.loc[_excluded_mask, "exclusion_reason"]
                    .dropna().astype(str).tolist()
                ) if "exclusion_reason" in crypto_filtered.columns else set()
                st.subheader(format_crypto_predictions_summary(
                    n_total=_n_total,
                    n_excluded=_n_excluded,
                    exclusion_reasons=_excl_reasons,
                ))

                if crypto_filtered.empty:
                    st.info("No predictions match filters.")
                else:
                    display = crypto_filtered.copy()
                    display["Prob"] = display["predicted_probability"].map(lambda p: f"{p:.1%}")
                    display["Confidence"] = display["predicted_probability"].map(
                        lambda p: "High" if p >= 0.60 else "Lower")

                    # Status column: per-row exclusion badge takes precedence
                    # over the pending/outcome string. Excluded predictions
                    # never trade, so the badge is the operationally
                    # authoritative answer for that row.
                    _outcome_str = display.apply(
                        lambda r: f"{'HIT' if r['actual_hit'] else 'miss'} ({r['actual_max_return']:+.1%})"
                        if pd.notna(r.get("actual_hit")) else "pending",
                        axis=1,
                    ) if "actual_hit" in display.columns else "pending"
                    display["Status"] = display.apply(
                        lambda r: format_crypto_exclusion_badge(
                            reason=r.get("exclusion_reason"),
                            dd90=r.get("exclusion_dd90"),
                            ret60=r.get("exclusion_ret60"),
                            ret5=r.get("exclusion_ret5"),
                        ) or (
                            f"{'HIT' if r['actual_hit'] else 'miss'} "
                            f"({r['actual_max_return']:+.1%})"
                            if pd.notna(r.get("actual_hit"))
                            else "pending"
                        ),
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
                                 "pct_move_str", "time_remaining_str",
                                 "Status"]

                    # Visual de-emphasis on excluded rows. Streamlit's
                    # st.dataframe accepts a pandas Styler; we grey the
                    # entire row when is_excluded is True so the operator's
                    # eye treats excluded rows as informational, not actionable.
                    _styled = display[show_cols].style
                    if "is_excluded" in display.columns:
                        _excluded_idx = display.index[_excluded_mask].tolist()

                        def _grey_excluded_row(row):
                            return (
                                ["color: #888; font-style: italic"] * len(row)
                                if row.name in _excluded_idx else [""] * len(row)
                            )
                        _styled = _styled.apply(_grey_excluded_row, axis=1)

                    st.dataframe(
                        _styled,
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
                            "Status": st.column_config.TextColumn(
                                "Status",
                                help=(
                                    "Outcome (pending / HIT / miss) for "
                                    "non-excluded rows, OR exclusion badge "
                                    "for predictions filtered by the "
                                    "post-parabolic / short-momentum rule "
                                    "chain (crypto/ml/postparabolic_filter.py, "
                                    "ADR-021 + ADR-028). Excluded rows are "
                                    "informational only — the engine never "
                                    "sees them."
                                ),
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
    except Exception as exc:  # noqa: BLE001 — a dropped crypto_* table must not crash the page
        logging.getLogger("dashboard.app").warning("Crypto tab unavailable: %s", exc)
        st.warning(
            "⚠️ Crypto data unavailable — a required table is missing or "
            "unreadable. Other tabs are unaffected."
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

# ===============================================================================
#  TAB 4 — Paper Trading (crypto-trading-engine DuckDB, read-only — ADR-020)
# ===============================================================================
_PAPER_BADGE = {"info": "🟢", "warn": "🟡", "critical": "🔴", "error": "⚪"}


@st.cache_data(ttl=60)
def _paper_drift_status() -> dict:
    """Run the Gap-2 drift monitor (read-only, no Telegram) and return its
    result as plain types. Cached 60s so page interactions don't re-query the
    engine DB on every rerun."""
    try:
        from monitoring.paper_trading_drift import run as _run_drift
        r = _run_drift()
        return {
            "status": r.status, "severity": r.severity, "title": r.title,
            "body": r.body, "metrics": dict(r.metrics),
        }
    except Exception as exc:  # engine DB unreadable, import error, etc.
        return {
            "status": "error", "severity": "error",
            "title": "drift monitor unavailable",
            "body": f"{type(exc).__name__}: {exc}", "metrics": {},
        }


def _paper_age_str(ts) -> str:
    if ts is None:
        return "—"
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - ts
    mins = delta.total_seconds() / 60.0
    if mins < 0:
        return "just now"
    return f"{mins:.0f} min ago" if mins < 120 else f"{mins / 60.0:.1f} h ago"


def _paper_trail_params() -> tuple[float, float, bool]:
    """(trail_pct, activation_pct, from_spec). Reads data/exports/active_spec.json;
    falls back to the locked Phase-1B Policy-D defaults if it's missing/unparseable."""
    try:
        with open(os.path.join("data", "exports", "active_spec.json")) as fh:
            w = json.load(fh).get("phase_1b_winner", {})
        return float(w.get("trail_pct", 0.30)), float(w.get("activation_pct", 0.01)), True
    except Exception:
        return 0.30, 0.01, False


def _paper_fmt_price(v) -> str:
    """Compact price string; '—' for missing. Crypto prices span 0.007–0.5+,
    so 6 significant figures keeps small-tick symbols readable."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:.6g}"


def _paper_fmt_usd(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"${v:,.2f}"


def _paper_fmt_signed_usd(v, *, unrealized: bool) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+,.2f}" + ("*" if unrealized else "")


def _paper_fmt_pct(v, *, unrealized: bool) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+.2f}%" + ("*" if unrealized else "")


def _paper_fmt_net(v, *, pending: bool, kind: str, unrealized: bool = False) -> str:
    """Render a net-column cell. ``pending`` (no reconcile since the last fill)
    wins over any value; a NULL value once reconciled shows "—", never 0.
    ``kind``: ``signed`` (+/-), ``pct`` (+/-%), or ``usd`` (cost, $)."""
    if pending:
        return "pending"
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    star = "*" if unrealized else ""
    if kind == "pct":
        return f"{v:+.2f}%" + star
    if kind == "signed":
        return f"{v:+,.2f}" + star
    return f"${v:,.2f}"


def _paper_position_chart(frame, *, armed: bool):
    """Layered altair chart for one position: price (blue), entry (gray dashed),
    and the red exit reference (stepwise trail-stop when armed, else the flat
    activation line). Returns None when the frame is empty."""
    import altair as alt

    if frame.empty:
        return None
    base = alt.Chart(frame).encode(
        x=alt.X("timestamp:T", title=None)
    )
    price = base.mark_line(color="#1f77b4").encode(
        y=alt.Y("price:Q", title="price", scale=alt.Scale(zero=False))
    )
    entry = base.mark_line(color="#888888", strokeDash=[4, 3]).encode(y="entry:Q")
    ref = base.mark_line(
        color="#d62728", interpolate="step-after"
    ).encode(y="exit_ref:Q")
    return (price + entry + ref).properties(height=190)


with tab_paper:
    st.title("Paper Trading")
    st.caption(
        f"Crypto-trading-engine state, read-only — `{engine_db_path()}` (ADR-020). "
        "Open-position P&L is an UNREALIZED live mark from the engine's "
        "`price_snapshots` (written each monitor cycle, ~60 s fresh)."
    )

    # --- Drift-monitor status banner (cached 60s) ---
    _drift = _paper_drift_status()
    _badge = _PAPER_BADGE.get(_drift.get("severity"), "⚪")
    st.markdown(f"### {_badge} {_drift.get('title', 'drift monitor')}")
    if _drift.get("body"):
        with st.expander("Drift monitor — all checks", expanded=_drift.get("status") != "ok"):
            st.text(_drift["body"])

    # --- Engine connection (per render; read-only) ---
    try:
        _engine_conn = _open_engine_conn()
    except Exception as _e:
        st.warning(
            f"Paper-trading engine database not available at `{engine_db_path()}` "
            f"— is the crypto-trading-engine deployed? ({type(_e).__name__}: {_e})"
        )
    else:
        try:
            _baseline = paper_baseline_date()
            st.subheader(f"Daily balance (since {_baseline.isoformat()})")
            _balance_df = get_daily_balance_since_baseline(_engine_conn, since=_baseline)
            if _balance_df.empty:
                st.info(
                    "No `daily_pnl` rows yet — the engine's reconcile timer is "
                    "disabled pending RECONCILE-001; the table populates once "
                    "the engine starts writing end-of-day equity."
                )
            else:
                _display_df = _balance_df.copy()
                _display_df["date"] = [
                    f"{d.isoformat()} (preliminary)" if prelim else d.isoformat()
                    for d, prelim in zip(_display_df["date"], _display_df["is_preliminary"])
                ]
                _display_df = _display_df.drop(columns=["is_preliminary"])
                st.dataframe(_display_df, use_container_width=True, hide_index=True)
                # fix-daily-balance-baseline-awareness: surface the pre-baseline
                # exclusions so the operator understands what the metrics
                # above attribute and what they don't.
                _pre = get_pre_baseline_open_summary(
                    _engine_conn, baseline_date=_baseline,
                )
                if _pre["n_pre_baseline_open_positions"] > 0:
                    st.caption(
                        f"Post-baseline strategy attribution: "
                        f"{_pre['n_pre_baseline_open_positions']} pre-baseline "
                        f"position(s) excluded "
                        f"(${_pre['pre_baseline_unrealized_pnl_usd']:+,.2f} "
                        f"unrealized, ${_pre['pre_baseline_cost_basis_usd']:,.2f} "
                        f"cost basis locked). See **Today's positions** below "
                        f"for the current cohort."
                    )
            st.caption(
                "Source: crypto-trading-engine `daily_pnl` (ADR-020, read-only) "
                "for the wallet `equity` column; per-row `realized` and "
                "`unrealized` are recomputed from `positions` filtered to "
                "`entry_date >= baseline_date` "
                "(fix-daily-balance-baseline-awareness). `daily Δ` = equity − "
                "prior present row; `cumulative Δ` is the running sum of "
                "`realized` P&L since baseline (inclusive of the first row). "
                "Today's row is synthesized in-process and updates "
                "live until reconcile fires at 23:00 UTC. `realized` is gross "
                "of funding/fees, so it diverges from the wallet `equity` "
                "curve by the accrued cost drag."
            )

            _summary = get_paper_engine_runs_summary(_engine_conn)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Engine monitor cycle", _paper_age_str(_summary["last_monitor_at"]))
            m2.metric("Last entry phase", _paper_age_str(_summary["last_entry_at"]))
            m3.metric("Open positions", _summary["n_open"])
            m4.metric("Closed (last 14d)", _summary["n_closed_14d"])

            _trail_pct, _activation_pct, _from_spec = _paper_trail_params()

            # ── Today's opened cohort: table + per-position price charts ──
            _cohort = get_paper_today_cohort(_engine_conn)
            _n = len(_cohort)
            st.subheader(f"Today's positions ({_n})")
            if _cohort.empty:
                st.info(
                    "No positions opened today yet — the entry phase runs at "
                    "06:30 UTC. Failed/cancelled entries (if any) are listed "
                    "under **Rejected entries** below."
                )
            else:
                _disp = pd.DataFrame({
                    "Symbol": _cohort["symbol"],
                    "Entry": _cohort["entry_price"].map(_paper_fmt_price),
                    "Exit": [
                        "open" if o else _paper_fmt_price(x)
                        for o, x in zip(_cohort["is_open"], _cohort["exit_price"])
                    ],
                    "Opened $": _cohort["opened_usd"].map(_paper_fmt_usd),
                    "PnL $": [
                        _paper_fmt_signed_usd(p, unrealized=o)
                        for p, o in zip(_cohort["pnl_usd"], _cohort["is_open"])
                    ],
                    "PnL %": [
                        _paper_fmt_pct(p, unrealized=o)
                        for p, o in zip(_cohort["pnl_pct"], _cohort["is_open"])
                    ],
                    "Funding": [
                        _paper_fmt_net(f, pending=bool(pend), kind="signed")
                        for f, pend in zip(
                            _cohort["funding_usd"], _cohort["net_pending"]
                        )
                    ],
                    "Commission": [
                        _paper_fmt_net(c, pending=bool(pend), kind="usd")
                        for c, pend in zip(
                            _cohort["commission_usd"], _cohort["net_pending"]
                        )
                    ],
                    "Net PnL": [
                        _paper_fmt_net(n, pending=bool(pend), kind="signed",
                                       unrealized=o)
                        for n, pend, o in zip(
                            _cohort["net_pnl_usd"], _cohort["net_pending"],
                            _cohort["is_open"],
                        )
                    ],
                    "%Net PnL": [
                        _paper_fmt_net(n, pending=bool(pend), kind="pct",
                                       unrealized=o)
                        for n, pend, o in zip(
                            _cohort["net_pnl_pct"], _cohort["net_pending"],
                            _cohort["is_open"],
                        )
                    ],
                })
                st.dataframe(_disp, use_container_width=True, hide_index=True)
                st.caption(
                    "Open positions first, then closed by exit time (newest first). "
                    "`Opened $` = entry price × qty (per-position deployed dollars). "
                    "Closed `PnL $` = gross `realized_pnl_usd` (funding/fees not "
                    "subtracted — FUNDING-001). `*` = unrealized live mark "
                    "((latest snapshot − entry) × qty) for still-open positions. "
                    "`Net PnL` = gross + funding − commission (ADR-002); funding/"
                    "commission are backfilled by the nightly 23:00 UTC reconcile, "
                    "so today's fresh fills read **pending** until then. Per-position "
                    "net is best-effort (FUNDING-002 attribution gaps) — the "
                    "authoritative daily net is `daily_pnl.net_pnl_usd`."
                    + ("" if _from_spec else
                       " active_spec.json missing — charts use Phase-1B-D defaults.")
                )

                # Stacked charts, one per cohort row (dynamic N), same order.
                for _, _row in _cohort.iterrows():
                    _armed = position_is_armed(
                        entry_price=_row["entry_price"],
                        peak_price=_row["peak_price"],
                        activation_pct=_activation_pct,
                    )
                    _snaps = get_paper_position_snapshots(
                        _engine_conn, _row["id"], max_points=400
                    )
                    _frame = build_position_chart_frame(
                        _snaps,
                        entry_price=_row["entry_price"],
                        peak_price=_row["peak_price"],
                        trail_pct=_trail_pct,
                        activation_pct=_activation_pct,
                    )
                    _state = "open" if _row["is_open"] else "closed"
                    _ref = (
                        f"trail-stop (peak − {_trail_pct:.0%}·(peak − entry))"
                        if _armed
                        else f"activation (entry × {1 + _activation_pct:.2f})"
                    )
                    st.markdown(f"**{_row['symbol']}** — {_state}")
                    _chart = _paper_position_chart(_frame, armed=_armed)
                    if _chart is None:
                        st.caption("No price snapshots recorded for this position.")
                    else:
                        st.altair_chart(_chart, use_container_width=True)
                        st.caption(
                            f"Blue = price · gray dashed = entry · red = {_ref}. "
                            "Series downsampled to ≤400 points (global min/max "
                            "preserved)."
                        )

            with st.expander("Rejected entries", expanded=False):
                _failed_df = get_paper_failed_entries(_engine_conn, limit=20)
                if _failed_df.empty:
                    st.caption("None.")
                else:
                    st.dataframe(_failed_df, use_container_width=True, hide_index=True)
        finally:
            _engine_conn.close()

# ===============================================================================
# SIGNAL PROBE TAB (research, read-only)
# ===============================================================================
with tab_probe:
    st.title("Signal Probe")
    st.caption(
        f"Raw multi-window research features per symbol, read-only — "
        f"`{signal_probe_db_path()}`. Collected every ~60 s by the signal-probe "
        f"timer; raw values only (NULL = not computable this cycle). Read via an "
        f"in-memory read-only snapshot so it never contends with the collector's "
        f"writes."
    )

    try:
        _probe_df = load_signal_probe_snapshot(signal_probe_db_path())
    except Exception as _e:
        st.warning(
            f"Signal-probe research database not available at "
            f"`{signal_probe_db_path()}` — is the collector deployed / has it "
            f"written a cycle yet? ({type(_e).__name__}: {_e})"
        )
    else:
        if _probe_df.empty:
            st.info(
                "No signal-probe rows yet — the collector has not written any "
                "cycles to the research DB."
            )
        else:
            _minutes = (
                _probe_df["ts"].drop_duplicates().sort_values(ascending=False)
            )
            _latest_ts = _minutes.iloc[0]

            # --- Accumulation counters (visible at a glance) ---
            _m1, _m2, _m3, _m4 = st.columns(4)
            _m1.metric("Rows", f"{len(_probe_df):,}")
            _m2.metric("Columns", _probe_df.shape[1])
            _m3.metric("Distinct minutes", f"{len(_minutes):,}")
            _m4.metric("Symbols", f"{_probe_df['symbol'].nunique():,}")
            st.caption(f"Latest minute: `{_latest_ts}` (exchange close-time).")

            # --- Controls ---
            _c1, _c2 = st.columns([1, 2])
            with _c1:
                _minute_choice = st.selectbox(
                    "Minute",
                    options=["Latest", "All minutes"]
                    + [str(m) for m in _minutes],
                    index=0,
                    key="probe_minute",
                    help="Default shows the latest closed minute (all symbols).",
                )
            with _c2:
                _all_symbols = sorted(_probe_df["symbol"].unique())
                _symbol_choice = st.multiselect(
                    "Symbols (empty = all)",
                    options=_all_symbols,
                    default=[],
                    key="probe_symbols",
                )

            _s1, _s2 = st.columns([2, 1])
            with _s1:
                _cols = list(_probe_df.columns)
                _sort_col = st.selectbox(
                    "Sort by",
                    options=_cols,
                    index=_cols.index("symbol"),
                    key="probe_sort_col",
                )
            with _s2:
                _sort_dir = st.radio(
                    "Order",
                    options=["Asc", "Desc"],
                    index=0,
                    horizontal=True,
                    key="probe_sort_dir",
                )

            # --- Apply filters/sort (pure pandas on the in-memory snapshot) ---
            _view = _probe_df
            if _minute_choice == "Latest":
                _view = _view[_view["ts"] == _latest_ts]
            elif _minute_choice != "All minutes":
                _view = _view[_view["ts"].astype(str) == _minute_choice]
            if _symbol_choice:
                _view = _view[_view["symbol"].isin(_symbol_choice)]
            _view = _view.sort_values(
                _sort_col, ascending=(_sort_dir == "Asc"), kind="stable"
            )

            st.caption(
                f"Showing {len(_view):,} rows × {_view.shape[1]} columns "
                f"(horizontal scroll for the full width)."
            )
            st.dataframe(_view, use_container_width=True, hide_index=True)
