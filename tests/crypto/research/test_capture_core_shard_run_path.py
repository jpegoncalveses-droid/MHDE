"""ADR-039 stage 2b piece 1 — shard-aware run path (--shard/--of + SnapshotClient seeding).

Single-box; the owner / REST are faked. No systemd/cpuset/multi-process launch. The
default (no --shard, or --of 1) must stay byte-identical to today's single full-universe
process so the parked config is inert.
"""
from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

from crypto.research.capture_core import service as svc
from crypto.research.capture_core import sharding
from crypto.research.capture_core import snapshot_owner as so

_UNIV = [f"SYM{i}USDT" for i in range(60)]


class FakeUniverseClient:
    def __init__(self, universe):
        self._u = list(universe)

    def fetch_usdtm_perp_universe(self):
        return list(self._u)

    def fetch_depth_snapshot(self, symbol, limit):   # must NOT be hit on the socket path
        raise AssertionError("direct REST depth fetch must not be called here")


def _svc(**kw):
    return svc.CaptureService(root="/tmp/unused-2b", client=FakeUniverseClient(_UNIV),
                              install_signals=False, enable_snapshots=False,
                              disk_guard_enabled=False, inode_guard_enabled=False, **kw)


# -- (a) subset = symbols_for_shard(N,K), disjoint + full coverage ------------

def test_resolve_universe_returns_shard_subset():
    for n in (2, 3, 4):
        for k in range(n):
            got = asyncio.run(_svc(shard=k, n_shards=n)._resolve_universe())
            assert got == sharding.symbols_for_shard(_UNIV, k, n)


def test_shard_subsets_are_disjoint_and_cover_universe():
    for n in (2, 3, 4):
        seen = []
        for k in range(n):
            seen += asyncio.run(_svc(shard=k, n_shards=n)._resolve_universe())
        assert sorted(seen) == sorted(_UNIV)            # full coverage
        assert len(seen) == len(set(seen))              # disjoint


def test_no_shard_resolves_full_universe():
    assert asyncio.run(_svc()._resolve_universe()) == _UNIV     # shard=None default


# -- (b) shard subscribes to ONLY its subset's streams -----------------------

def _per_symbol_syms(streams):
    return {st.split("@", 1)[0].upper() for st in streams if not st.startswith("!")}


def test_non_owner_shard_subscribes_only_to_subset_no_array_streams():
    subset = sharding.symbols_for_shard(_UNIV, 1, 3)             # shard 1: not array owner
    streams = svc.capture_streams_for_shard(subset, owns_array_streams=False)
    assert _per_symbol_syms(streams) == {x.upper() for x in subset}
    assert set(subset) < set(_UNIV)                             # genuinely a strict subset
    assert not any(st.startswith("!") for st in streams)        # NO market-wide array streams


def test_array_owner_shard_includes_array_streams():
    subset = sharding.symbols_for_shard(_UNIV, 0, 3)
    streams = svc.capture_streams_for_shard(subset, owns_array_streams=True)
    assert _per_symbol_syms(streams) == {x.upper() for x in subset}
    assert any(st.startswith("!markPrice@arr") for st in streams)
    assert "!forceOrder@arr" in streams


def test_only_shard0_or_single_process_owns_array_streams():
    assert _svc()._owns_array is True                           # single-process
    assert _svc(shard=0, n_shards=3)._owns_array is True
    assert _svc(shard=1, n_shards=3)._owns_array is False
    assert _svc(shard=2, n_shards=3)._owns_array is False


# -- (c) seeding: SnapshotClient when socket configured, else direct REST -----

def test_socket_configured_seeds_via_snapshot_client_not_direct_rest():
    calls = []

    class FakeClient:
        async def request(self, symbol):
            calls.append(symbol)
            return {"symbol": symbol, "snapshot": {"lastUpdateId": 1}}

    s = _svc(snapshot_socket_path="/tmp/owner.sock",
             snapshot_client_factory=lambda path: FakeClient())
    assert isinstance(s._snap_sched, so.SnapshotClientScheduler)

    s.seed_universe(["SYM0USDT", "SYM1USDT"])

    async def drain():
        task = asyncio.ensure_future(s._snap_sched.run())
        for _ in range(200):
            await asyncio.sleep(0)
            if len(calls) >= 2:
                break
        s._snap_sched.stop()
        await task

    asyncio.run(drain())
    assert set(calls) == {"SYM0USDT", "SYM1USDT"}      # seeded via the owner, not REST


def test_no_socket_uses_direct_rest_scheduler():
    from crypto.research.capture_core.snapshot import SnapshotScheduler
    s = svc.CaptureService(root="/tmp/unused-2b", client=FakeUniverseClient(_UNIV),
                           install_signals=False, enable_snapshots=True,
                           disk_guard_enabled=False, inode_guard_enabled=False)
    assert isinstance(s._snap_sched, SnapshotScheduler)
    assert not isinstance(s._snap_sched, so.SnapshotClientScheduler)


# -- (d) backward compat: CLI guard + --of 1 == single full-universe process --

def test_cli_rejects_invalid_shard_args():
    from main import cli
    r = CliRunner()
    assert r.invoke(cli, ["crypto", "capture-core-run", "--of", "0"]).exit_code != 0
    assert r.invoke(cli, ["crypto", "capture-core-run", "--of", "3"]).exit_code != 0   # missing --shard
    assert r.invoke(cli, ["crypto", "capture-core-run", "--of", "2",
                          "--shard", "2"]).exit_code != 0                              # out of range


def test_of_1_equals_single_process_full_universe():
    s = _svc(shard=0, n_shards=1)
    assert asyncio.run(s._resolve_universe()) == _UNIV         # --of 1 --shard 0 == full
    assert s._owns_array is True


# -- seed-loop isolation (parity with the SnapshotScheduler this replaced) -----

def test_seed_loop_survives_a_malformed_or_raising_owner_response():
    # A single symbol whose owner response raises (e.g. a malformed/non-JSON reply
    # surfacing as a decode error) must NOT kill the seeding loop and silently stale
    # the whole shard's books — the other symbols still seed, the loop stays alive.
    seeded = []

    class FlakyClient:
        async def request(self, symbol):
            if symbol == "BADUSDT":
                raise ValueError("malformed owner response")
            return {"symbol": symbol, "snapshot": {"lastUpdateId": 1}}

    sched = so.SnapshotClientScheduler(
        client=FlakyClient(), on_snapshot=lambda sym, snap, ns: seeded.append(sym))
    sched.request("OKUSDT")
    sched.request("BADUSDT")
    sched.request("OK2USDT")

    async def drive():
        task = asyncio.ensure_future(sched.run())
        for _ in range(500):
            await asyncio.sleep(0)
            if len(seeded) >= 2 and sched.errors >= 1:
                break
        sched.stop()
        await task                                  # raises if the loop died

    asyncio.run(drive())
    assert set(seeded) == {"OKUSDT", "OK2USDT"}     # good symbols seeded despite the bad one
    assert sched.errors >= 1                         # bad symbol counted, loop survived


def test_seed_loop_does_not_swallow_cancellation():
    # `except Exception` (not BaseException): CancelledError must propagate so async
    # shutdown stays clean (same for KeyboardInterrupt/SystemExit).
    class CancellingClient:
        async def request(self, symbol):
            raise asyncio.CancelledError()

    sched = so.SnapshotClientScheduler(client=CancellingClient(), on_snapshot=lambda *a: None)
    sched.request("X")

    async def drive():
        await sched.run()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(drive())
