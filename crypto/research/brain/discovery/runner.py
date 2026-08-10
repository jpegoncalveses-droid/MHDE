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

import pyarrow.compute as pc

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


#: Label columns compute_instance_lifts actually consumes (+ window_end_ns for the floor filter).
#: Projecting to these drops fwd_return + recv_ts_ns, cutting the dominant label memory term.
_LABEL_LOAD_COLUMNS = ["symbol", "window_start_ns", "window_end_ns",
                       "horizon_min", "valid", "mfe", "mae"]


def build_price_index(markprice_rows: Sequence[Mapping]) -> dict:
    """``symbol -> {window_start_ns: (close, high, low)}`` from markprice snapshots.

    RETAINED, NOT DEAD: production uses ``build_price_index_columnar`` (reads the markprice
    arrow Table directly, no list-of-dicts); this version is kept as its equivalence ORACLE
    (test_brain_store_columnar)."""
    idx: dict = {}
    for s in markprice_rows:
        idx.setdefault(s["symbol"], {})[int(s["window_start_ns"])] = (
            s["mark_close"], s["mark_high"], s["mark_low"])
    return idx


def build_price_index_columnar(markprice_table) -> dict:
    """``build_price_index`` from a columnar markprice ``pyarrow.Table`` (option-B read) instead
    of a list-of-dicts — same output (``to_pylist`` maps a parquet NULL to None exactly as the
    dict path did), without materializing the markprice rows as Python dicts."""
    idx: dict = {}
    if markprice_table.num_rows == 0:
        return idx
    syms = markprice_table.column("symbol").to_pylist()
    wins = markprice_table.column("window_start_ns").to_pylist()
    close = markprice_table.column("mark_close").to_pylist()
    high = markprice_table.column("mark_high").to_pylist()
    low = markprice_table.column("mark_low").to_pylist()
    for s, w, c, h, l in zip(syms, wins, close, high, low):
        idx.setdefault(s, {})[int(w)] = (c, h, l)
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
                       max_depth=dcfg.MAX_DEPTH, beam_width=dcfg.BEAM_WIDTH,
                       m=dcfg.CONFIRM_M, z=dcfg.CONFIRM_Z,
                       exit_grid=None, window_ns=dcfg.WINDOW_NS, seed=0) -> dict:
    """One discovery pass over already-loaded data. Returns a summary dict."""
    exit_grid = exit_grid if exit_grid is not None else X.build_exit_grid()
    max_cap = max(er.time_cap_min for er in exit_grid)

    # 1. Stage 1: generate -> score -> null.
    survivors, diagnostics = S.discover_entries(
        engineered, lifts, feature_ids=feature_ids, n_bins=n_bins,
        n_permutations=n_permutations, null_quantile=null_quantile, min_firing=min_firing,
        max_depth=max_depth, beam_width=beam_width, seed=seed)
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
    """Load the RECENT WINDOW of the brain store + labels + registry frontier and run one pass.

    Reads only the last ``DISCOVERY_HISTORY_DAYS`` of horizon-``score_horizon_min`` labels and a
    slightly wider primitive window (``+DISCOVERY_PRIMITIVE_LOOKBACK_DAYS`` for full-fidelity
    z-score), streamed + column-projected so peak memory is the window + one batch, not the whole
    ~22G store (the discovery-OOM fix). ALL symbols are kept — the cross-universe rank needs them;
    only TIME is bounded, and both raw + labels are freed before the heavy null pass.
    """
    # Frontier first -> the two windowing floors (labels vs the z-lookback-widened primitive floor).
    reg = brain_registry.connect(registry_path)
    try:
        frontier = brain_labels._markprice_frontier_ns(reg) or 0
    finally:
        reg.close()
    label_floor = max(0, frontier - dcfg.DISCOVERY_HISTORY_NS)
    primitive_floor = max(0, frontier - (dcfg.DISCOVERY_HISTORY_NS + dcfg.DISCOVERY_PRIMITIVE_LOOKBACK_NS))

    def _read_ds(dataset, columns):
        cols = list(columns)
        if "window_end_ns" not in cols:
            cols.append("window_end_ns")           # the window_end floor filter needs this column
        return brain_store.read_snapshots_columnar(
            store_root, dataset, after_recv_ts_ns=primitive_floor,
            window_end_floor_ns=primitive_floor, columns=cols)

    # Columnar streaming primitive read (option B): each dataset read ONCE as a projected columnar
    # table, so peak is one dataset's needed columns + the output matrix, NOT the ~12-13G summed
    # list-of-dicts that OOMed the read. Engineered output is byte-identical to the scalar path
    # (compute_engineered oracle test). ALL symbols kept (cross-universe rank); only TIME is bounded.
    engineered = E.compute_engineered_columnar(_read_ds)
    mp_tbl = _read_ds(brain_labels.MARKPRICE_DATASET,
                      ["symbol", "window_start_ns", "mark_close", "mark_high", "mark_low"])
    price_index = build_price_index_columnar(mp_tbl)
    coin_vols = coin_volatilities(price_index)
    del mp_tbl                                  # free the markprice read before the heavy null pass

    label_rows = brain_store.read_snapshots(
        label_store_root, brain_labels.LABEL_DATASET,
        after_recv_ts_ns=label_floor, window_end_floor_ns=label_floor,
        columns=_LABEL_LOAD_COLUMNS, row_filter=pc.field("horizon_min") == score_horizon_min)
    lifts = S.compute_instance_lifts(label_rows, horizon_min=score_horizon_min,
                                     side=dcfg.SCORE_SIDE)
    del label_rows                             # free the label load before the heavy null pass

    conn = RS.connect(discovery_db_path)
    TL.ensure_schema(conn)
    try:
        return run_discovery_pass(
            conn, engineered, lifts, price_index, coin_vols,
            feature_ids=E.engineered_feature_ids(), frontier_ns=frontier, now_ns=now_ns,
            score_horizon_min=score_horizon_min, seed=seed, **pass_kw)
    finally:
        conn.close()
