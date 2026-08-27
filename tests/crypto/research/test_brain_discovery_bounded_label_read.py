"""P1 — bounded label read + bounded price-index build (the confirmed discovery-OOM seam).

Measured 2026-08-27 (`data/processed/discovery_oom_observation.md`): the pass read all
1,102,981 label fragments (23.5 min, ~1.28 ms/fragment) and was OOM-killed at t=4070s the
moment the read loop ENDED and `read_snapshots_columnar` ran its final
`pa.concat_tables(merged).combine_chunks()` (`store.py:393`) — RSS +1.23G in the last 30s
window against a 13.0 GiB cap. The full label table must NEVER materialize: the label phase
only ever reduces it to the `lifts` dict.

These tests pin: (1) peak rows resident is bounded by the read batch size and INDEPENDENT of
fragment count; (2) the streamed aggregate is byte-identical to the old read-all-then-concat
path; (3) the same for the price index (the measured +1.1G non-fatal step).
"""
from __future__ import annotations

import pyarrow.compute as pc

from crypto.research.brain import labels as brain_labels
from crypto.research.brain import store
from crypto.research.brain.discovery import runner as RUN
from crypto.research.brain.discovery import scoring as S

_W = 60_000_000_000


def _label_row(sym, i, *, horizon=60, mfe=None, mae=None, valid=True):
    ws = i * _W
    return {"recv_ts_ns": ws + _W, "symbol": sym, "window_start_ns": ws,
            "window_end_ns": ws + _W, "horizon_min": horizon,
            "fwd_return": 0.01 * i,
            "mfe": (0.02 + 0.001 * i) if mfe is None else mfe,
            "mae": (-0.01 - 0.0005 * i) if mae is None else mae,
            "valid": valid}


def _mark_row(sym, i, close, high, low):
    row = {name: 0 for name in store.MARKPRICE_SNAPSHOT_SCHEMA.names}
    ws = i * _W
    row.update(symbol=sym, window_start_ns=ws, window_end_ns=ws + _W, recv_ts_ns=ws + _W,
               mark_close=close, mark_high=high, mark_low=low)
    return row


def _write_fragmented(root, dataset, schema, rows_per_fragment):
    """One `write_snapshots` call per group => one part file per group (many tiny fragments,
    exactly the shape of the live labels store: 1.1M fragments, ~6.6 KB each)."""
    for group in rows_per_fragment:
        store.write_snapshots(str(root), dataset, schema, group)


def _label_fragments(n_fragments, rows_each=3):
    out, i = [], 0
    for f in range(n_fragments):
        sym = f"SYM{f % 4}USDT"
        out.append([_label_row(sym, i + k) for k in range(rows_each)])
        i += rows_each
    return out


# ---------------------------------------------------------------- P1a: bounded label fold

def test_label_fold_peak_rows_bounded_and_independent_of_fragment_count(tmp_path):
    """Peak rows handed to the accumulator is bounded by the read batch size and does NOT
    grow with fragment count. Fails on the old path, which concatenates every fragment into
    one table (peak == total rows)."""
    seen = {}
    for n_frag in (8, 64):
        root = tmp_path / f"store{n_frag}"
        _write_fragmented(root, brain_labels.LABEL_DATASET, brain_labels.LABEL_SCHEMA,
                          _label_fragments(n_frag))
        peak = {"rows": 0}

        def _fold(acc, tbl, _peak=peak):
            _peak["rows"] = max(_peak["rows"], tbl.num_rows)
            acc.append(tbl.num_rows)

        total = store.fold_snapshots_columnar(
            str(root), brain_labels.LABEL_DATASET, fold=_fold, init=list)
        seen[n_frag] = peak["rows"]
        assert sum(total) == n_frag * 3          # every row was still visited exactly once

    assert seen[8] == seen[64], (
        f"peak rows must not grow with fragment count: {seen}")
    assert seen[64] <= store._READ_BATCH_ROWS


def test_streamed_lifts_are_byte_identical_to_concat_path(tmp_path):
    """The streaming aggregate must equal the old read-all-then-concat aggregate EXACTLY —
    same float bits, same key set, same duplicate-key last-wins, same coin baseline."""
    root = tmp_path / "store"
    frags = _label_fragments(12)
    # duplicate key (last-wins) + an excluded null-leg row + an off-horizon + an invalid row
    frags.append([_label_row("SYM0USDT", 0, mfe=0.5, mae=-0.1)])
    frags.append([_label_row("SYM1USDT", 99, mfe=None)])
    frags.append([_label_row("SYM2USDT", 98, horizon=1440)])
    frags.append([_label_row("SYM3USDT", 97, valid=False)])
    _write_fragmented(root, brain_labels.LABEL_DATASET, brain_labels.LABEL_SCHEMA, frags)

    kw = dict(columns=RUN._LABEL_LOAD_COLUMNS, row_filter=pc.field("horizon_min") == 60)
    tbl = store.read_snapshots_columnar(str(root), brain_labels.LABEL_DATASET, **kw)
    expected = S.compute_instance_lifts_columnar(tbl, horizon_min=60, side="long")

    acc = S.InstanceLiftAccumulator(horizon_min=60, side="long")
    store.fold_snapshots_columnar(str(root), brain_labels.LABEL_DATASET,
                                  fold=lambda a, t: a.update(t), init=lambda: acc, **kw)
    streamed = acc.finalize()

    assert streamed == expected
    assert list(streamed.keys()) == list(expected.keys())        # insertion order too
    for k in expected:
        assert streamed[k].hex() == expected[k].hex()            # exact float bits


