"""Tests for PR-2 stream builders + row mappers (incl. array fan-out)."""
from __future__ import annotations

from crypto.research.capture_core import service as svc


def test_per_symbol_streams_covers_aggtrade_depth_bookticker():
    out = svc.per_symbol_streams(["BTCUSDT"])
    assert out == ["btcusdt@aggTrade", "btcusdt@depth@100ms", "btcusdt@bookTicker"]


def test_capture_streams_appends_market_wide_array_streams():
    out = svc.capture_streams(["BTCUSDT", "ETHUSDT"])
    assert "!forceOrder@arr" in out
    assert "!markPrice@arr@1s" in out
    # per-symbol come first, market-wide last
    assert out[-2:] == ["!forceOrder@arr", "!markPrice@arr@1s"]
    assert out.count("!forceOrder@arr") == 1


def test_depth_bookticker_streams_for_partial_loadtest():
    out = svc.depth_bookticker_streams(["BTCUSDT"])
    assert out == ["btcusdt@depth@100ms", "btcusdt@bookTicker"]


def test_depth_row_coerces_ids_and_keeps_levels():
    data = {"e": "depthUpdate", "E": "1", "T": "2", "s": "BTCUSDT",
            "U": "100", "u": "110", "pu": "99",
            "b": [["67000.0", "1.0"], ["66999.0", "0.0"]], "a": [["67001.0", "2.0"]]}
    row = svc.depth_row(data, recv_ns=7)
    assert row["U"] == 100 and row["u"] == 110 and row["pu"] == 99
    assert row["recv_ts_ns"] == 7
    assert row["b"] == [["67000.0", "1.0"], ["66999.0", "0.0"]]  # zero-qty kept


def test_bookticker_row_coerces():
    data = {"e": "bookTicker", "u": "400", "s": "ETHUSDT", "b": "42.0", "B": "10",
            "a": "42.1", "A": "5", "T": "9", "E": "8"}
    row = svc.bookticker_row(data, recv_ns=1)
    assert row["u"] == 400 and row["s"] == "ETHUSDT" and row["b"] == "42.0"


def test_forceorder_fan_out_single_event_per_message():
    data = {"e": "forceOrder", "E": "5", "o": {
        "s": "SOLUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "q": "100",
        "p": "150.0", "ap": "149.9", "X": "FILLED", "l": "20", "z": "100", "T": "4"}}
    rows = svc.forceorder_rows(data, recv_ns=2)
    assert len(rows) == 1
    r = rows[0]
    assert r["s"] == "SOLUSDT" and r["S"] == "SELL" and r["ap"] == "149.9"
    assert r["E"] == 5 and r["T"] == 4 and r["recv_ts_ns"] == 2


def test_markprice_array_fans_out_to_one_row_per_symbol():
    data = [
        {"e": "markPriceUpdate", "E": "1", "s": "BTCUSDT", "p": "67000", "i": "67005",
         "P": "67010", "r": "0.0001", "T": "100"},
        {"e": "markPriceUpdate", "E": "1", "s": "ETHUSDT", "p": "42", "i": "42.1",
         "P": "42.2", "r": "0.0002", "T": "100"},
    ]
    rows = svc.markprice_rows(data, recv_ns=3)
    assert [r["s"] for r in rows] == ["BTCUSDT", "ETHUSDT"]
    assert rows[0]["r"] == "0.0001" and rows[1]["i"] == "42.1"
    assert all(r["recv_ts_ns"] == 3 for r in rows)


def test_snapshot_row_attaches_symbol_and_coerces_id():
    snap = {"lastUpdateId": "123", "E": "5", "T": "6",
            "bids": [["1", "2"]], "asks": [["3", "4"]]}
    row = svc.snapshot_row("BTCUSDT", snap, recv_ns=9)
    assert row["s"] == "BTCUSDT" and row["lastUpdateId"] == 123
    assert row["b"] == [["1", "2"]] and row["a"] == [["3", "4"]]  # bids/asks -> b/a


def test_snapshot_row_missing_event_time_falls_back_to_recv_ms():
    # Defensive: if a snapshot ever lacks E, partition on recv-derived ms, never 0
    # (which would land the row in date=1970-01-01).
    snap = {"lastUpdateId": 1, "bids": [], "asks": []}
    recv_ns = 1_748_563_200_000 * 1_000_000   # ms -> ns
    row = svc.snapshot_row("BTCUSDT", snap, recv_ns=recv_ns)
    assert row["E"] == 1_748_563_200_000
