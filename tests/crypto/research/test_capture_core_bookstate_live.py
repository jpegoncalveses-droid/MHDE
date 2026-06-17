"""Live ground-truth proof for the online level book (skipped without network).

Validates level-application semantics against INDEPENDENT real REST snapshots —
the piece the cursor-only maintainer could not cover. Reconstruct a book from
snapshot A + the live @depth diff stream; independently seed a second book from a
LATER snapshot B; feed both the SAME diffs. Once both are synced they have applied
the identical contiguous diff stream to the same update id, so a correct level
engine makes them CONVERGE on the top-N — an exact, race-free check (no mid-diff
timing window). Well-formedness is asserted throughout; an adversarial corruption
must break the match.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from crypto.research.capture_core.book import DepthMaintainer

_SYMBOL = "BTCUSDT"
_WS_URL = f"wss://fstream.binance.com/ws/{_SYMBOL.lower()}@depth@100ms"
_MAX_DIFFS = 400          # ~40s ceiling at 100ms; we converge far sooner
_RECV_TIMEOUT = 15.0


def _well_formed(m: DepthMaintainer) -> None:
    bids = [(float(p), float(q)) for p, q in m.bids.items()]
    asks = [(float(p), float(q)) for p, q in m.asks.items()]
    assert all(q > 0 for _, q in bids) and all(q > 0 for _, q in asks)   # no zero/neg qty
    if bids and asks:
        assert max(p for p, _ in bids) < min(p for p, _ in asks)          # best bid < best ask


async def _converge() -> tuple[DepthMaintainer, DepthMaintainer]:
    import websockets
    from crypto.research.capture_core.client import CaptureRestClient

    rest = CaptureRestClient()
    m1 = DepthMaintainer(_SYMBOL)    # seeded from snapshot A (earlier)
    m2 = DepthMaintainer(_SYMBOL)    # seeded from snapshot B (later)

    async with websockets.connect(_WS_URL, open_timeout=_RECV_TIMEOUT) as ws:
        async def feed_both():
            d = json.loads(await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT))
            args = (int(d["U"]), int(d["u"]), int(d["pu"]), int(d["E"]))
            m1.on_diff(*args, bids=d.get("b"), asks=d.get("a"))
            m2.on_diff(*args, bids=d.get("b"), asks=d.get("a"))

        # buffer a few diffs, then seed m1 from snapshot A
        for _ in range(5):
            await feed_both()
        a = await asyncio.to_thread(rest.fetch_depth_snapshot, _SYMBOL)
        m1.on_snapshot(int(a["lastUpdateId"]), 0, bids=a["bids"], asks=a["asks"])

        seeded_b = False
        for _ in range(_MAX_DIFFS):
            await feed_both()
            if m1.synced:
                _well_formed(m1)                  # invariant holds across real maintenance
            if not seeded_b and m1.synced:
                # seed m2 from an INDEPENDENT, fresher snapshot B
                b = await asyncio.to_thread(rest.fetch_depth_snapshot, _SYMBOL)
                m2.on_snapshot(int(b["lastUpdateId"]), 0, bids=b["bids"], asks=b["asks"])
                seeded_b = True
            if m1.synced and m2.synced and m1.last_u == m2.last_u:
                _well_formed(m2)
                return m1, m2
    raise AssertionError("books did not both sync + converge within the diff budget")


def test_live_reconstruct_matches_independent_snapshot():
    pytest.importorskip("websockets")
    from websockets.exceptions import WebSocketException
    # Skip ONLY on true network/transport failure — let any error from the
    # code-under-test (the maintainer) propagate as a real test failure, so a
    # genuine regression on the capture host is never masked as a skip.
    net_errors = (OSError, asyncio.TimeoutError, WebSocketException)
    try:
        m1, m2 = asyncio.run(_converge())
    except net_errors as exc:                     # DNS/connect/timeout/WS transport
        pytest.skip(f"live Binance depth unavailable: {exc!r}")

    # GROUND TRUTH: both at the same update id via the same diffs -> identical top-N.
    top_n = 20
    assert m1.last_u == m2.last_u and m1.synced and m2.synced
    b1, a1 = m1.top_levels(top_n)
    b2, a2 = m2.top_levels(top_n)
    norm = lambda lvls: [(float(p), float(q)) for p, q in lvls]
    assert norm(b1) == norm(b2), "reconstructed bids differ from the independent snapshot's book"
    assert norm(a1) == norm(a2), "reconstructed asks differ from the independent snapshot's book"

    # ADVERSARIAL: a corrupted book (missed qty-0 / inverted level) must fail the match.
    worst = b1[0][0]
    m1.bids[worst] = str(float(m1.bids[worst]) + 10_000.0)   # corrupt best-bid qty
    bc, _ = m1.top_levels(top_n)
    assert norm(bc) != norm(b2), "a corrupted book must NOT match the ground truth"
