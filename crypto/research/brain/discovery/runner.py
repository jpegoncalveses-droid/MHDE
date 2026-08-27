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

import hashlib
import logging
import statistics
from typing import Mapping, Optional, Sequence

import numpy as np
import pyarrow.compute as pc

from crypto.research.brain import labels as brain_labels
from crypto.research.brain import registry as brain_registry
from crypto.research.brain import store as brain_store
from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import admission as AD
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

    RETAINED, NOT DEAD: production streams the markprice read through
    :class:`PriceIndexAccumulator`; this version and ``build_price_index_columnar`` are kept
    as its equivalence ORACLES (test_brain_store_columnar,
    test_brain_discovery_bounded_label_read)."""
    idx: dict = {}
    for s in markprice_rows:
        idx.setdefault(s["symbol"], {})[int(s["window_start_ns"])] = (
            s["mark_close"], s["mark_high"], s["mark_low"])
    return idx


def build_price_index_columnar(markprice_table) -> dict:
    """``build_price_index`` from a columnar markprice ``pyarrow.Table`` — same output
    (``to_pylist`` maps a parquet NULL to None exactly as the dict path did).

    ORACLE ONLY as of the streaming fix: production uses :class:`PriceIndexAccumulator`,
    which folds the same rows batch-by-batch instead of materialising the whole table."""
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


class PriceIndexAccumulator:
    """STREAMING twin of :func:`build_price_index_columnar` — folds markprice batch tables.

    The whole-table build holds FIVE simultaneous ``to_pylist()`` transients beside the
    growing index; the 2026-08-27 instrumented run measured that as a +1.1 G step
    (10.12 G -> 11.26 G peak). Non-fatal today, but it is pure headroom: folding per batch
    bounds those transients by batch size. Output is identical (same rows, same order, same
    last-wins on a duplicate ``(symbol, window)``).
    """

    __slots__ = ("_idx",)

    def __init__(self):
        self._idx: dict = {}

    def update(self, markprice_table) -> None:
        if markprice_table is None or markprice_table.num_rows == 0:
            return
        syms = markprice_table.column("symbol").to_pylist()
        wins = markprice_table.column("window_start_ns").to_pylist()
        close = markprice_table.column("mark_close").to_pylist()
        high = markprice_table.column("mark_high").to_pylist()
        low = markprice_table.column("mark_low").to_pylist()
        idx = self._idx
        for sym, w, c, h, l in zip(syms, wins, close, high, low):
            idx.setdefault(sym, {})[int(w)] = (c, h, l)

    def finalize(self) -> dict:
        out = self._idx
        self._idx = {}
        return out


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


def _sampled_fires(entry_rule, engineered, *, max_instances: Optional[int], seed: int,
                   salt: str = "") -> list:
    """The rule's fired instances in CANONICAL (sorted) order, sampled down to at most
    ``max_instances`` when it fires more broadly.

    The sample is DETERMINISTIC per rule and attempt-stable: the RNG is seeded from the
    rule's ``canonical_id`` digest (xor the pass seed), NOT from process state or attempt
    identity — so a resumed/re-run pass discovers the same exit for the same rule over the
    same population, and the selection is independent of ``R.fires``'s set-iteration order
    (which is PYTHONHASHSEED-dependent; the sort also makes the UNSAMPLED path's
    continuation order reproducible, which it previously was not). Rules firing
    ``<= max_instances`` are returned whole — byte-identical continuations downstream.

    ``salt`` makes an INDEPENDENT draw for a different consumer of the same rule at the same
    cap. Stage-2 (exit discovery) and stage-4 (trade logging) share this sampler and the same
    cap, so an unsalted stage-4 would redraw stage-2's EXACT sample whenever the firing set is
    unchanged — making the trade log a 100% in-sample echo of the instances the exit was
    fitted on. The empty default leaves the stage-2 digest byte-identical, preserving its
    attempt-stability guarantee."""
    fired = sorted(R.fires(entry_rule, engineered))
    if max_instances is None or len(fired) <= max_instances:
        return fired
    key = entry_rule.canonical_id if not salt else f"{entry_rule.canonical_id}\x00{salt}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    rule_seed = int.from_bytes(digest[:8], "big") ^ (seed & 0xFFFF_FFFF_FFFF_FFFF)
    idx = np.random.default_rng(rule_seed).choice(len(fired), size=max_instances,
                                                  replace=False)
    idx.sort()
    return [fired[i] for i in idx]


def _entry_continuations(entry_rule, engineered, price_index, coin_vols, *, max_cap, window_ns,
                         only_settled_at: Optional[int] = None,
                         max_instances: Optional[int] = None, seed: int = 0,
                         salt: str = ""):
    """Build continuations + per-instance vols for an entry's firing instances."""
    conts: dict = {}
    vols: dict = {}
    for k in _sampled_fires(entry_rule, engineered, max_instances=max_instances, seed=seed,
                            salt=salt):
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


