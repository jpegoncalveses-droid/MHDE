"""Fix 3: cursor-aware FOOTER-FREE fragment skip in the scoped reader (full-mtime refinement).

Every date-range fragment used to be footer-opened each tick to read its recv stats, even when
EVERY row in it is already below the forward-only cursor. This skips such a fragment WITHOUT
opening it, using a footer-free, provable upper bound on the fragment's max ``recv_ts_ns``:

  * ``compact-h<H>-*`` -> ``(H+1)*3600s - 1``: the capture closed-hour compactor buckets the parts
    it merged by flush hour (``maintenance.py``: ``int(mtime//3600)``) and stamps H into the name,
    so every row was flushed in ``[H, H+1)`` and (recv <= flush) has ``recv < (H+1)*3600s``.
  * raw ``part-*`` -> ``floor(st_mtime*1e9)`` AT FULL PRECISION: a row is received before its part
    is flushed, so ``recv <= flush mtime``. The full mtime (not the truncated hour) is what lets
    the OPEN clock hour's already-flushed, below-cursor parts be skipped too — the hour-granular
    bound could not, since the open hour's hour IS the cursor's hour.

Skip iff ``ceiling + skew_guard <= cursor``. ``compact-migrated-*`` (a whole sealed DAY) and any
unrecognized name carry no provable ceiling -> never skipped. A 60s skew guard keeps any fragment
whose flush is within the guard of the cursor open, absorbing sub-second recv-vs-flush clock skew.

THE LOAD-BEARING TESTS (``*_oracle`` / ``*_randomized_equivalence``): the scoped + skipped read
returns BYTE-IDENTICAL rows to the ``recv > cursor`` whole-tree oracle — no in-window row dropped,
at any point in the hour, with the full-mtime skip active.
"""
from __future__ import annotations

import os
import pathlib
import random
from uuid import uuid4

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader

_MS_TO_NS = 1_000_000
_HOUR_NS = 3600 * 1_000_000_000
# 2026-06-21 00:00:00 UTC == epoch second 1_782_000_000 == epoch hour 495_000. Hour-of-day k is
# epoch hour 495000+k; ALL fragments live in this one UTC day so ``date=`` is constant and the
# 1-day date-prune margin keeps them all — the suite isolates the recv-ceiling skip, not date-prune.
_DAY0_S = 1_782_000_000
_BASE_HOUR = _DAY0_S // 3600          # 495_000


def _ns(hour_of_day: int, minute: int = 0, sec: int = 0) -> int:
    return (_DAY0_S + hour_of_day * 3600 + minute * 60 + sec) * 1_000_000_000


def _sym_dir(root, symbol) -> pathlib.Path:
    return pathlib.Path(root, "bookTicker", f"symbol={symbol}")


def _existing(root, symbol) -> set:
    d = _sym_dir(root, symbol)
    return set(d.rglob("*.parquet")) if d.exists() else set()


def _write_part(root, symbol, recv_ns) -> pathlib.Path:
    """Append+flush one bookTicker row (``E = recv`` so ``date=`` tracks recv) -> a new
    ``part-<uuid>``. Returns the new file path. Caller sets the mtime (the flush time) explicitly."""
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
    """Pin a fragment's flush mtime to an exact ns instant (a VALID fragment has mtime >= max recv)."""
    t = ns / 1_000_000_000
    os.utime(path, (t, t))


def _rename(path: pathlib.Path, new_name: str) -> pathlib.Path:
    dest = path.with_name(new_name)
    path.rename(dest)
    return dest


def _make_compact_h(path: pathlib.Path, hour_epoch: int) -> pathlib.Path:
    return _rename(path, f"compact-h{hour_epoch}-{uuid4().hex}.parquet")


def _make_compact_migrated(path: pathlib.Path) -> pathlib.Path:
    return _rename(path, f"compact-migrated-{uuid4().hex}.parquet")


def _read(root, symbol, cursor_ns):
    return reader.read_new_bookticker(str(root), after_recv_ts_ns=cursor_ns, symbols=[symbol])


def _spy(monkeypatch):
    """Record the ``source`` list ``ds.dataset`` is constructed over (which fragments open)."""
    calls = []
    orig = reader.ds.dataset

    def spy(source, *a, **k):
        calls.append(source)
        return orig(source, *a, **k)

    monkeypatch.setattr(reader.ds, "dataset", spy)
    return calls


def _opened_names(calls) -> set:
    assert len(calls) == 1, f"one scoped construction expected, got {len(calls)}"
    return {pathlib.Path(p).name for p in calls[0]}


# --- footer-free SKIP drivers (RED against the hour-granular reader) --------------------

def test_current_hour_subcursor_raw_part_skipped(tmp_path, monkeypatch):
    # THE full-mtime capability: a raw part in the SAME clock hour as the cursor, flushed early
    # enough that mtime + guard is below the cursor, is PROVABLY empty (recv <= mtime) and skipped.
    # The hour-granular rule could not skip it (its hour == the cursor's hour).
    sym = "AAAUSDT"
    early = _write_part(tmp_path, sym, _ns(3, 5)); _set_mtime_ns(early, _ns(3, 5, 10))
    live = _write_part(tmp_path, sym, _ns(3, 50, 15)); _set_mtime_ns(live, _ns(3, 50, 20))
    cursor = _ns(3, 50)                       # same hour 3; early flushed ~45 min before, live just after
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    names = _opened_names(calls)
    assert early.name not in names, \
        "a current-hour part flushed before cursor-guard is provably empty -> skipped (full mtime)"
    assert live.name in names, \
        "a current-hour part whose mtime is at/after the cursor may hold newer rows -> read"
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 50, 15)]


