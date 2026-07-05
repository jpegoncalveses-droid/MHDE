"""OKX instrument -> Binance-style symbol normalization + the universe filter.

The write-time normalization contract: rows are written under Binance-style
symbol names (``BTC-USDT-SWAP`` -> ``BTCUSDT``) so the brain's on-disk
universe enumeration and readers need zero changes. The 5 Binance multiplier
symbols (1000PEPE, ...) are NEVER bridged: OKX ``PEPEUSDT`` is a distinct
series at 1000x the scale of Binance ``1000PEPEUSDT``.

Universe filter (recon Q2, live-verified 2026-07-03): ``ctType == linear``,
``settleCcy == USDT``, ``state == live``, and ``instCategory == "1"`` — the
last one is LOAD-BEARING: OKX lists ~121 equity/pre-IPO perps (instCategory
"3"/"4", e.g. AAPL-USDT-SWAP) under instType=SWAP with ordinary
``-USDT-SWAP`` names.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

_SUFFIX = "-USDT-SWAP"


def instid_to_symbol(inst_id: str) -> Optional[str]:
    """``BASE-USDT-SWAP`` -> ``BASEUSDT``; anything else -> None (not ours)."""
    if not inst_id.endswith(_SUFFIX):
        return None
    base = inst_id[: -len(_SUFFIX)]
    if not base or "-" in base:
        return None
    return base + "USDT"


def is_capture_instrument(inst: Mapping) -> bool:
    """The four-way Stage A universe predicate over one ``/public/instruments`` row."""
    return (
        inst.get("ctType") == "linear"
        and inst.get("settleCcy") == "USDT"
        and inst.get("state") == "live"
        and inst.get("instCategory") == "1"      # crypto only; excludes equity perps
        and instid_to_symbol(inst.get("instId", "")) is not None
    )


def filter_universe(instruments: Iterable[Mapping]) -> list[str]:
    """Sorted instIds of the capture universe from raw instrument rows."""
    return sorted(i["instId"] for i in instruments if is_capture_instrument(i))


class SymbolMap:
    """Mutable holder of the resolved universe, shared by the all-scope join
    parsers (which must filter venue-wide payloads to the universe) and updated
    in place on the hourly re-resolve so the parser closures stay current."""

    def __init__(self, inst_ids: Iterable[str]) -> None:
        self._ids: list[str] = []
        self._symbols: dict[str, str] = {}
        self.update(inst_ids)

    def update(self, inst_ids: Iterable[str]) -> None:
        self._ids = sorted(inst_ids)
        self._symbols = {}
        for inst_id in self._ids:
            symbol = instid_to_symbol(inst_id)
            if symbol is not None:
                self._symbols[inst_id] = symbol

    @property
    def inst_ids(self) -> list[str]:
        return list(self._ids)

    def contains(self, inst_id: str) -> bool:
        return inst_id in self._symbols

    def symbol_for(self, inst_id: str) -> Optional[str]:
        return self._symbols.get(inst_id)