#: Sampler salt that makes stage-4's draw independent of stage-2's at the same cap.
_STAGE4_SAMPLE_SALT = "stage4-tradelog"


def _stage4_continuations(entry_rule, engineered, price_index, coin_vols, *, max_cap,
                          window_ns, seed: int = 0,
                          max_instances: Optional[int] = dcfg.TRADELOG_MAX_INSTANCES):
    """Stage-4 (trade-logging) continuations, BOUNDED by ``TRADELOG_MAX_INSTANCES``.

    This was the last unsampled continuation build in the pass: stage-2 has been sampled
    since Option S, but stage-4 ran the full firing set for every PROMOTED rule. With the
    promoted set unbounded by design (growth watch, trigger ~500) that is the tail term the
    RCA names as the relapse risk once P1 makes the tail reachable again. Same deterministic
    rule-seeded sampler as stage-2, so the selection is reproducible.
    """
    return _entry_continuations(entry_rule, engineered, price_index, coin_vols,
                                max_cap=max_cap, window_ns=window_ns,
                                max_instances=max_instances, seed=seed,
                                salt=_STAGE4_SAMPLE_SALT)


logger = logging.getLogger("mhde.crypto.brain.discovery")


def _expiry_active(*, frontier_ns: int, resume_ns: int) -> bool:
    """The KI-166 gap-rollout hold: pace-expiry runs only once the frontier has
    reached ``resume_ns`` (0 = no hold). Frontier-relative, never wall-clock."""
    return frontier_ns >= resume_ns


