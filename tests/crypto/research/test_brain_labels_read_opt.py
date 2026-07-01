"""Fix 2: label-read optimization — bound the markprice reads ``labels.run_once`` does so the
first UTC-midnight crossing can't materialize all ~812 symbols × multi-day toward the 2G cap.

(a) symbol DISCOVERY (``symbols=None``) enumerates the markprice ``symbol=`` dirs
    (``store.list_symbols``, a directory listing) instead of reading the WHOLE store to harvest
    the symbol set.
(b) the per-symbol markprice read pushes a ``window_end >= floor`` filter into
    ``store.read_snapshots``, so below-floor windows (every horizon already settled + written)
    are never materialized.

Both keep the emitted labels BYTE-IDENTICAL: a window with ``window_end < floor`` has every
horizon settled+seen (no new label) and is never a forward window of any still-settling entry
(those sit at ``window_end >= floor``, so their forward windows are ``> floor``) — so dropping it
changes no return / MFE / MAE / validity. Pinned here AND by the whole existing labels suite
(test_brain_labels.py) staying green.
"""
from __future__ import annotations

from crypto.research.brain import labels
from crypto.research.brain import store as brain_store
from crypto.research.brain import registry

_MIN_NS = 60_000_000_000
_MS_TO_NS = 1_000_000
_DAY_NS = 86_400 * 1_000_000_000                  # the 1-day margin the date-prune + pushdown share
_BASE_NS = 1_781_640_000_000 * _MS_TO_NS          # 2026-06-16 20:00:00 UTC (clean grid origin)


def _w(k: int) -> int:
    return _BASE_NS + k * _MIN_NS


def _mp(symbol, k, *, close):
    return {
        "recv_ts_ns": _w(k) + 59 * _MS_TO_NS, "symbol": symbol,
        "window_start_ns": _w(k), "window_end_ns": _w(k + 1),
        "mark_open": close, "mark_high": close, "mark_low": close, "mark_close": close,
        "index_open": close, "index_high": close, "index_low": close, "index_close": close,
        "settle_open": close, "settle_high": close, "settle_low": close, "settle_close": close,
        "funding_last": 0.0, "funding_min": 0.0, "funding_max": 0.0,
        "next_funding_time_last": 0, "update_count": 1,
    }


def _write_markprice(root, snaps):
    brain_store.write_snapshots(str(root), "markprice", brain_store.MARKPRICE_SNAPSHOT_SCHEMA, snaps)


def _seed_frontier(registry_path, frontier_end_ns):
    conn = registry.connect(str(registry_path))
    registry.advance(
        conn, "markprice", new_recv_ts_ns=frontier_end_ns,
        bookkeeping=[{
            "dataset": "markprice", "symbol": "AAAUSDT",
            "window_start_ns": frontier_end_ns - _MIN_NS, "window_end_ns": frontier_end_ns,
            "recv_ts_ns": frontier_end_ns, "n_events": 1,
        }],
        now_ns=frontier_end_ns)
    conn.close()


def _bykey(rows):
    return {(r["symbol"], r["window_start_ns"], r["horizon_min"]):
            (r["fwd_return"], r["mfe"], r["mae"], r["valid"]) for r in rows}


# -- (store) list_symbols: a directory listing, UTF-8 / digit-leading safe, sorted ------

def test_list_symbols_returns_sorted_symbol_partition_names(tmp_path):
    _write_markprice(tmp_path / "store", [_mp("ZZZUSDT", 0, close=1.0),
                                          _mp("AAAUSDT", 0, close=1.0),
                                          _mp("1000XUSDT", 0, close=1.0)])
    assert brain_store.list_symbols(str(tmp_path / "store"), "markprice") == \
        ["1000XUSDT", "AAAUSDT", "ZZZUSDT"]


def test_list_symbols_missing_dataset_is_empty(tmp_path):
    assert brain_store.list_symbols(str(tmp_path / "store"), "markprice") == []


# -- (store) window_end >= floor pushdown drops only below-floor rows -------------------

def test_read_snapshots_window_end_floor_drops_below_floor(tmp_path):
    _write_markprice(tmp_path / "store", [_mp("AAAUSDT", k, close=100.0 + k) for k in range(6)])
    floor = _w(3)                                  # keep window_end >= _w(3): windows k=2..5
    rows = brain_store.read_snapshots(str(tmp_path / "store"), "markprice", "AAAUSDT",
                                      window_end_floor_ns=floor)
    assert sorted(r["window_start_ns"] for r in rows) == [_w(2), _w(3), _w(4), _w(5)]


def test_read_snapshots_window_end_floor_zero_returns_all(tmp_path):
    _write_markprice(tmp_path / "store", [_mp("AAAUSDT", k, close=100.0 + k) for k in range(6)])
    # default (and an explicit 0 floor) is a no-op — every existing caller is unaffected.
    assert len(brain_store.read_snapshots(str(tmp_path / "store"), "markprice", "AAAUSDT")) == 6
    assert len(brain_store.read_snapshots(str(tmp_path / "store"), "markprice", "AAAUSDT",
                                          window_end_floor_ns=0)) == 6


