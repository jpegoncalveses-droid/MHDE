"""§9 — the discovery BATCH runner (its own cadence, NOT the tick loop).

The substrate tick loop keeps computing primitives + labels. This separate batch pass
periodically: refreshes the engineered primitives (computed on-read, lookahead-free),
runs Stage 1 (generate -> score -> permutation null) on settled labels, records the run
funnel + every survivor, advances rule states via forward confirmation against the newly
settled labels, runs Stage 2 (exit discovery) on entries that still lack an exit, and logs
simulated round trips for promoted rules. It writes ONLY the discovery DB; it reads the
brain store + registry read-only.

Wired as a user-scope systemd .service + .timer (BUILT-NOT-DEPLOYED — enabling it is the
operator's deploy, not this PR). It consumes the already-forward-only label store, so no
history replay / cursor seeding is needed; ``frontier`` (the settlement watermark) is the
discovery window stamped on new rules so forward confirmation only ever uses instances that
settled AFTER discovery.

``run_discovery_pass`` is the pure orchestration over already-loaded data (unit-testable);
``run_discovery`` is the thin store-loading wrapper the CLI/systemd unit calls.
"""
from __future__ import annotations

import statistics
from typing import Mapping, Optional, Sequence

from crypto.research.brain import labels as brain_labels
from crypto.research.brain import registry as brain_registry
from crypto.research.brain import store as brain_store
from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import confirmation as CF
from crypto.research.brain.discovery import engineered as E
from crypto.research.brain.discovery import exits as X
from crypto.research.brain.discovery import rules as R
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import scoring as S
from crypto.research.brain.discovery import tradelog as TL


def needed_datasets(base_features=E.BASE_FEATURES) -> list[str]:
    return sorted({bf.dataset for bf in base_features})


def build_price_index(markprice_rows: Sequence[Mapping]) -> dict:
    """``symbol -> {window_start_ns: (close, high, low)}`` from markprice snapshots."""
    idx: dict = {}
    for s in markprice_rows:
        idx.setdefault(s["symbol"], {})[int(s["window_start_ns"])] = (
            s["mark_close"], s["mark_high"], s["mark_low"])
    return idx


def coin_volatilities(price_index: Mapping[str, Mapping[int, tuple]]) -> dict:
    """Per-coin volatility = population std of consecutive-window simple returns (the scale
    for the vol-multiple exit barriers). None when too few windows."""
    vols: dict = {}
    for sym, wmap in price_index.items():
        ws = sorted(wmap)
        rets = [wmap[ws[i]][0] / wmap[ws[i - 1]][0] - 1.0
                for i in range(1, len(ws)) if wmap[ws[i - 1]][0]]
        vols[sym] = statistics.pstdev(rets) if len(rets) >= 2 else None
    return vols


def build_continuation(symbol: str, t_entry: int, price_index, engineered, *,
                       max_cap: int, window_ns: int) -> Optional[list]:
    """The forward continuation (rel_* per window + engineered fv) from the entry close."""
    wmap = price_index.get(symbol)
    if wmap is None:
        return None
    ref = wmap.get(t_entry)
    if ref is None or not ref[0]:
        return None
    refp = ref[0]
    cont = []
    for k in range(1, max_cap + 1):
        bar = wmap.get(t_entry + k * window_ns)
        if bar is None:
            break                                       # truncated (un-settled forward path)
        cont.append({"rel_high": bar[1] / refp, "rel_low": bar[2] / refp,
                     "rel_close": bar[0] / refp, "fv": engineered.get((symbol, t_entry + k * window_ns))})
    return cont


def _entry_continuations(entry_rule, engineered, price_index, coin_vols, *, max_cap, window_ns,
                         only_settled_at: Optional[int] = None):
    """Build continuations + per-instance vols for an entry's firing instances."""
    conts: dict = {}
    vols: dict = {}
    for k in R.fires(entry_rule, engineered):
        if only_settled_at is not None and k[1] > only_settled_at:
            continue
        v = coin_vols.get(k[0])
        if not v:
            continue
        c = build_continuation(k[0], k[1], price_index, engineered, max_cap=max_cap,
                               window_ns=window_ns)
        if c:
            conts[k] = c
            vols[k] = v
    return conts, vols