def run_discovery_pass(conn, engineered, lifts, price_index, coin_vols, *, feature_ids,
                       frontier_ns, now_ns, score_horizon_min=dcfg.SCORE_HORIZON_MIN,
                       n_bins=dcfg.QUANTILE_BINS, n_permutations=dcfg.N_PERMUTATIONS,
                       null_quantile=dcfg.NULL_QUANTILE, min_firing=dcfg.MIN_FIRING_INSTANCES,
                       max_depth=dcfg.MAX_DEPTH, beam_width=dcfg.BEAM_WIDTH,
                       exit_max_instances=dcfg.EXIT_DISCOVERY_MAX_INSTANCES,
                       tradelog_max_instances=dcfg.TRADELOG_MAX_INSTANCES,
                       m=dcfg.CONFIRM_M, z=dcfg.CONFIRM_Z,
                       confirm_hysteresis=dcfg.CONFIRM_DEMOTE_HYSTERESIS,
                       expire_pace_factor=dcfg.EXPIRE_PACE_FACTOR,
                       expire_resume_frontier_ns=dcfg.EXPIRE_RESUME_FRONTIER_NS,
                       admit_max_per_family=dcfg.ADMIT_MAX_PER_FAMILY,
                       exit_grid=None, window_ns=dcfg.WINDOW_NS, seed=0) -> dict:
    """One discovery pass over already-loaded data. Returns a summary dict."""
    exit_grid = exit_grid if exit_grid is not None else X.build_exit_grid()
    max_cap = max(er.time_cap_min for er in exit_grid)

    # 1. Stage 1: generate -> score -> null.
    survivors, diagnostics = S.discover_entries(
        engineered, lifts, feature_ids=feature_ids, n_bins=n_bins,
        n_permutations=n_permutations, null_quantile=null_quantile, min_firing=min_firing,
        max_depth=max_depth, beam_width=beam_width, seed=seed)
    # The run row is recorded FIRST so its run_id stamps every fresh insert's cohort
    # (minted_run_id, S1/ADR-043) — cohort identity is first-class, not a timestamp
    # join. Re-run safety unchanged: upserts dedupe on rule_id.
    run_id = RS.record_run(conn, started_at_ns=now_ns, frontier_ns=frontier_ns,
                           score_horizon_min=score_horizon_min, funnel=diagnostics,
                           n_survivors=len(survivors))
    for er in survivors:
        breadth = R.fires_breadth(er.rule, engineered)
        RS.upsert_entry(conn, er, score_horizon_min=score_horizon_min, breadth=breadth,
                        discovery_window_ns=frontier_ns, now_ns=now_ns,
                        minted_run_id=run_id)

    # 1b. Family-level admission (ADR-043): gate the DISCOVERED->CONFIRMING advance —
    # resolvability floor (n_fires >= M), one per family per pass, quota k concurrent
    # seats, in-sample margin selection; losers are BENCHED (never walked). This is
    # THE bound on the confirming set (the walk's cost is linear in it).
    adm = AD.run_admission(conn, m=m, k=admit_max_per_family, now_ns=now_ns)

    # 2. Forward confirmation: advance discovered->confirming->promoted|rejected.
    conf = CF.run_confirmation(conn, engineered, lifts, m=m, z=z,
                               hysteresis=confirm_hysteresis, now_ns=now_ns)

    # 2b. Pace-collapse retention (ADR-042): AFTER confirmation so this pass's recount is
    # what is judged; before stage-2 so expired rules draw no exit work. Terminal but
    # retained — family/cohort provenance survives for the graduation bar. Runs on the
    # EVIDENCE clock (frontier, F4/KI-166) and only once the frontier has passed the
    # gap-rollout hold (otherwise the post-gap frontier delta overcounts opportunity
    # for pre-gap mints and re-manufactures stall artifacts).
    if _expiry_active(frontier_ns=frontier_ns, resume_ns=expire_resume_frontier_ns):
        n_expired = RS.expire_slow_resolvers(
            conn, now_ns=now_ns, frontier_ns=frontier_ns, m=m,
            hysteresis=confirm_hysteresis,
            history_ns=dcfg.DISCOVERY_HISTORY_NS, opportunity_floor=m,
            pace_factor=expire_pace_factor)
        # S8 mature-seat eviction (ADR-042 amendment): drains the dead zone
        # (mature, never-promoted, below the resolving band) that pace-expiry
        # structurally cannot, so seats always turn over. Same evidence clock,
        # same gap-rollout hold.
        n_evicted = RS.evict_mature_unresolved(
            conn, now_ns=now_ns, frontier_ns=frontier_ns, m=m,
            hysteresis=confirm_hysteresis, history_ns=dcfg.DISCOVERY_HISTORY_NS)
    else:
        n_expired = 0
        n_evicted = 0
        logger.info("brain discovery: pace-expiry HELD — frontier %d below the KI-166 "
                    "gap-rollout resume threshold %d", frontier_ns,
                    expire_resume_frontier_ns)

    # 3. Stage 2: exit discovery for confirming/promoted entries that still lack an exit.
    # FAMILY-DEDUPED: rule identity re-mints ~1,000 near-duplicates per pass (thresholds
    # shift on the moving quantile grid) and exit work per identity was the pass's
    # dominant growth term. A new family member inherits the family's discovered exit
    # (deterministic donor) instead of re-running the exit search.
    exits_found = 0
    exits_inherited = 0
    for state in (RS.CONFIRMING, RS.PROMOTED):
        for row in RS.list_rules(conn, state=state):
            if row["exit_def"] is not None:
                continue
            donor = RS.family_exit_donor(conn, row["family_key"], exclude=row["rule_id"])
            if donor is not None:
                RS.inherit_exit(conn, row["rule_id"], donor, now_ns=now_ns)
                exits_inherited += 1
                continue
            entry_rule = RS.deserialize_rule(row["entry_def"])
            # Stage-2 samples <=exit_max_instances fired instances per rule (deterministic,
            # rule-seeded) — the unbounded continuation build was the +2G that pushed six
            # gate attempts into the host OOM kill zone. Stage-4 is bounded too (P2).
            conts, vols = _entry_continuations(entry_rule, engineered, price_index, coin_vols,
                                               max_cap=max_cap, window_ns=window_ns,
                                               only_settled_at=frontier_ns,
                                               max_instances=exit_max_instances, seed=seed)
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
        conts, vols = _stage4_continuations(entry_rule, engineered, price_index, coin_vols,
                                            max_cap=exit_rule.time_cap_min,
                                            window_ns=window_ns, seed=seed,
                                            max_instances=tradelog_max_instances)
        trades = TL.build_trades(row["rule_id"], exit_rule, list(conts), conts, vols,
                                 window_ns=window_ns, now_ns=now_ns)
        trades_logged += TL.record_trades(conn, trades, exit_def=row["exit_def"], now_ns=now_ns)

    return {"survivors": len(survivors), "diagnostics": diagnostics, "exits_found": exits_found,
            "exits_inherited": exits_inherited, "expired": n_expired, "evicted": n_evicted, "admitted": adm["admitted"], "benched": adm["benched"],
            "trades_logged": trades_logged, **conf}


