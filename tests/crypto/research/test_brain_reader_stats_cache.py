"""Drift Fix 3: the bounded FRAGMENT-STATS CACHE — exact recv bounds, one footer read per
fragment per process, and the AHEAD-of-window skip that makes fast-tick cost lag-independent.

The mtime/name rules (test_brain_reader_hour_skip) skip fragments provably BELOW the cursor
footer-free — but they have no UPPER bound, so every raw part flushed between the cursor and
``now`` was footer-opened by pyarrow on EVERY tick while the loop was behind: unskippable
opens grew ~linearly with lag (write-rate x lag), the measured feedback loop behind the
2026-07-02 runaway (~24k markPrice opens/tick at 843s lag, ~52k at 30 min).

The cache stores each fragment's EXACT ``recv_ts_ns`` [min, max] — read ONCE from the parquet
footer statistics — keyed ``(path, mtime_ns, size)`` (immutable fragments make that a content
identity; a replaced file misses and re-reads). With it the scoped reader excludes, before
``ds.dataset()`` ever sees them:

  * fragments with ``max <= cursor``  (exact row values -> no skew guard needed), and
  * fragments with ``min > before_recv_ts_ns`` (the read ceiling) — the AHEAD skip: a part
    flushed past the forward window provably holds no in-window row, so a behind cursor reads
    O(window) fragments, not O(lag).

LOAD-BEARING: the oracle/randomized tests assert byte-identical rows vs the plain
``recv > cursor`` (and ``<= ceiling``) row filter, cold cache and warm, across advancing
cursors. The cache is an LRU bounded by ``cfg.BRAIN_FRAGMENT_STATS_CACHE_MAX``; an evicted or
missing entry only costs one footer re-read, never correctness.
"""
from __future__ import annotations

import os
import pathlib
import random
from uuid import uuid4

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import config as cfg
from crypto.research.brain import reader

_MS_TO_NS = 1_000_000
_DAY0_S = 1_782_000_000            # 2026-06-21 00:00:00 UTC (one UTC day; date-prune inert)


def _ns(hour: int, minute: int = 0, sec: int = 0) -> int:
    return (_DAY0_S + hour * 3600 + minute * 60 + sec) * 1_000_000_000


def _existing(root, symbol) -> set:
    d = pathlib.Path(root, "bookTicker", f"symbol={symbol}")
    return set(d.rglob("*.parquet")) if d.exists() else set()


def _write_part(root, symbol, recv_ns) -> pathlib.Path:
    before = _existing(root, symbol)
    e_ms = recv_ns // _MS_TO_NS
    w = capture_store.bookticker_writer(str(root))
    w.append({"recv_ts_ns": recv_ns, "e": "bookTicker", "u": 1, "s": symbol,
              "b": "100.0", "B": "1.0", "a": "101.0", "A": "1.0", "T": e_ms, "E": e_ms})
    w.flush_all()
    new = _existing(root, symbol) - before
    assert len(new) == 1, new
    return next(iter(new))


def _set_mtime_ns(path: pathlib.Path, ns: int) -> None:
    os.utime(path, (ns / 1e9, ns / 1e9))


def _read(root, symbol, cursor_ns, before_ns=None):
    return reader.read_new_bookticker(str(root), after_recv_ts_ns=cursor_ns,
                                      symbols=[symbol], before_recv_ts_ns=before_ns)


def _fresh_cache(monkeypatch):
    """Give the module a fresh, real cache so tests never see another test's entries."""
    cache = reader._FragmentStatsCache(cfg.BRAIN_FRAGMENT_STATS_CACHE_MAX)
    monkeypatch.setattr(reader, "_FRAGMENT_STATS_CACHE", cache)
    return cache


def _spy_dataset(monkeypatch):
    calls = []
    orig = reader.ds.dataset

    def spy(source, *a, **k):
        calls.append(source)
        return orig(source, *a, **k)

    monkeypatch.setattr(reader.ds, "dataset", spy)
    return calls


def _spy_metadata(monkeypatch):
    calls = []
    orig = reader.pq.read_metadata

    def spy(path, *a, **k):
        calls.append(str(path))
        return orig(path, *a, **k)

    monkeypatch.setattr(reader.pq, "read_metadata", spy)
    return calls


