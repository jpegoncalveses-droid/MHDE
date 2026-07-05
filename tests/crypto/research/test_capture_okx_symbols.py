"""OKX Stage A: symbol mapping + universe filter (write-time normalization contract).

The OKX collectors write Binance-style symbol names so the brain readers need zero
changes. The mapping is mechanical (strip ``-USDT-SWAP``); the 5 Binance multiplier
symbols (1000PEPE etc.) are NEVER bridged — OKX ``PEPEUSDT`` is a distinct series.
The universe filter is the recon-pinned four-way predicate; ``instCategory == "1"``
is LOAD-BEARING: OKX lists ~121 equity/pre-IPO perps (AAPL-USDT-SWAP, ...) under
instType=SWAP with ordinary ``-USDT-SWAP`` names (live-verified 2026-07-03).
"""
from __future__ import annotations

from crypto.research.capture_core_okx import symbols as sym


def _inst(inst_id, *, ct_type="linear", settle="USDT", state="live", cat="1"):
    return {"instId": inst_id, "ctType": ct_type, "settleCcy": settle,
            "state": state, "instCategory": cat}


# -- instid_to_symbol ----------------------------------------------------------

def test_instid_maps_to_binance_style():
    assert sym.instid_to_symbol("BTC-USDT-SWAP") == "BTCUSDT"
    assert sym.instid_to_symbol("1INCH-USDT-SWAP") == "1INCHUSDT"


def test_multiplier_symbols_are_never_bridged():
    # OKX PEPE-USDT-SWAP is PEPEUSDT — a DISTINCT series from Binance 1000PEPEUSDT
    # (1000x scale). The mapping must never manufacture the 1000-prefix.
    assert sym.instid_to_symbol("PEPE-USDT-SWAP") == "PEPEUSDT"
    assert sym.instid_to_symbol("SHIB-USDT-SWAP") == "SHIBUSDT"


def test_non_usdt_linear_instids_map_to_none():
    assert sym.instid_to_symbol("BTC-USD-SWAP") is None      # inverse
    assert sym.instid_to_symbol("BTC-USDC-SWAP") is None     # USDC-margined
    assert sym.instid_to_symbol("BTC-USDT") is None          # spot/index id
    assert sym.instid_to_symbol("BTC-USDT-251226") is None   # dated future


# -- is_capture_instrument / filter_universe ------------------------------------

def test_filter_requires_all_four_predicates():
    good = _inst("BTC-USDT-SWAP")
    assert sym.is_capture_instrument(good)
    assert not sym.is_capture_instrument(_inst("BTC-USD-SWAP", ct_type="inverse",
                                                settle="BTC"))
    assert not sym.is_capture_instrument(_inst("X-USDT-SWAP", state="suspend"))
    assert not sym.is_capture_instrument(_inst("Y-USDT-SWAP", state="preopen"))
    # the load-bearing one: equity/pre-IPO perps carry ordinary -USDT-SWAP names
    assert not sym.is_capture_instrument(_inst("AAPL-USDT-SWAP", cat="3"))
    assert not sym.is_capture_instrument(_inst("Z-USDT-SWAP", cat="4"))
    assert not sym.is_capture_instrument(_inst("W-USDT-SWAP", cat=""))


def test_filter_universe_returns_sorted_instids():
    instruments = [
        _inst("ETH-USDT-SWAP"),
        _inst("AAPL-USDT-SWAP", cat="3"),          # equity perp -> excluded
        _inst("BTC-USD-SWAP", ct_type="inverse", settle="BTC"),
        _inst("BTC-USDT-SWAP"),
        _inst("PEPE-USDT-SWAP"),
        _inst("NEW-USDT-SWAP", state="preopen"),   # not yet live -> excluded
    ]
    assert sym.filter_universe(instruments) == [
        "BTC-USDT-SWAP", "ETH-USDT-SWAP", "PEPE-USDT-SWAP"]


# -- SymbolMap (shared holder used by the all-scope join parsers) ---------------

def test_symbol_map_lookup_and_update():
    m = sym.SymbolMap(["BTC-USDT-SWAP", "PEPE-USDT-SWAP"])
    assert m.inst_ids == ["BTC-USDT-SWAP", "PEPE-USDT-SWAP"]
    assert m.contains("BTC-USDT-SWAP")
    assert not m.contains("AAPL-USDT-SWAP")
    assert m.symbol_for("PEPE-USDT-SWAP") == "PEPEUSDT"
    assert m.symbol_for("AAPL-USDT-SWAP") is None
    m.update(["ETH-USDT-SWAP"])
    assert m.inst_ids == ["ETH-USDT-SWAP"]
    assert not m.contains("BTC-USDT-SWAP")
    assert m.symbol_for("ETH-USDT-SWAP") == "ETHUSDT"
