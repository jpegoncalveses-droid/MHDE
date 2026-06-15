"""Hot-path tests for the capture-core store writer (pre-sharding perf work).

The firehose CPU profile attributed ~13% of one core to two pure-Python per-row
helpers in ``store.py``:

  * ``_date_str`` — ``strftime`` formatted once per row (7.3% self-time).
  * ``_estimate_row_bytes`` — a genexpr building ``str()`` reprs of the nested
    price-level lists on every append (6.0% self-time).

These tests pin the behaviour that the optimisation MUST preserve:
  (a) date-string correctness across the UTC day boundary, and that formatting
      now happens once per epoch-day (cached) rather than once per row;
  (b) the byte estimate stays within tolerance of the row's actual serialized
      size, so the size-based flush still triggers at ~the same buffered volume.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from crypto.research.capture_core import store


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _actual_bytes(row: dict) -> int:
    """Ground-truth serialized size: compact-JSON UTF-8 byte length.

    A stable, implementation-independent stand-in for the row's real on-the-wire
    / in-buffer size, used to bound how far the cheap estimate may drift.
    """
    return len(json.dumps(row, separators=(",", ":")).encode("utf-8"))


def _depth_row(n_levels: int) -> dict:
    """A depthUpdate row whose nested ``b``/``a`` lists dominate its size."""
    levels = [[f"{1000 + i}.50", f"{i}.700"] for i in range(n_levels)]
    return {
        "recv_ts_ns": 1_700_000_000_000_000_000,
        "e": "depthUpdate",
        "E": _ms(datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)),
        "T": _ms(datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)),
        "s": "BTCUSDT",
        "U": 1, "u": 2, "pu": 0,
        "b": levels,
        "a": levels,
    }


def _aggtrade_row() -> dict:
    e_ms = _ms(datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc))
    return {"recv_ts_ns": 1_700_000_000_000_000_000, "e": "aggTrade", "E": e_ms,
            "a": 10, "s": "BTCUSDT", "p": "100.5", "q": "2.0", "f": 1, "l": 2,
            "T": e_ms, "m": False}


# -- (a) date-string: correct across the UTC boundary, formatted once per day --

def test_date_str_correct_across_utc_day_boundary():
    store._DATE_STR_CACHE.clear()
    last_of_day = _ms(datetime(2026, 6, 14, 23, 59, 59, 999_000, tzinfo=timezone.utc))
    midnight = _ms(datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc))
    assert store._date_str(last_of_day) == "2026-06-14"
    assert store._date_str(midnight) == "2026-06-15"
    # exact boundary: one millisecond before midnight is still the previous day
    assert store._date_str(midnight - 1) == "2026-06-14"


def test_date_str_formats_once_per_epoch_day_not_per_row():
    store._DATE_STR_CACHE.clear()
    base = _ms(datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc))
    # 5000 rows spread across the SAME UTC day (17s apart -> ~23.6h span)
    for i in range(5000):
        store._date_str(base + i * 17_000)
    assert len(store._DATE_STR_CACHE) == 1          # one format for the whole day
    # a row on the next day adds exactly one more cache entry
    store._date_str(base + 86_400_000)
    assert len(store._DATE_STR_CACHE) == 2


def test_date_str_matches_reference_strftime_over_a_range_of_days():
    store._DATE_STR_CACHE.clear()
    base = _ms(datetime(2026, 1, 1, 6, 30, 0, tzinfo=timezone.utc))
    for d in range(0, 400):                          # spans year + leap boundaries
        ms = base + d * 86_400_000
        ref = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        assert store._date_str(ms) == ref


# -- (b) byte estimate stays within tolerance of actual serialized size --

def test_estimate_row_bytes_within_tolerance_aggtrade():
    row = _aggtrade_row()
    est = store._estimate_row_bytes(row)
    actual = _actual_bytes(row)
    assert abs(est - actual) / actual <= 0.30


def test_estimate_row_bytes_within_tolerance_large_depth_row():
    row = _depth_row(500)                             # nested-list dominated
    est = store._estimate_row_bytes(row)
    actual = _actual_bytes(row)
    assert abs(est - actual) / actual <= 0.30


def test_estimate_row_bytes_scales_with_depth_levels():
    """A bigger book must estimate strictly larger so size-flush still triggers."""
    small = store._estimate_row_bytes(_depth_row(10))
    big = store._estimate_row_bytes(_depth_row(1000))
    assert big > small * 10