def _dataset_names(ds_calls) -> set:
    out = set()
    for src in ds_calls:
        out |= {pathlib.Path(p).name for p in src}
    return out


# --- footer-once: a warm cache re-opens nothing for unchanged fragments -----------------

def test_warm_cache_reopens_no_footer_for_unchanged_fragments(tmp_path, monkeypatch):
    # The fan-out shape: parts whose rows are BELOW the cursor but whose mtime is RECENT (the
    # mtime rule cannot skip them). Cold read: one metadata read each, then excluded via exact
    # stats. Warm read: NO footer access at all — no metadata re-read, not in any ds.dataset.
    _fresh_cache(monkeypatch)
    sym = "AAAUSDT"
    stale = []
    for m in (10, 20, 30):
        p = _write_part(tmp_path, sym, _ns(3, m))
        _set_mtime_ns(p, _ns(3, 58))                       # flushed "just now" -> mtime rule keeps
        stale.append(p)
    live = _write_part(tmp_path, sym, _ns(3, 57))
    _set_mtime_ns(live, _ns(3, 58))
    cursor = _ns(3, 50)

    md1 = _spy_metadata(monkeypatch)
    ds1 = _spy_dataset(monkeypatch)
    rows1 = _read(tmp_path, sym, cursor)
    assert [r["recv_ts_ns"] for r in rows1] == [_ns(3, 57)]
    assert {pathlib.Path(p).name for p in md1} == {p.name for p in stale} | {live.name}, \
        "cold read: every candidate fragment's footer stats read exactly once"
    assert _dataset_names(ds1) == {live.name}, \
        "exact stats exclude the provably-empty parts from the dataset on the FIRST read"

    md2 = _spy_metadata(monkeypatch)
    ds2 = _spy_dataset(monkeypatch)
    rows2 = _read(tmp_path, sym, cursor)
    assert rows2 == rows1
    assert md2 == [], "warm cache: zero footer re-reads for unchanged fragments"
    assert _dataset_names(ds2) == {live.name}


def test_ahead_fragment_excluded_by_stats_and_never_reopened(tmp_path, monkeypatch):
    # THE feedback-loop killer: a part flushed AHEAD of the read ceiling (min recv > before)
    # is excluded from ds.dataset on the first read (one metadata read) and costs nothing on
    # subsequent ticks while the cursor is still behind it.
    _fresh_cache(monkeypatch)
    sym = "AAAUSDT"
    in_window = _write_part(tmp_path, sym, _ns(3, 51))
    _set_mtime_ns(in_window, _ns(3, 59))
    ahead = _write_part(tmp_path, sym, _ns(3, 58))          # beyond the 3:55 ceiling
    _set_mtime_ns(ahead, _ns(3, 59))
    cursor, ceiling = _ns(3, 50), _ns(3, 55)

    md1 = _spy_metadata(monkeypatch)
    ds1 = _spy_dataset(monkeypatch)
    rows1 = _read(tmp_path, sym, cursor, before_ns=ceiling)
    assert [r["recv_ts_ns"] for r in rows1] == [_ns(3, 51)]
    assert _dataset_names(ds1) == {in_window.name}, \
        "a fragment wholly ahead of the ceiling is excluded from the dataset"
    assert ahead.name in {pathlib.Path(p).name for p in md1}

    md2 = _spy_metadata(monkeypatch)
    rows2 = _read(tmp_path, sym, cursor, before_ns=ceiling)
    assert rows2 == rows1
    assert md2 == [], "second tick at the same lag: the ahead fragment costs zero footer reads"


def test_unbounded_read_keeps_ahead_fragments(tmp_path, monkeypatch):
    # No ceiling (before_recv_ts_ns=None, the deliberate full-forward path) -> the ahead skip
    # must not apply; every above-cursor row comes back.
    _fresh_cache(monkeypatch)
    sym = "AAAUSDT"
    for m, s in ((51, 0), (58, 0)):
        p = _write_part(tmp_path, sym, _ns(3, m, s))
        _set_mtime_ns(p, _ns(3, 59))
    got = sorted(r["recv_ts_ns"] for r in _read(tmp_path, sym, _ns(3, 50)))
    assert got == [_ns(3, 51), _ns(3, 58)]


