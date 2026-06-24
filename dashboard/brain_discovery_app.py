"""Brain discovery engine — operator dashboard (§10). Read-only, mobile-readable.

A SEPARATE Streamlit app (decoupled from the production dashboard so it can never disturb
the live page) over the brain's stores. Run it with:

    MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python -m streamlit run dashboard/brain_discovery_app.py

It NEVER mutates a store. Five levels: liveness, discovery activity, the rule store (the
heart), the trade log, and aggregate health.

HONEST EXPECTATION (§11), surfaced on the page: early on you will see huge candidate counts
with almost everything dying at the null, slow accumulation in ``confirming``, and few or no
promotions. That is CORRECT — the null is designed to kill the overwhelming majority. The
metric that matters is whether anything survives FORWARD confirmation and holds.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from dashboard.auth import require_auth
from dashboard.services import brain_discovery_queries as Q


def _ts(ns) -> str:
    if not ns:
        return "—"
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _table(rows, round_cols=()):
    if not rows:
        st.info("No data yet.")
        return
    df = pd.DataFrame(rows)
    for c in round_cols:
        if c in df.columns:
            df[c] = df[c].astype(float).round(6)
    st.dataframe(df, use_container_width=True, hide_index=True)


def main():
    st.set_page_config(page_title="MHDE — Brain Discovery", layout="wide",
                       initial_sidebar_state="collapsed")
    require_auth()
    st.title("Brain Discovery Engine")
    st.caption("Read-only. Early on: huge candidate counts, almost all dying at the null, "
               "few/no promotions — that is correct (§11). What matters is forward-confirmed "
               "rules that hold.")

    tab_live, tab_act, tab_rules, tab_trades, tab_health = st.tabs(
        ["Liveness", "Discovery activity", "Rule store", "Trade log", "Aggregate health"])

    # 1 — liveness
    with tab_live:
        rows = Q.liveness()
        if rows:
            st.metric("Last substrate advance",
                      _ts(max((r["updated_at_ns"] for r in rows), default=0)))
        _table([{"reader": r["reader"], "cursor_recv_ns": r["last_recv_ts_ns"],
                 "updated": _ts(r["updated_at_ns"])} for r in rows])

    # 2 — discovery activity (the funnel)
    with tab_act:
        runs = Q.runs()
        if runs:
            latest = runs[0]
            st.caption(f"Latest run @ {_ts(latest['started_at_ns'])} · "
                       f"horizon {latest['score_horizon_min']}m · survivors {latest['n_survivors']}")
            try:
                funnel = json.loads(latest["funnel"])
                _table([{"depth": d.get("depth"), "candidates": d.get("n_candidates"),
                         "scorable": d.get("n_scorable"), "null_bar": d.get("null_bar"),
                         "passed_null": d.get("n_passed")} for d in funnel],
                       round_cols=("null_bar",))
            except (ValueError, TypeError):
                pass
        _table([{"run": r["run_id"], "when": _ts(r["started_at_ns"]),
                 "survivors": r["n_survivors"], "promoted": r["n_promoted"]} for r in runs])

    # 3 — the rule store (the heart)
    with tab_rules:
        state = st.selectbox("State", ["(all)", "discovered", "confirming", "promoted", "rejected"])
        rows = Q.rules(None if state == "(all)" else state)
        _table([{"rule": r["rule_id"], "state": r["state"], "depth": r["depth"],
                 "edge": r["insample_edge"], "null_margin": r["null_margin"],
                 "fresh": r["fresh_count"], "fwd_edge": r["forward_edge"],
                 "breadth": r["breadth"], "freq": r["n_fires"], "exit": r["exit_def"]}
                for r in rows], round_cols=("edge", "null_margin", "fwd_edge"))

    # 4 — trade log (promoted rules)
    with tab_trades:
        promoted = [r["rule_id"] for r in Q.rules("promoted")]
        if promoted:
            rid = st.selectbox("Promoted rule", promoted)
            _table([{"coin": t["symbol"], "entry": _ts(t["entry_window_ns"]),
                     "exit": _ts(t["exit_window_ns"]), "held(win)": t["holding_windows"],
                     "why": t["exit_reason"], "return": t["rt_return"],
                     "risk_adj": t["rt_vol_normalized"]} for t in Q.trades(rid)],
                   round_cols=("return", "risk_adj"))
        else:
            st.info("No promoted rules yet.")

    # 5 — aggregate health
    with tab_health:
        agg = Q.aggregates()
        if agg:
            _table([{"rule": rid, "trades": a["n_trades"], "hit_rate": a["hit_rate"],
                     "mean_risk_adj": a["mean_vol_normalized"],
                     "cum_risk_adj": a["sum_vol_normalized"]} for rid, a in agg.items()],
                   round_cols=("hit_rate", "mean_risk_adj", "cum_risk_adj"))
            rid = st.selectbox("Equity curve for", list(agg))
            pts = Q.equity(rid)
            if pts:
                st.line_chart(pd.DataFrame({"cum_risk_adj": [p[1] for p in pts]}))
        else:
            st.info("No promoted-rule round trips yet.")


if __name__ == "__main__":
    main()