def _load_price_index(store_root: str, *, primitive_floor: int) -> dict:
    """Markprice window -> price index, STREAMED (never the whole markprice table at once)."""
    acc = brain_store.fold_snapshots_columnar(
        store_root, brain_labels.MARKPRICE_DATASET,
        after_recv_ts_ns=primitive_floor, window_end_floor_ns=primitive_floor,
        columns=["symbol", "window_start_ns", "mark_close", "mark_high", "mark_low",
                 "window_end_ns"],
        fold=lambda a, t: a.update(t), init=PriceIndexAccumulator)
    return acc.finalize()


def _load_lifts(label_store_root: str, *, label_floor: int, score_horizon_min: int) -> dict:
    """Label window -> instance lifts, STREAMED.

    The label phase's ONLY output is this dict, so the full label table never needs to exist.
    Assembling it is the measured OOM kill site (2026-08-27: 1,102,981 fragments read over
    23.5 min, killed the instant the read loop ended and the concat began). Peak is now one
    batch + the accumulator, independent of fragment count.
    """
    acc = brain_store.fold_snapshots_columnar(
        label_store_root, brain_labels.LABEL_DATASET,
        after_recv_ts_ns=label_floor, window_end_floor_ns=label_floor,
        columns=_LABEL_LOAD_COLUMNS,
        row_filter=pc.field("horizon_min") == score_horizon_min,
        fold=lambda a, t: a.update(t),
        init=lambda: S.InstanceLiftAccumulator(horizon_min=score_horizon_min,
                                               side=dcfg.SCORE_SIDE))
    return acc.finalize()


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
    # Price index STREAMED (P1b): the whole-table build's five simultaneous to_pylist()
    # transients were a measured +1.1G step at the highest-residency moment of the load.
    price_index = _load_price_index(store_root, primitive_floor=primitive_floor)
    coin_vols = coin_volatilities(price_index)

    # Labels read COLUMNAR too (the last list-of-dicts load path): ~6M rows as Python dicts
    # was a ~3G transient — the measured OOM term of the 2026-08-11 gate — vs ~0.3G as a
    # projected columnar table. Same selection (identical reader semantics), byte-identical
    # lifts (compute_instance_lifts_columnar oracle test).
    lifts = _load_lifts(label_store_root, label_floor=label_floor,
                        score_horizon_min=score_horizon_min)

    conn = RS.connect(discovery_db_path)
    TL.ensure_schema(conn)
    try:
        return run_discovery_pass(
            conn, engineered, lifts, price_index, coin_vols,
            feature_ids=E.engineered_feature_ids(), frontier_ns=frontier, now_ns=now_ns,
            score_horizon_min=score_horizon_min, seed=seed, **pass_kw)
    finally:
        conn.close()
