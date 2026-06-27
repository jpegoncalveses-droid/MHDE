"""Reader SCOPED dataset construction — the structural fix for the tick-loop stall.

THE BUG: ``_read_dataset_rows`` built ``ds.dataset(base, partitioning="hive")`` over the
WHOLE ``symbol=*/date=*`` tree and materialized the full in-memory fragment list BEFORE
any symbol/date/recv filter applied. Cost scaled with TOTAL fragment count, not the
batch's — klines (226k fragments, never date-pruned) ground at the 2G cgroup ceiling;
depth_state (~3M fragments) std::bad_alloc'd at construction. ``run_pass`` rebuilt this
whole tree once per 25-symbol batch (22x/pass). The PR #74 window bound, the row filter,
and ``to_pylist`` all act DOWNSTREAM of construction and cannot touch it.

THE FIX (a batched read, ``symbols`` given): construct the dataset over ONLY the batch's
``symbol=/date=`` partition directories, so construction scales with the batch's fragment
count, not the whole dataset's. The ``symbols=None`` full-universe path is unchanged
(deliberate whole-tree read; date-pruning preserved — see test_brain_reader_date_prune).

Correctness is pinned by the existing date-prune suite (T1..T7) PLUS the equivalence and
missing-partition guards here.
"""
from __future__ import annotations

import logging
import pathlib

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader

_MS_TO_NS = 1_000_000
_DAY_MS = 86_400_000
# 2026-06-16 00:00:00 UTC, in ms. day n -> + n days.
_D0_MS = 1_781_568_000_000


def _day_ms(n: int, *, h: int = 0, m: int = 0, s: int = 0) -> int:
    return _D0_MS + n * _DAY_MS + (h * 3600 + m * 60 + s) * 1000


def _write_bt(root, symbol, e_ms, recv_ns=None) -> int:
    """Append+flush one bookTicker row. ``E`` (ms) drives the date= partition; recv_ns
    (defaults to E in ns) drives the recv window. Returns recv_ns."""
    recv_ns = e_ms * _MS_TO_NS if recv_ns is None else recv_ns
    w = capture_store.bookticker_writer(str(root))
    w.append({"recv_ts_ns": recv_ns, "e": "bookTicker", "u": 1, "s": symbol,
              "b": "100.0", "B": "1.0", "a": "101.0", "A": "1.0", "T": e_ms, "E": e_ms})
    w.flush_all()
    return recv_ns


def _spy_dataset(monkeypatch):
    """Wrap reader.ds.dataset to RECORD the source argument and still run the real read."""
    calls = []
    orig = reader.ds.dataset

    def spy(source, *a, **k):
        calls.append(source)
        return orig(source, *a, **k)

    monkeypatch.setattr(reader.ds, "dataset", spy)
    return calls


# --- RED DRIVER: a batched read constructs over only the batch's partition dirs -------

def test_scoped_construction_passes_only_batch_partition_dirs(tmp_path, monkeypatch):
    sym_a, sym_b = "AAAUSDT", "BBBUSDT"
    # sym_a: a cross-midnight in-window row (E on 06-19, recv on 06-20), a plain 06-20
    # row, and an out-of-range 06-16 row (date-pruned by the 1-day-margin floor 06-19).
    _write_bt(tmp_path, sym_a, _day_ms(3, h=23, m=59), recv_ns=_day_ms(4, s=1) * _MS_TO_NS)
    _write_bt(tmp_path, sym_a, _day_ms(4, h=10))
    _write_bt(tmp_path, sym_a, _day_ms(0, h=12))             # 06-16 — pruned
    _write_bt(tmp_path, sym_b, _day_ms(4, h=10))             # other symbol — out of batch
    cursor = _day_ms(4) * _MS_TO_NS                          # 06-20 00:00 -> lower_date 06-19

    calls = _spy_dataset(monkeypatch)
    reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym_a])

    assert len(calls) == 1, "exactly one dataset construction per batched read"
    src = calls[0]
    assert isinstance(src, list), \
        f"scoped construction must pass a LIST of partition dirs, got {type(src).__name__} (whole-tree)"
    paths = [str(p) for p in src]
    assert all(f"symbol={sym_a}" in p for p in paths), f"only the batch symbol's dirs: {paths}"
    assert not any(f"symbol={sym_b}" in p for p in paths), f"never another symbol's dirs: {paths}"
    assert not any("date=2026-06-16" in p for p in paths), f"date floor must prune 06-16: {paths}"
    assert {p.split("date=")[1][:10] for p in paths} == {"2026-06-19", "2026-06-20"}, \
        f"exactly the in-range dates: {paths}"


