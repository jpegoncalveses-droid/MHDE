"""Reader hardening (both surfaced by the validation canary, both pre-existing):

  A) PARTITION PRUNING — a bounded read (``symbols=[...]``) must open only the
     selected symbols' fragments. The old reader filtered on the in-row ``s`` field
     (not the Hive ``symbol=`` partition), so pyarrow scanned every symbol's
     fragments regardless (9.85 GiB peak at 5 symbols / 15 min). Proven here by a
     corrupt file in an UNSELECTED partition that the read must never open.

  B) FRAGMENT TOLERANCE — one truncated/corrupt parquet must be SKIPPED + RECORDED
     (logged), never crash the read (mirrors the 438-byte DEXEUSDT aggTrade file
     that crashed the canary). A skip is missing data: the partition is logged so it
     is visible, never silently dropped.

The fix lives in ``reader._read_dataset_rows`` and is shared by every per-source
reader; exercised here via ``read_new_bookticker``.
"""
from __future__ import annotations

import logging
import pathlib

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader

_E_MS = 1_781_640_000_000          # 2026-06-16 20:00:00 UTC -> date=2026-06-16 partition
_R0 = _E_MS * 1_000_000

# A 438-byte truncated parquet (PAR1 header, no footer) — the exact DEXEUSDT shape:
# "Parquet magic bytes not found in footer".
_TRUNCATED = b"PAR1" + b"\x00" * 434


def _write_bt(root, symbol, recv_ns):
    w = capture_store.bookticker_writer(str(root))
    w.append({"recv_ts_ns": recv_ns, "e": "bookTicker", "u": 1, "s": symbol,
              "b": "100.0", "B": "1.0", "a": "101.0", "A": "1.0", "T": _E_MS, "E": _E_MS})
    w.flush_all()


def _date_dir(root, symbol):
    return next(pathlib.Path(root, "bookTicker", f"symbol={symbol}").glob("date=*"))


def _drop_truncated(root, symbol):
    (_date_dir(root, symbol) / "part-truncated.parquet").write_bytes(_TRUNCATED)


def test_corrupt_fragment_in_unselected_partition_is_never_opened(tmp_path):
    # A: pruning. CCC holds a truncated file; reading [AAA, BBB] must prune CCC's
    # partition so the corrupt file is never opened (the old in-row filter scanned it).
    _write_bt(tmp_path, "AAAUSDT", _R0)
    _write_bt(tmp_path, "BBBUSDT", _R0 + 1)
    _write_bt(tmp_path, "CCCUSDT", _R0 + 2)
    _drop_truncated(tmp_path, "CCCUSDT")
    rows = reader.read_new_bookticker(str(tmp_path), symbols=["AAAUSDT", "BBBUSDT"])
    assert {r["symbol"] for r in rows} == {"AAAUSDT", "BBBUSDT"}


def test_corrupt_fragment_within_selected_partition_is_skipped_and_recorded(tmp_path, caplog):
    # B: tolerance. AAA holds a good file AND a truncated one; reading [AAA] must
    # return the good rows, skip the corrupt fragment, and LOG it (never crash).
    _write_bt(tmp_path, "AAAUSDT", _R0)
    _drop_truncated(tmp_path, "AAAUSDT")
    with caplog.at_level(logging.WARNING):
        rows = reader.read_new_bookticker(str(tmp_path), symbols=["AAAUSDT"])
    assert [r["symbol"] for r in rows] == ["AAAUSDT"]
    assert any("part-truncated.parquet" in r.getMessage() for r in caplog.records), \
        "the skipped corrupt fragment must be recorded (logged), not silently dropped"