# --- key invalidation: (path, mtime_ns, size) is a content identity ---------------------

def test_replaced_file_same_name_misses_cache_and_rereads(tmp_path, monkeypatch):
    _fresh_cache(monkeypatch)
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(3, 10))              # below cursor -> cached as skippable
    _set_mtime_ns(p, _ns(3, 58))
    cursor = _ns(3, 50)
    assert _read(tmp_path, sym, cursor) == []

    # Replace the file IN PLACE with new content holding an above-cursor row (not an MHDE
    # workflow — this pins that the cache key, not luck, protects correctness).
    fresh = _write_part(tmp_path, sym, _ns(3, 56))
    os.replace(fresh, p)
    _set_mtime_ns(p, _ns(3, 59))                            # new mtime -> new key -> miss
    rows = _read(tmp_path, sym, cursor)
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 56)], \
        "a replaced fragment (new mtime/size) must be re-read, never served from the cache"


# --- boundedness -------------------------------------------------------------------------

def test_cache_is_lru_bounded():
    c = reader._FragmentStatsCache(3)
    for i in range(5):
        c.put(("p%d" % i, 1, 1), (i, i))
    assert len(c) == 3
    assert c.get(("p0", 1, 1)) is reader._CACHE_MISS       # evicted (oldest)
    assert c.get(("p4", 1, 1)) == (4, 4)
    c.get(("p2", 1, 1))                                    # touch -> most recent
    c.put(("p5", 1, 1), (5, 5))
    assert c.get(("p2", 1, 1)) == (2, 2), "recently-used entry survives the next eviction"
    assert c.get(("p3", 1, 1)) is reader._CACHE_MISS


def test_cache_default_bound_is_config_knob():
    assert reader._FRAGMENT_STATS_CACHE.maxsize == cfg.BRAIN_FRAGMENT_STATS_CACHE_MAX


# --- corruption tolerance stays intact ---------------------------------------------------

def test_corrupt_fragment_tolerated_cold_and_warm(tmp_path, monkeypatch):
    _fresh_cache(monkeypatch)
    sym = "AAAUSDT"
    bad = _write_part(tmp_path, sym, _ns(3, 52))
    _set_mtime_ns(bad, _ns(3, 59))
    with open(bad, "r+b") as f:                            # truncate -> unreadable footer
        f.truncate(16)
    good = _write_part(tmp_path, sym, _ns(3, 53))
    _set_mtime_ns(good, _ns(3, 59))
    cursor = _ns(3, 50)
    for _ in range(2):                                     # cold, then warm — never crashes
        rows = _read(tmp_path, sym, cursor)
        assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 53)]


# --- THE LOAD-BEARING ORACLE: cache-active reads == the row-filter oracle ---------------

def test_oracle_byte_identical_cold_and_warm_advancing_cursor(tmp_path, monkeypatch):
    _fresh_cache(monkeypatch)
    rng = random.Random(20260702)
    sym = "AAAUSDT"
    recvs = []
    for _ in range(70):
        rv = _ns(rng.randint(0, 7), rng.randint(0, 59), rng.randint(0, 59))
        p = _write_part(tmp_path, sym, rv)
        _set_mtime_ns(p, rv + rng.randint(0, 30) * 1_000_000_000)
        recvs.append(rv)
    # forward-only cursor with a W=300s ceiling, each position read twice (cold then warm)
    cursor = _ns(0, 30)
    w_ns = 300 * 1_000_000_000
    for _ in range(20):
        ceiling = cursor + w_ns
        expected = sorted(rv for rv in recvs if cursor < rv <= ceiling)
        for attempt in ("cold", "warm"):
            got = sorted(r["recv_ts_ns"] for r in _read(tmp_path, sym, cursor, before_ns=ceiling))
            assert got == expected, f"{attempt} cursor={cursor}: {len(got)} vs {len(expected)}"
        cursor += rng.randint(60, 1200) * 1_000_000_000