def run_discovery_pass(conn, engineered, lifts, price_index, coin_vols, *, feature_ids,
                       frontier_ns, now_ns, score_horizon_min=dcfg.SCORE_HORIZON_MIN,
                       n_bins=dcfg.QUANTILE_BINS, n_permutations=dcfg.N_PERMUTATIONS,
                       null_quantile=dcfg.NULL_QUANTILE, min_firing=dcfg.MIN_FIRING_INSTANCES,
                       max_depth=dcfg.MAX_DEPTH, m=dcfg.CONFIRM_M, z=dcfg.CONFIRM_Z,
                       exit_grid=None, window_ns=dcfg.WINDOW_NS, seed=0) -> dict:
    """One discovery pass over already-loaded data. Returns a summary dict."""
    exit_grid = exit_grid if exit_grid is not None else X.build_exit_grid()
    max_cap = max(er.time_cap_min for er in exit_grid)

    # 1. Stage 1: generate -> score -> null.
    survivors, diagnostics = S.discover_entries(
        engineered, lifts, feature_ids=feature_ids, n_bins=n_bins,
        n_permutations=n_permutations, null_quantile=null_quantile, min_firing=min_firing,
        max_depth=max_depth, seed=seed)
    for er in survivors:
        breadth = len({k[0] for k in R.fires(er.rule, engineered)})
        RS.upsert_entry(conn, er, score_horizon_min=score_horizon_min, breadth=breadth,
                        discovery_window_ns=frontier_ns, now_ns=now_ns)
    RS.record_run(conn, started_at_ns=now_ns, frontier_ns=frontier_ns,
                  score_horizon_min=score_horizon_min, funnel=diagnostics,
                  n_survivors=len(survivors))

    # 2. Forward confirmation: advance discovered->confirming->promoted|rejected.
    conf = CF.run_confirmation(conn, engineered, lifts, m=m, z=z, now_ns=now_ns)

    # 3. Stage 2: exit discovery for confirming/promoted entries that still lack an exit.
    exits_found = 0
    for state in (RS.CONFIRMING, RS.PROMOTED):
        for row in RS.list_rules(conn, state=state):
            if row["exit_def"] is not None:
                continue
            entry_rule = RS.deserialize_rule(row["entry_def"])
            conts, vols = _entry_continuations(entry_rule, engineered, price_index, coin_vols,
                                               max_cap=max_cap, window_ns=window_ns,
                                               only_settled_at=frontier_ns)
            inst = list(conts)
            if len(inst) < min_firing:
                continue
            res = X.discover_exit(inst, conts, vols, exit_grid=exit_grid,
                                  n_permutations=n_permutations, null_quantile=null_quantile,
                                  min_firing=min_firing, seed=seed)
            if res is not None:
                RS.set_exit(conn, row["rule_id"], X.exit_to_json(res.exit_rule), now_ns)
                exits_found += 1

    # 4. Log simulated round trips for promoted rules that have an exit.
    trades_logged = 0
    for row in RS.list_rules(conn, state=RS.PROMOTED):
        if row["exit_def"] is None:
            continue
        entry_rule = RS.deserialize_rule(row["entry_def"])
        exit_rule = X.exit_from_json(row["exit_def"])
        conts, vols = _entry_continuations(entry_rule, engineered, price_index, coin_vols,
                                           max_cap=exit_rule.time_cap_min, window_ns=window_ns)
        trades = TL.build_trades(row["rule_id"], exit_rule, list(conts), conts, vols,
                                 window_ns=window_ns, now_ns=now_ns)
        trades_logged += TL.record_trades(conn, trades, exit_def=row["exit_def"], now_ns=now_ns)

    return {"survivors": len(survivors), "diagnostics": diagnostics, "exits_found": exits_found,
            "trades_logged": trades_logged, **conf}


def run_discovery(*, store_root=dcfg.BRAIN_STORE_ROOT, label_store_root=dcfg.LABEL_STORE_ROOT,
                  registry_path=dcfg.BRAIN_REGISTRY_PATH, discovery_db_path=dcfg.DISCOVERY_DB_PATH,
                  now_ns: int, score_horizon_min=dcfg.SCORE_HORIZON_MIN, seed=0, **pass_kw) -> dict:
    """Load the brain store + labels + registry frontier and run one discovery pass."""
    raw = {ds: brain_store.read_snapshots(store_root, ds) for ds in needed_datasets()}
    engineered = E.compute_engineered(raw)
    label_rows = brain_store.read_snapshots(label_store_root, brain_labels.LABEL_DATASET)
    lifts = S.compute_instance_lifts(label_rows, horizon_min=score_horizon_min,
                                     side=dcfg.SCORE_SIDE)
    markprice_rows = raw.get(brain_labels.MARKPRICE_DATASET) or []
    price_index = build_price_index(markprice_rows)
    coin_vols = coin_volatilities(price_index)

    reg = brain_registry.connect(registry_path)
    try:
        frontier = brain_labels._markprice_frontier_ns(reg) or 0
    finally:
        reg.close()

    conn = RS.connect(discovery_db_path)
    TL.ensure_schema(conn)
    try:
        return run_discovery_pass(
            conn, engineered, lifts, price_index, coin_vols,
            feature_ids=E.engineered_feature_ids(), frontier_ns=frontier, now_ns=now_ns,
            score_horizon_min=score_horizon_min, seed=seed, **pass_kw)
    finally:
        conn.close()