def test_label_load_path_never_reads_the_whole_label_table(monkeypatch, tmp_path):
    """`_load_lifts` must reach the same lifts WITHOUT ever calling the whole-table reader
    for the labels dataset — that call is the measured kill site (`store.py:393`)."""
    root = tmp_path / "store"
    _write_fragmented(root, brain_labels.LABEL_DATASET, brain_labels.LABEL_SCHEMA,
                      _label_fragments(6))
    kw = dict(columns=RUN._LABEL_LOAD_COLUMNS, row_filter=pc.field("horizon_min") == 60)
    expected = S.compute_instance_lifts_columnar(
        store.read_snapshots_columnar(str(root), brain_labels.LABEL_DATASET, **kw),
        horizon_min=60, side="long")

    seen = []
    real = store.read_snapshots_columnar

    def _spy(r, d, *a, **k):
        seen.append(d)
        return real(r, d, *a, **k)

    monkeypatch.setattr(store, "read_snapshots_columnar", _spy)
    got = RUN._load_lifts(str(root), label_floor=0, score_horizon_min=60)

    assert got == expected
    assert brain_labels.LABEL_DATASET not in seen, (
        f"labels must not be read wholesale; saw {seen}")


def test_streamed_lifts_share_one_symbol_string_across_batches(tmp_path):
    """Symbol strings must be shared ACROSS batches.

    The store is symbol-partitioned (one symbol per fragment), so each batch's
    `dictionary_encode()` mints a fresh `str` per symbol. Naively keeping those puts one
    `str` per FRAGMENT into the lift keys (~1.1M on the live labels store) instead of one
    per SYMBOL (~915) — a memory regression inside the very path being bounded.
    """
    root = tmp_path / "store"
    # 30 fragments over only 3 symbols => 30 batches, 3 distinct symbol values
    frags = [[_label_row(f"SYM{f % 3}USDT", f)] for f in range(30)]
    _write_fragmented(root, brain_labels.LABEL_DATASET, brain_labels.LABEL_SCHEMA, frags)

    acc = store.fold_snapshots_columnar(
        str(root), brain_labels.LABEL_DATASET,
        columns=RUN._LABEL_LOAD_COLUMNS, row_filter=pc.field("horizon_min") == 60,
        fold=lambda a, t: a.update(t),
        init=lambda: S.InstanceLiftAccumulator(horizon_min=60, side="long"))
    lifts = acc.finalize()

    symbols = {k[0] for k in lifts}
    identities = {id(k[0]) for k in lifts}
    assert len(symbols) == 3
    assert len(identities) == len(symbols), (
        f"{len(identities)} distinct str objects for {len(symbols)} symbols — "
        "symbols are not interned across batches")


# ---------------------------------------------------------------- P1b: bounded price index

def test_streamed_price_index_is_identical_to_whole_table_build(tmp_path):
    """The price index built by folding batches equals the one built from the whole table
    (the 5 simultaneous `to_pylist()` transients cost a measured +1.1G step)."""
    root = tmp_path / "mp"
    frags = [[_mark_row("BTCUSDT", i, 100.0 + i, 101.0 + i, 99.0 + i)] for i in range(10)]
    frags += [[_mark_row("ETHUSDT", i, 50.0 + i, 51.0 + i, 49.0 + i)] for i in range(10)]
    _write_fragmented(root, "markprice", store.MARKPRICE_SNAPSHOT_SCHEMA, frags)

    cols = ["symbol", "window_start_ns", "mark_close", "mark_high", "mark_low"]
    tbl = store.read_snapshots_columnar(str(root), "markprice", columns=cols)
    expected = RUN.build_price_index_columnar(tbl)

    acc = RUN.PriceIndexAccumulator()
    store.fold_snapshots_columnar(str(root), "markprice", columns=cols,
                                  fold=lambda a, t: a.update(t), init=lambda: acc)
    assert acc.finalize() == expected


def test_price_index_accumulator_peak_rows_bounded(tmp_path):
    """The accumulator sees one batch at a time, never the whole markprice table."""
    root = tmp_path / "mp2"
    _write_fragmented(root, "markprice", store.MARKPRICE_SNAPSHOT_SCHEMA,
                      [[_mark_row("BTCUSDT", i, 100.0 + i, 101.0 + i, 99.0 + i)]
                       for i in range(40)])
    peak = {"rows": 0}

    def _fold(a, t):
        peak["rows"] = max(peak["rows"], t.num_rows)
        a.update(t)

    store.fold_snapshots_columnar(str(root), "markprice",
                                  columns=["symbol", "window_start_ns", "mark_close",
                                           "mark_high", "mark_low"],
                                  fold=_fold, init=RUN.PriceIndexAccumulator)
    assert peak["rows"] <= store._READ_BATCH_ROWS
