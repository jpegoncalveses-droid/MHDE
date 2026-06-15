"""Capture-core symbol sharding (ADR-039, Stage 1).

A pure, deterministic ``symbol -> shard`` assignment so a symbol is always owned by
the SAME shard across processes and restarts (stable across the hourly universe
re-resolve and across machine restarts).

It uses a **stable content hash** (``hashlib.blake2b``), NOT the builtin ``hash()``:
Python salts ``str`` hashing per process (``PYTHONHASHSEED``), so ``hash(symbol) % N``
would re-map every symbol on each restart and scatter a partition's history across
shards. blake2b depends only on the bytes, giving the same assignment everywhere.

Stage 1 ships only this pure function (consumed by the future per-process run path)
and the ``part-<shard>-*`` writer naming in :mod:`store`. The multi-process launch,
systemd template, cpuset pinning, and REST snapshot-owner are Stage 2.
"""
from __future__ import annotations

import hashlib
from typing import Sequence

from crypto.research.capture_core import config as cfg


def shard_for_symbol(symbol: str, n_shards: int = cfg.CAPTURE_N_SHARDS) -> int:
    """Deterministic shard index in ``[0, n_shards)`` for ``symbol``.

    Stable across processes/restarts (blake2b of the symbol bytes, never the salted
    builtin ``hash``) and roughly even across symbols. ``n_shards <= 1`` -> shard 0.
    """
    if n_shards <= 1:
        return 0
    digest = hashlib.blake2b(symbol.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % n_shards


def symbols_for_shard(symbols: Sequence[str], shard_id: int,
                      n_shards: int = cfg.CAPTURE_N_SHARDS) -> list[str]:
    """The subset of ``symbols`` owned by ``shard_id`` (input order preserved).

    Across ``shard_id in range(n_shards)`` the subsets are disjoint and cover the
    whole universe — each symbol lands in exactly one shard.
    """
    return [s for s in symbols if shard_for_symbol(s, n_shards) == shard_id]
