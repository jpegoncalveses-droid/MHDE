"""OKX contract-value (ctVal) table + contracts->coin conversion.

OKX WS payloads express size in CONTRACTS; the byte-identical Binance rows store
coin-denominated quantities. One contract is ``ctVal`` coins (a decimal STRING on
the ``/public/instruments`` payload: BTC 0.01, ETH 0.1, PEPE 10000000, SHIB 1000000).
The conversion uses :class:`decimal.Decimal` — float would lose precision on the
1000x multiplier symbols. This is the first ctVal facility in the codebase; it is
sourced from the same ``/public/instruments`` payload the universe filter consumes
(:mod:`crypto.research.capture_core_okx.symbols`) and refreshed on the same hourly
universe re-resolve.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping

from crypto.research.capture_core_okx.symbols import is_capture_instrument


def parse_ctval_table(instruments: Iterable[Mapping]) -> dict[str, Decimal]:
    """``instId -> Decimal(ctVal)`` for the capture universe, from ``/public/instruments`` rows.

    Non-universe rows (inverse, equity perps, dead) are excluded via the shared
    Stage A predicate, so the ctVal table and the symbol universe never diverge.
    """
    table: dict[str, Decimal] = {}
    for inst in instruments:
        if is_capture_instrument(inst):
            table[inst["instId"]] = Decimal(str(inst["ctVal"]))
    return table


def contracts_to_coin(size_contracts: str, ct_val: Decimal) -> str:
    """``size_contracts x ct_val`` as an exact fixed-point coin string (never float).

    Fixed-point formatting (``format(..., "f")``) keeps large PEPE/SHIB products out
    of scientific notation so the stored value stays a clean, ``float()``-castable
    venue string — the brain WS readers cast q/B/A with no ``_safe_float`` tolerance.
    """
    return format(Decimal(str(size_contracts)) * ct_val, "f")