# --- GUARD: scoped rows == the recv>cursor oracle (no dropped / extra / double rows) ---

def test_scoped_read_matches_recv_window_oracle(tmp_path):
    sym = "AAAUSDT"
    cross = _write_bt(tmp_path, sym, _day_ms(3, h=23, m=59), recv_ns=_day_ms(4, s=1) * _MS_TO_NS)
    plain = _write_bt(tmp_path, sym, _day_ms(4, h=10))
    _write_bt(tmp_path, sym, _day_ms(0, h=12))               # 06-16, recv < cursor -> out
    cursor = _day_ms(4) * _MS_TO_NS
    rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    assert sorted(r["recv_ts_ns"] for r in rows) == sorted([cross, plain])


# --- GUARD: a missing symbol partition dir is skipped, not an error -------------------

def test_missing_symbol_partition_is_skipped(tmp_path):
    sym = "AAAUSDT"
    r = _write_bt(tmp_path, sym, _day_ms(4, h=10))
    cursor = _day_ms(4) * _MS_TO_NS - 1
    # NOPEUSDT has no partition dir at all; an empty in-window batch member must not error.
    rows = reader.read_new_bookticker(
        str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym, "NOPEUSDT"])
    assert [x["recv_ts_ns"] for x in rows] == [r]


def test_all_missing_partitions_returns_empty(tmp_path):
    _write_bt(tmp_path, "AAAUSDT", _day_ms(4, h=10))
    cursor = _day_ms(4) * _MS_TO_NS - 1
    rows = reader.read_new_bookticker(
        str(tmp_path), after_recv_ts_ns=cursor, symbols=["NOPEUSDT", "ZZZUSDT"])
    assert rows == []


# --- GUARD: the symbols=None full-universe path stays whole-tree (date-prune preserved) -

def test_symbols_none_keeps_whole_tree_construction(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    _write_bt(tmp_path, sym, _day_ms(4, h=10))
    cursor = _day_ms(4) * _MS_TO_NS - 1
    calls = _spy_dataset(monkeypatch)
    reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor)   # symbols=None
    assert len(calls) == 1
    assert isinstance(calls[0], str), \
        "symbols=None must keep the whole-tree (str base) construction — the deliberate full read"


# --- HARDENING: a corrupt fragment that sorts FIRST in the scoped list must not crash ---
# (real capture corrupt parts are part-<uuid>.parquet — the lex-first can be the corrupt one,
# so the scoped file-list must not let it crash ds.dataset() schema inference.)

def _drop_corrupt_sorting_first(root, dataset, symbol, date_str):
    """A 438-byte truncated parquet named to sort BEFORE any real part-<uuid> file (a uuid4
    hex is never all-zeros), so it becomes the head of the scoped file list."""
    d = pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date_str}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "part-00000000000000000000000000000000.parquet").write_bytes(b"PAR1" + b"\x00" * 434)


def test_corrupt_lead_fragment_in_scoped_partition_is_skipped_not_crashed(tmp_path, caplog):
    sym = "AAAUSDT"
    r = _write_bt(tmp_path, sym, _day_ms(4, h=10))               # good row (real part-<uuid>)
    _drop_corrupt_sorting_first(tmp_path, "bookTicker", sym, "2026-06-20")   # sorts FIRST
    cursor = _day_ms(4) * _MS_TO_NS - 1
    with caplog.at_level(logging.WARNING):
        rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    assert [x["recv_ts_ns"] for x in rows] == [r], "good row returned; the corrupt lead must not crash the read"
    assert any("part-00000000000000000000000000000000" in rec.getMessage() for rec in caplog.records), \
        "the corrupt lead fragment is skipped + logged (never silently dropped, never crashing)"


def test_only_corrupt_fragment_returns_empty_not_crash(tmp_path):
    sym = "AAAUSDT"
    _drop_corrupt_sorting_first(tmp_path, "bookTicker", sym, "2026-06-20")   # the ONLY file
    cursor = _day_ms(4) * _MS_TO_NS - 1
    rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    assert rows == []                                           # no readable file -> empty, no crash


# --- a duplicate symbol in the batch must not double-count (old isin() was set membership) ---

def test_duplicate_symbol_does_not_double_count(tmp_path):
    sym = "AAAUSDT"
    r = _write_bt(tmp_path, sym, _day_ms(4, h=10))
    cursor = _day_ms(4) * _MS_TO_NS - 1
    rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym, sym])
    assert [x["recv_ts_ns"] for x in rows] == [r], "a repeated symbol must read its file once, not twice"
