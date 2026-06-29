"""Fix 3: cursor/hour-aware FOOTER-FREE fragment skip in the scoped reader.

Every date-range fragment used to be footer-opened each tick to read its recv stats, even
when its ENTIRE hour is already below the forward-only cursor. This skips such fragments
WITHOUT opening them, using two provable, footer-free hour signals — both equal to the
capture compactor's OWN bucketing key (``maintenance.py`` buckets writer parts by
``int(mtime // 3600)``), so a skipped fragment provably holds no in-window (recv > cursor)
row:

  * ``compact-h<H>-*`` -> ``H``, the flush-hour stamped in the filename.
  * raw ``part-*``     -> ``floor(st_mtime / 3600)``; ``recv_ts_ns <= flush mtime`` (a row is
                          received BEFORE its part is flushed), so every row's recv is
                          ``< (hour + 1) * 3600s``.

``compact-migrated-*`` (a whole sealed day, no single hour) and any other name carry no hour
bound and are NEVER skipped. A small skew/boundary guard keeps the current hour AND the
just-closed hour (within the guard) open, absorbing sub-second recv-vs-flush clock skew.

THE LOAD-BEARING TESTS (``*_oracle`` / ``*_randomized_equivalence``): the scoped + hour-skipped
read returns BYTE-IDENTICAL rows to the ``recv > cursor`` whole-tree oracle — no in-window row
dropped, at the hour boundary or anywhere.
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
# 2026-06-21 00:00:00 UTC == epoch second 1_782_000_000 == epoch hour 495_000. Hour-of-day k
# is epoch hour 495000+k; ALL fragments live in this one UTC day so ``date=`` is constant and
# the 1-day date-prune margin keeps them all — the suite isolates hour-skip, not date-prune.
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
    ``part-<uuid>``. Returns the new file path."""
    before = _existing(root, symbol)
    e_ms = recv_ns // _MS_TO_NS
    w = capture_store.bookticker_writer(str(root))
    w.append({"recv_ts_ns": recv_ns, "e": "bookTicker", "u": 1, "s": symbol,
              "b": "100.0", "B": "1.0", "a": "101.0", "A": "1.0", "T": e_ms, "E": e_ms})
    w.flush_all()
    new = _existing(root, symbol) - before
    assert len(new) == 1, new
    return next(iter(new))


def _set_mtime_hour(path: pathlib.Path, hour_of_day: int) -> None:
    """Pin a fragment's mtime mid-way into the given hour-of-day (deterministic)."""
    t = _DAY0_S + hour_of_day * 3600 + 1800
    os.utime(path, (t, t))


def _set_mtime_epoch_s(path: pathlib.Path, epoch_s: int) -> None:
    os.utime(path, (epoch_s, epoch_s))


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


# --- footer-free SKIP drivers (RED against today's open-everything reader) -------------

def test_raw_part_below_cursor_hour_skipped_by_mtime(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    old = _write_part(tmp_path, sym, _ns(1, 10)); _set_mtime_hour(old, 1)
    cur = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_hour(cur, 3)
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    names = _opened_names(calls)
    assert old.name not in names, \
        "a raw part whose mtime-hour is below the cursor hour must be skipped footer-free"
    assert cur.name in names
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 45)]


def test_compact_h_below_cursor_hour_skipped_by_filename(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p_old = _write_part(tmp_path, sym, _ns(1, 10))
    old = _make_compact_h(p_old, _BASE_HOUR + 1)            # filename hour = 495001 (below cursor)
    _set_mtime_epoch_s(old, _DAY0_S + 100 * 3600)          # FUTURE mtime: prove FILENAME governs
    cur = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_hour(cur, 3)
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    names = _opened_names(calls)
    assert old.name not in names, \
        "compact-h<H> with H below cursor hour skipped by FILENAME (its mtime is in the future here)"
    assert cur.name in names
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 45)]


def test_raw_part_at_cursor_hour_still_read(tmp_path, monkeypatch):
    # operator-required boundary: a raw part whose mtime-hour == cursor hour MUST be opened.
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_hour(p, 3)
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    assert p.name in _opened_names(calls), "current-hour raw part must never be skipped"
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 45)]