def test_raw_part_well_below_cursor_skipped(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    old = _write_part(tmp_path, sym, _ns(1, 10)); _set_mtime_ns(old, _ns(1, 10, 5))
    cur = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_ns(cur, _ns(3, 45, 5))
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    names = _opened_names(calls)
    assert old.name not in names, "a raw part flushed well below the cursor is skipped footer-free"
    assert cur.name in names
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 45)]


def test_compact_h_below_cursor_skipped_by_filename(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p_old = _write_part(tmp_path, sym, _ns(1, 10))
    old = _make_compact_h(p_old, _BASE_HOUR + 1)            # filename hour 1 (below cursor hour 3)
    _set_mtime_ns(old, _ns(100, 0))                        # FUTURE mtime: prove FILENAME governs compact-h
    cur = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_ns(cur, _ns(3, 45, 5))
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    names = _opened_names(calls)
    assert old.name not in names, \
        "compact-h<H> with H's hour fully below cursor skipped by FILENAME (mtime is in the future here)"
    assert cur.name in names
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 45)]


def test_compact_h_current_hour_still_read(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(3, 45))
    f = _make_compact_h(p, _BASE_HOUR + 3)                  # filename hour == cursor hour -> may hold > cursor
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    assert f.name in _opened_names(calls), "a compact-h whose hour spans the cursor must be read"
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 45)]


def test_compact_migrated_never_skipped(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(0, 10))
    m = _make_compact_migrated(p); _set_mtime_ns(m, _ns(0, 10, 5))   # old name + old mtime -> still opened
    keep = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_ns(keep, _ns(3, 45, 5))
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    _read(tmp_path, sym, cursor)
    assert m.name in _opened_names(calls), \
        "compact-migrated-* spans a whole day (no single flush instant) -> never skipped"


# --- the skew guard (mtime granularity) ------------------------------------------------

def test_guard_keeps_recent_part_within_skew(tmp_path, monkeypatch):
    # a raw part flushed within the guard of the cursor is KEPT (its rows could be cursor-or-newer
    # under any backward recv-vs-flush clock skew).
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(3, 30)); _set_mtime_ns(p, _ns(3, 49, 30))   # 30s inside the 60s guard
    cursor = _ns(3, 50)
    calls = _spy(monkeypatch)
    _read(tmp_path, sym, cursor)
    assert p.name in _opened_names(calls), "mtime within the 60s guard of the cursor -> still opened"


def test_guard_skips_part_past_skew(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(3, 30)); _set_mtime_ns(p, _ns(3, 48))        # 2 min before cursor
    keep = _write_part(tmp_path, sym, _ns(3, 50, 30)); _set_mtime_ns(keep, _ns(3, 50, 30))
    cursor = _ns(3, 50)
    calls = _spy(monkeypatch)
    _read(tmp_path, sym, cursor)
    names = _opened_names(calls)
    assert p.name not in names, "mtime more than the guard below the cursor -> provably empty -> skipped"
    assert keep.name in names


# --- THE LOAD-BEARING ORACLE: full-mtime-skipped read == recv>cursor whole-tree set ----

def test_fullmtime_read_matches_recv_window_oracle(tmp_path):
    sym = "AAAUSDT"
    recvs = [_ns(0, 10), _ns(1, 20), _ns(2, 30), _ns(3, 5), _ns(3, 45), _ns(3, 55), _ns(4, 5)]
    for rv in recvs:
        p = _write_part(tmp_path, sym, rv)
        _set_mtime_ns(p, rv + 5_000_000_000)               # flush 5s after recv (valid fragment)
    cursor = _ns(3, 50)
    got = sorted(r["recv_ts_ns"] for r in _read(tmp_path, sym, cursor))
    expected = sorted(rv for rv in recvs if rv > cursor)   # {3:55, 4:05}
    assert got == expected, f"{got} != {expected}"


def test_fullmtime_randomized_equivalence(tmp_path):
    rng = random.Random(20260629)
    sym = "AAAUSDT"
    recvs = []
    for _ in range(80):
        h, m, s = rng.randint(0, 9), rng.randint(0, 59), rng.randint(0, 59)
        rv = _ns(h, m, s)
        p = _write_part(tmp_path, sym, rv)
        _set_mtime_ns(p, rv + rng.randint(0, 30) * 1_000_000_000)   # flush 0-30s after recv (valid)
        if rng.random() < 0.3:                                      # some -> compact-h<recv's flush hour>
            _make_compact_h(p, _BASE_HOUR + h)
        recvs.append(rv)
    for _ in range(24):
        cursor = _ns(rng.randint(0, 9), rng.randint(0, 59), rng.randint(0, 59))
        got = sorted(r["recv_ts_ns"] for r in _read(tmp_path, sym, cursor))
        expected = sorted(rv for rv in recvs if rv > cursor)
        assert got == expected, f"cursor={cursor}: {len(got)} vs {len(expected)} rows"


def test_all_fragments_below_cursor_returns_empty_no_crash(tmp_path):
    sym = "AAAUSDT"
    for h in (0, 1, 2):
        p = _write_part(tmp_path, sym, _ns(h, 10)); _set_mtime_ns(p, _ns(h, 10, 5))
    cursor = _ns(5, 0)                                      # every fragment provably below cursor
    assert _read(tmp_path, sym, cursor) == []