# -- (labels) discovery uses list_symbols, NOT a whole-store read ----------------------

def test_discovery_enumerates_symbol_dirs_not_whole_store(tmp_path, monkeypatch):
    _write_markprice(tmp_path / "store",
                     [_mp("AAAUSDT", k, close=100.0 + k) for k in range(12)] +
                     [_mp("BBBUSDT", k, close=100.0 + k) for k in range(12)])
    _seed_frontier(tmp_path / "reg.db", _w(8))
    seen_symbol_args = []
    orig = brain_store.read_snapshots

    def spy(root, dataset, symbol=None, **kw):
        seen_symbol_args.append(symbol)
        return orig(root, dataset, symbol, **kw)

    monkeypatch.setattr(labels.store, "read_snapshots", spy)
    labels.run_once(store_root=str(tmp_path / "store"), capture_root=str(tmp_path / "capture"),
                    registry_path=str(tmp_path / "reg.db"), horizons_min=[5])  # symbols=None
    assert None not in seen_symbol_args, \
        "discovery must enumerate symbol= dirs, never a whole-store (symbol=None) read"
    assert set(seen_symbol_args) == {"AAAUSDT", "BBBUSDT"}


# -- (labels) symbols=None yields BYTE-IDENTICAL labels to an explicit list -------------

def test_symbols_none_yields_byte_identical_labels_to_explicit_list(tmp_path):
    snaps = ([_mp("AAAUSDT", k, close=100.0 + k) for k in range(12)] +
             [_mp("BBBUSDT", k, close=200.0 + k) for k in range(12)])
    _write_markprice(tmp_path / "s1", snaps); _seed_frontier(tmp_path / "r1", _w(10))
    out_explicit = labels.run_once(
        store_root=str(tmp_path / "s1"), capture_root=str(tmp_path / "cap"),
        registry_path=str(tmp_path / "r1"), horizons_min=[5, 15], symbols=["AAAUSDT", "BBBUSDT"])
    _write_markprice(tmp_path / "s2", snaps); _seed_frontier(tmp_path / "r2", _w(10))
    out_discovery = labels.run_once(
        store_root=str(tmp_path / "s2"), capture_root=str(tmp_path / "cap"),
        registry_path=str(tmp_path / "r2"), horizons_min=[5, 15])  # symbols=None
    assert _bykey(out_explicit) == _bykey(out_discovery)
    assert len(out_explicit) == len(out_discovery)


# -- (labels) the per-symbol read is window_end-floor bounded (pushdown active) ---------

def test_per_symbol_read_passes_window_end_floor(tmp_path, monkeypatch):
    _write_markprice(tmp_path / "store", [_mp("AAAUSDT", k, close=100.0 + k) for k in range(4, 11)])
    _seed_frontier(tmp_path / "reg.db", _w(10))    # frontier _w(10); horizons=[5] -> floor _w(5)
    floors = []
    orig = brain_store.read_snapshots

    def spy(root, dataset, symbol=None, **kw):
        if symbol is not None:
            floors.append(kw.get("window_end_floor_ns"))
        return orig(root, dataset, symbol, **kw)

    monkeypatch.setattr(labels.store, "read_snapshots", spy)
    labels.run_once(store_root=str(tmp_path / "store"), capture_root=str(tmp_path / "capture"),
                    registry_path=str(tmp_path / "reg.db"), horizons_min=[5], symbols=["AAAUSDT"])
    # floor = (frontier - maxH) - the shared 1-day margin, so it never drops a date-prune-kept window.
    assert floors == [_w(10) - 5 * _MIN_NS - _DAY_NS], \
        "per-symbol read pushes window_end >= (frontier-maxH) minus the date-prune margin"


def test_bound_reads_off_disables_the_window_end_floor(tmp_path, monkeypatch):
    _write_markprice(tmp_path / "store", [_mp("AAAUSDT", k, close=100.0 + k) for k in range(4, 11)])
    _seed_frontier(tmp_path / "reg.db", _w(10))
    floors = []
    orig = brain_store.read_snapshots

    def spy(root, dataset, symbol=None, **kw):
        if symbol is not None:
            floors.append(kw.get("window_end_floor_ns"))
        return orig(root, dataset, symbol, **kw)

    monkeypatch.setattr(labels.store, "read_snapshots", spy)
    labels.run_once(store_root=str(tmp_path / "store"), capture_root=str(tmp_path / "capture"),
                    registry_path=str(tmp_path / "reg.db"), horizons_min=[5], symbols=["AAAUSDT"],
                    bound_reads=False)
    assert floors == [0], "bound_reads=False restores the unbounded read (no floor pushdown)"