def test_compact_h_at_cursor_hour_still_read(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(3, 45))
    f = _make_compact_h(p, _BASE_HOUR + 3)                  # filename hour == cursor hour
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    rows = _read(tmp_path, sym, cursor)
    assert f.name in _opened_names(calls), "current-hour compact-h must never be skipped"
    assert [r["recv_ts_ns"] for r in rows] == [_ns(3, 45)]


def test_compact_migrated_never_skipped(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p = _write_part(tmp_path, sym, _ns(0, 10))
    m = _make_compact_migrated(p); _set_mtime_hour(m, 0)    # old name + old mtime -> still opened
    keep = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_hour(keep, 3)
    cursor = _ns(3, 30)
    calls = _spy(monkeypatch)
    _read(tmp_path, sym, cursor)
    assert m.name in _opened_names(calls), \
        "compact-migrated-* spans a whole day (no single hour) -> never hour-skipped"


def test_guard_keeps_just_closed_hour_open_within_skew(tmp_path, monkeypatch):
    # cursor only 30s into hour 3 (< the 60s skew guard): the just-closed hour 2 must STILL be
    # opened, absorbing any backward recv-vs-flush clock skew at the hour boundary.
    sym = "AAAUSDT"
    p2 = _write_part(tmp_path, sym, _ns(2, 59)); _set_mtime_hour(p2, 2)
    cursor = _ns(3, 0, 30)
    calls = _spy(monkeypatch)
    _read(tmp_path, sym, cursor)
    assert p2.name in _opened_names(calls), \
        "within the skew guard the just-closed hour is still opened"


def test_guard_skips_just_closed_hour_past_skew(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    p2 = _write_part(tmp_path, sym, _ns(2, 30)); _set_mtime_hour(p2, 2)
    cur = _write_part(tmp_path, sym, _ns(3, 45)); _set_mtime_hour(cur, 3)
    cursor = _ns(3, 30)                                     # well past the 60s guard
    calls = _spy(monkeypatch)
    _read(tmp_path, sym, cursor)
    names = _opened_names(calls)
    assert p2.name not in names, "past the skew guard the just-closed hour is skipped"
    assert cur.name in names


# --- THE LOAD-BEARING ORACLE: hour-skipped read == recv>cursor whole-tree set ----------

def test_hourskip_read_matches_recv_window_oracle(tmp_path):
    sym = "AAAUSDT"
    recvs = [_ns(0, 10), _ns(1, 20), _ns(2, 30), _ns(3, 15), _ns(3, 45), _ns(4, 5), _ns(5, 50)]
    for rv in recvs:
        p = _write_part(tmp_path, sym, rv)
        _set_mtime_hour(p, rv // _HOUR_NS - _BASE_HOUR)     # mtime hour == recv hour (valid frag)
    cursor = _ns(3, 30)
    got = sorted(r["recv_ts_ns"] for r in _read(tmp_path, sym, cursor))
    expected = sorted(rv for rv in recvs if rv > cursor)    # the recv>cursor oracle
    assert got == expected, f"{got} != {expected}"


def test_hourskip_randomized_equivalence(tmp_path):
    rng = random.Random(20260629)
    sym = "AAAUSDT"
    recvs = []
    for _ in range(60):
        h, m, s = rng.randint(0, 9), rng.randint(0, 59), rng.randint(0, 59)
        rv = _ns(h, m, s)
        p = _write_part(tmp_path, sym, rv)
        _set_mtime_epoch_s(p, _DAY0_S + h * 3600 + 3599)    # mtime end-of-hour h: recv <= mtime
        if rng.random() < 0.3:                              # promote some to compact-h<own hour>
            _make_compact_h(p, _BASE_HOUR + h)
        recvs.append(rv)
    for _ in range(16):
        cursor = _ns(rng.randint(0, 9), rng.randint(0, 59), rng.randint(0, 59))
        got = sorted(r["recv_ts_ns"] for r in _read(tmp_path, sym, cursor))
        expected = sorted(rv for rv in recvs if rv > cursor)
        assert got == expected, f"cursor={cursor}: {len(got)} vs {len(expected)} rows"


def test_all_fragments_below_cursor_returns_empty_no_crash(tmp_path):
    sym = "AAAUSDT"
    for h in (0, 1, 2):
        p = _write_part(tmp_path, sym, _ns(h, 10)); _set_mtime_hour(p, h)
    cursor = _ns(5, 0)                                      # every fragment provably below cursor
    assert _read(tmp_path, sym, cursor) == []
